"""
Валидатор базы препаратов vetvoice (drugs_calc.json).

Сравнивает данные vetvoice с данными из открытых источников:
  * vetprotocol.ru (по МНН)
  * vetlek.ru (по торговому наименования)
  * vidal.ru/veterinar
  * galen.vetrf.ru (если есть API-ключ)

Проверяет:
  1. Дозировки: доза (мг/кг), частота, путь введения, курс
  2. Побочные действия: список побочек
  3. Противопоказания: беременность, лактация, возраст
  4. Лекарственная форма и концентрация
  5. Соответствие МНН

Формирует отчёт о расхождениях и предлагает исправления.

Использование:
    python validate_vetvoice.py --drugs-calc /path/to/drugs_calc.json \
                                 --vetprotocol /path/to/vetprotocol.json \
                                 --vetlek /path/to/vetlek.json \
                                 --output /path/to/report.json \
                                 --apply-fixes /path/to/drugs_calc_fixed.json
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger("vetvoice_validator")
if not log.handlers:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


# ---------------------------------------------------------------------------
# Модели результатов валидации
# ---------------------------------------------------------------------------

@dataclass
class Discrepancy:
    """Одно найденное расхождение."""
    drug_id: int = 0
    drug_name: str = ""
    field: str = ""  # dose_per_kg, frequency, side_effects, ...
    vetvoice_value: Any = None
    source_value: Any = None
    source: str = ""  # vetprotocol / vetlek / vidal / galen
    severity: str = "info"  # info / warning / error
    suggested_fix: Any = None
    notes: str = ""


@dataclass
class ValidationReport:
    """Полный отчёт по валидации."""
    total_drugs: int = 0
    checked_drugs: int = 0
    matched_drugs: int = 0
    discrepancies: List[Discrepancy] = field(default_factory=list)
    fixed_drugs: int = 0
    sources_used: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "total_drugs": self.total_drugs,
            "checked_drugs": self.checked_drugs,
            "matched_drugs": self.matched_drugs,
            "discrepancies_count": len(self.discrepancies),
            "fixed_drugs": self.fixed_drugs,
            "sources_used": self.sources_used,
            "discrepancies_by_severity": {
                s: sum(1 for d in self.discrepancies if d.severity == s)
                for s in ("info", "warning", "error")
            },
            "discrepancies_by_field": _count_by_field(self.discrepancies),
            "discrepancies": [d.to_dict() if hasattr(d, 'to_dict') else asdict(d)
                              for d in self.discrepancies],
        }


def _count_by_field(discrepancies: List[Discrepancy]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for d in discrepancies:
        counts[d.field] = counts.get(d.field, 0) + 1
    return dict(sorted(counts.items(), key=lambda x: -x[1]))


#monkey-patch to_dict if not present
def _discrepancy_to_dict(self) -> dict:
    return asdict(self)
Discrepancy.to_dict = _discrepancy_to_dict


# ---------------------------------------------------------------------------
# Утилиты для нормализации строк
# ---------------------------------------------------------------------------

_PUNCT_RE = re.compile(r"[^\w\s%/-]", re.U)
_WS_RE = re.compile(r"\s+", re.U)
_DOSE_NUM_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*(мг|мл|мкг|МЕ|г)\s*/\s*(кг|м²|м2)?", re.I)


def normalize_str(s: Any) -> str:
    """Привести строку к каноничному виду для сравнения."""
    if s is None:
        return ""
    s = str(s).lower().strip()
    s = _PUNCT_RE.sub(" ", s)
    s = _WS_RE.sub(" ", s).strip()
    return s


def normalize_name(s: str) -> str:
    """Нормализовать название препарата для матчинга."""
    s = normalize_str(s)
    # удалить символ «®» и всякие специальные знаки
    s = re.sub(r"[®™]", "", s)
    # удалить общие слова
    for w in ("раствор", "суспензия", "таблетки", "порошок", "гель",
              "мазь", "капли", "инъекций", "для", "применения"):
        s = re.sub(rf"\b{w}\b", "", s)
    s = _WS_RE.sub(" ", s).strip()
    return s


def extract_dose_numbers(text: str) -> List[float]:
    """Извлечь все числовые значения доз из текста."""
    if not text:
        return []
    nums = []
    for m in _DOSE_NUM_RE.finditer(text.lower()):
        try:
            nums.append(float(m.group(1).replace(",", ".")))
        except ValueError:
            pass
    return nums


def dose_values_close(a: Optional[float], b: Optional[float],
                      tolerance: float = 0.2) -> bool:
    """Сравнить две дозы с допуском (20% по умолчанию)."""
    if a is None or b is None:
        return False
    if a == 0 and b == 0:
        return True
    if a == 0 or b == 0:
        return False
    ratio = abs(a - b) / max(a, b)
    return ratio <= tolerance


# ---------------------------------------------------------------------------
# Сопоставление препаратов vetvoice с источниками
# ---------------------------------------------------------------------------

def build_vetprotocol_index(data: List[dict]) -> Dict[str, dict]:
    """Индекс препаратов vetprotocol по МНН и slug."""
    idx: Dict[str, dict] = {}
    for d in data:
        if not d.get("name"):
            continue
        # по МНН (нормализованное имя)
        key = normalize_name(d["name"])
        if key:
            idx[key] = d
        # по синонимам
        for syn in d.get("synonyms", []):
            k = normalize_name(syn)
            if k:
                idx.setdefault(k, d)
        # по slug
        if d.get("slug"):
            idx[f"slug:{d['slug']}"] = d
    return idx


def build_vetlek_index(data: List[dict]) -> Dict[str, dict]:
    """Индекс инструкций vetlek по нормализованному названию."""
    idx: Dict[str, dict] = {}
    for d in data:
        if not d.get("title"):
            continue
        key = normalize_name(d["title"])
        if key:
            idx[key] = d
        # по ID
        if d.get("direction_id"):
            idx[f"id:{d['direction_id']}"] = d
    return idx


def build_fsvps_index(data: List[dict]) -> Dict[str, dict]:
    """Индекс препаратов fsvps по торговому наименованию и МНН.

    fsvps — это госреестр Россельхознадзора (Открытые данные), содержит
    2347 препаратов с полными структурированными полями. Покрывает в т.ч.
    иммунобиологические (вакцины, сыворотки) — то, что не покрывают
    vetprotocol/vetlek.
    """
    idx: Dict[str, dict] = {}
    for d in data:
        # по торговому наименованию
        if d.get("trade_name"):
            key = normalize_name(d["trade_name"])
            if key:
                idx.setdefault(key, d)
        # по МНН
        if d.get("inn"):
            key = normalize_name(d["inn"])
            if key:
                idx.setdefault(key, d)
    return idx


def find_in_fsvps(
    vv_drug: dict, idx: Dict[str, dict]
) -> Optional[dict]:
    """Найти соответствие в fsvps по названию или МНН."""
    candidates = []
    if vv_drug.get("name"):
        candidates.append(vv_drug["name"])
    if vv_drug.get("inn"):
        candidates.append(vv_drug["inn"])

    for cand in candidates:
        key = normalize_name(cand)
        if key in idx:
            return idx[key]
    # Fuzzy: подстрока
    for cand in candidates:
        key = normalize_name(cand)
        if not key or len(key) < 4:
            continue
        for k, v in idx.items():
            if (key in k or k in key) and len(k) > 4:
                return v
    return None


def find_in_vetprotocol(
    vv_drug: dict, idx: Dict[str, dict]
) -> Optional[dict]:
    """Найти соответствие в vetprotocol по МНН или торговому наименованию."""
    candidates = []
    if vv_drug.get("inn"):
        candidates.append(vv_drug["inn"])
    if vv_drug.get("name"):
        candidates.append(vv_drug["name"])

    for cand in candidates:
        key = normalize_name(cand)
        if key in idx:
            return idx[key]
        # fuzzy: попробовать подстроку
        for k, v in idx.items():
            if k.startswith("slug:") or k.startswith("id:"):
                continue
            if key and (key in k or k in key) and len(key) > 3:
                return v
    return None


def find_in_vetlek(
    vv_drug: dict, idx: Dict[str, dict]
) -> Optional[dict]:
    """Найти соответствие в vetlek по названию."""
    candidates = []
    if vv_drug.get("name"):
        candidates.append(vv_drug["name"])
    if vv_drug.get("inn"):
        candidates.append(vv_drug["inn"])

    for cand in candidates:
        key = normalize_name(cand)
        if key in idx:
            return idx[key]
        # fuzzy
        for k, v in idx.items():
            if k.startswith("id:"):
                continue
            if key and (key in k or k in key) and len(key) > 4:
                return v
    return None


# ---------------------------------------------------------------------------
# Проверка отдельных полей
# ---------------------------------------------------------------------------

def _is_realistic_dose(value: float) -> bool:
    """Проверить, что доза (мг/кг) находится в реалистичном диапазоне.

    Большинство ветпрепаратов имеют дозу 0.001–50 мг/кг.
    Значения > 50 мг/кг — подозрительные (часто это разовая доза в мг,
    а не мг/кг). Жёсткий предел > 80 мг/кг считаем нереалистичным
    и не применяем как fix.
    """
    if value is None or value <= 0:
        return False
    return 0.001 <= value <= 80.0


def _is_safe_to_overwrite(old_val: float, new_val: float) -> bool:
    """Проверить, безопасно ли перезаписать old_val на new_val в aggressive режиме.

    Не перезаписываем если:
    - new_val нереалистичен
    - Различие > 10x (это скорее всего false positive в матчинге —
      совпало название, но МНН разный)
    - Одно из значений пустое/ноль
    """
    if old_val in (None, 0) or new_val in (None, 0):
        return old_val in (None, 0) and new_val not in (None, 0)
    if not _is_realistic_dose(new_val):
        return False
    if not _is_realistic_dose(old_val):
        return True  # старое явно кривое — заменяем
    # Различие > 10x — подозрительно, пропускаем
    ratio = max(old_val, new_val) / min(old_val, new_val)
    if ratio > 10:
        return False
    return True


# ---------------------------------------------------------------------------
# Видовая разница доз — главная фича валидатора
# ---------------------------------------------------------------------------
#
# В drugs_calc.json у каждого препарата есть:
#   - dose_per_kg (глобальная доза)
#   - animals: [Собаки, Кошки]  (какие виды вообще применимы)
#   - animal_specific: {
#       "Собаки": { dose_per_kg, dose_min, dose_max, method, frequency, notes },
#       "Кошки":  { ... }
#     }
#
# Если в vetvoice drug.animals = ["Собаки", "Кошки"], но dose_per_kg один —
# это подозрительно: дозы для собаки и кошки обычно различаются.
#
# vetprotocol хранит дозы с пометкой животного, например:
#   { animal: "Собаки", dose_per_kg: 50, ... }
#   { animal: "Кошки", dose_per_kg: 25, ... }
#
# Алгоритм:
#   1. Группируем дозы vetprotocol по животным (берём максимум из диапазона).
#   2. Для каждого животного из vetprotocol:
#      - Если в vetvoice.animal_specific[animal] нет дозы → добавляем (warning).
#      - Если есть и отличается >30% → отмечаем расхождение (info, ручная проверка).
#   3. Если у vetvoice drug.animals несколько видов, а animal_specific пустой
#      или dose_per_kg один на все → warning «нужны видовые дозы».
# ---------------------------------------------------------------------------

# Соответствие вариантов написания животных в vetlek текстах и
# каноничных имён в vetvoice
_VL_ANIMAL_ALIASES = {
    "Собаки": ["собак", "собаки", "собаку", "собакам"],
    "Кошки":  ["кошек", "кошки", "кошку", "кошкам"],
    "КРС":    ["крс", "крупного рогатого скота", "крупному рогатому скоту",
              "крупным рогатым скотом", "коров", "коровам", "тёлк", "телят",
              "теленка", "теленку", "быков"],
    "МРС":    ["мрс", "мелкого рогатого скота", "овец", "овцам", "овц",
              "коз", "козам"],
    "Свиньи": ["свин", "свиней", "свиньи", "свинью", "свиномат",
              "порос", "поросят", "поросятам"],
    "Лошади": ["лошад", "лошадей", "лошади", "лошадь", "лошадям",
              "коней", "коням"],
    "Птица":  ["птиц", "птицы", "птицу", "птицам", "кур", "курам",
              "цыплят", "цыплятам", "гусей", "гусей", "уток", "уткам",
              "индюш", "пернат"],
    "Кролики": ["кролик", "кроликов", "кролика", "кроликам"],
    "Пушные звери": ["пушн", "пушных", "пушным", "норк", "песец",
                    "лисиц", "собол"],
    "Пчёлы": ["пчёл", "пчел", "пчёлы", "пчелам", "пчелин"],
}


def _find_animals_in_text(text: str) -> List[str]:
    """Найти упоминания видов животных в тексте vetlek."""
    if not text:
        return []
    low = text.lower()
    found = []
    for canonical, aliases in _VL_ANIMAL_ALIASES.items():
        if any(a in low for a in aliases):
            found.append(canonical)
    return found


def _extract_vl_dosages_by_animal(text: str) -> Dict[str, List[float]]:
    """Извлечь дозы из текста vetlek с привязкой к животным.

    Ветlek-инструкции часто структурированы как:
      «Собакам и кошкам вводят внутримышечно в дозе 0,1 мл/кг»
      или
      «КРС — 5 мл на 100 кг массы, МРС — 2 мл»

    Парсим по предложениям, ищем упоминания животных и ближайшие числа.
    """
    if not text:
        return {}
    # Разбиваем на предложения/части по «;», «.», переносу
    parts = re.split(r"[;.]\s+|\n", text)
    result: Dict[str, List[float]] = {}
    for part in parts:
        animals = _find_animals_in_text(part)
        if not animals:
            continue
        # Ищем числа с единицами
        matches = re.finditer(
            r"(\d+(?:[.,]\d+)?)\s*(мг|мл|мкг|МЕ|г)\s*/?\s*(кг|м²|м2)?",
            part, re.I,
        )
        doses = []
        for m in matches:
            try:
                val = float(m.group(1).replace(",", "."))
                # только мг/кг (если есть явный «/кг»)
                if m.group(3) and "кг" in m.group(3).lower():
                    if _is_realistic_dose(val):
                        doses.append(val)
            except ValueError:
                pass
        if doses:
            for animal in animals:
                result.setdefault(animal, []).extend(doses)
    return result


def _group_vp_doses_by_animal(vp_drug: dict) -> Dict[str, Dict[str, Any]]:
    """Сгруппировать дозы vetprotocol по животным.

    Возвращает {animal: {dose_per_kg, dose_unit, frequency, route, course_days,
                         indication, count}}
    Если для животного несколько доз — берём среднее с min/max.
    """
    result: Dict[str, Dict[str, Any]] = {}
    for d in vp_drug.get("doses", []):
        animal = d.get("animal", "")
        if not animal:
            continue
        # Нормализуем имя животного
        animal_norm = _normalize_vv_animal(animal)
        dose = d.get("dose_per_kg")
        if dose is None:
            continue
        if animal_norm not in result:
            result[animal_norm] = {
                "doses": [],
                "dose_unit": d.get("dose_unit", ""),
                "frequency": d.get("frequency", ""),
                "route": d.get("route", ""),
                "course_days": d.get("course_days", ""),
                "indications": [],
            }
        result[animal_norm]["doses"].append(dose)
        if d.get("indication"):
            result[animal_norm]["indications"].append(d["indication"])
    # Усредняем
    for animal, info in result.items():
        doses = info.pop("doses")
        if doses:
            info["dose_per_kg"] = max(doses)  # берём верхнюю границу диапазона
            info["dose_min"] = min(doses)
            info["dose_max"] = max(doses)
            info["count"] = len(doses)
        else:
            info["dose_per_kg"] = None
            info["count"] = 0
        info["indications"] = list(set(info["indications"]))[:3]
    return result


def _normalize_vv_animal(name: str) -> str:
    """Нормализовать имя животного к каноничному виду vetvoice."""
    if not name:
        return ""
    name = name.strip()
    # прямой поиск по алиасам
    low = name.lower()
    for canonical, aliases in _VL_ANIMAL_ALIASES.items():
        for alias in aliases:
            if alias in low:
                return canonical
    # Если не нашли — возвращаем как есть (возможно, уже каноничное)
    return name


def check_dosage_by_animal(
    vv_drug: dict, vp_drug: Optional[dict], vl_drug: Optional[dict],
    discrepancies: List[Discrepancy], drug_id: int, drug_name: str,
) -> None:
    """⭐ Главная проверка: видовая разница доз.

    Сравнивает vetprotocol/vetlek дозы по животным с vetvoice.animal_specific.
    """
    vv_animals = vv_drug.get("animals", []) or []
    vv_animal_specific = vv_drug.get("animal_specific", {}) or {}
    vv_dose_per_kg = vv_drug.get("dose_per_kg")

    # 1) Если в vetvoice указано несколько животных, но dose_per_kg один
    # и animal_specific пустой или содержит одно значение — это подозрительно
    if (len(vv_animals) > 1 and vv_dose_per_kg
            and (not vv_animal_specific or len(vv_animal_specific) < 2)):
        discrepancies.append(Discrepancy(
            drug_id=drug_id, drug_name=drug_name,
            field="animal_specific.missing",
            vetvoice_value={
                "animals": vv_animals,
                "dose_per_kg": vv_dose_per_kg,
                "animal_specific_keys": list(vv_animal_specific.keys()),
            },
            source_value=None,
            source="self",
            severity="warning",
            suggested_fix=None,
            notes=f"Препарат указан для {len(vv_animals)} видов "
                  f"({', '.join(vv_animals)}), но animal_specific неполный. "
                  f"Дозировка для разных видов часто различается — "
                  f"нужно заполнить animal_specific для каждого вида.",
        ))

    # 2) Сравнение с vetprotocol: группируем дозы vetprotocol по животным
    if vp_drug:
        vp_by_animal = _group_vp_doses_by_animal(vp_drug)
        for animal, vp_info in vp_by_animal.items():
            vp_dose = vp_info.get("dose_per_kg")
            if vp_dose is None:
                continue
            is_realistic = _is_realistic_dose(vp_dose)
            unit_has_per_kg = "/" in vp_info.get("dose_unit", "") and \
                              "кг" in vp_info.get("dose_unit", "")

            # Ищем это животное в animal_specific vetvoice
            vv_spec = vv_animal_specific.get(animal)
            if vv_spec:
                # В animal_specific уже есть доза для этого животного
                vv_animal_dose = vv_spec.get("dose_per_kg")
                if vv_animal_dose in (None, 0):
                    # доза пустая — предложим заполнить
                    if is_realistic and unit_has_per_kg:
                        discrepancies.append(Discrepancy(
                            drug_id=drug_id, drug_name=drug_name,
                            field=f"animal_specific.{animal}.dose_per_kg",
                            vetvoice_value=vv_animal_dose,
                            source_value=vp_dose,
                            source="vetprotocol",
                            severity="warning",
                            suggested_fix={
                                "dose_per_kg": vp_dose,
                                "dose_unit": vp_info.get("dose_unit", "мг/кг"),
                                "frequency": vp_info.get("frequency", ""),
                                "method": vp_info.get("route", ""),
                                "course_days": vp_info.get("course_days", ""),
                            },
                            notes=f"vetprotocol знает дозу для {animal}: "
                                  f"{vp_dose} {vp_info.get('dose_unit', '')}, "
                                  f"а в vetvoice animal_specific.{animal}.dose_per_kg "
                                  f"пусто.",
                        ))
                elif not dose_values_close(vv_animal_dose, vp_dose, 0.3):
                    # Дозы различаются — ручная проверка
                    discrepancies.append(Discrepancy(
                        drug_id=drug_id, drug_name=drug_name,
                        field=f"animal_specific.{animal}.dose_per_kg",
                        vetvoice_value=vv_animal_dose,
                        source_value=vp_dose,
                        source="vetprotocol",
                        severity="info" if not is_realistic else "warning",
                        suggested_fix=None,  # не перезаписываем
                        notes=f"Расхождение для {animal}: "
                              f"vetvoice={vv_animal_dose}, "
                              f"vetprotocol={vp_dose} — "
                              f"требуется ручная проверка",
                    ))
            else:
                # В animal_specific нет этого животного — предложим добавить
                if animal in vv_animals:
                    # Если в vv_animals это животное есть, а в animal_specific
                    # нет — это явный пробел
                    if is_realistic and unit_has_per_kg:
                        discrepancies.append(Discrepancy(
                            drug_id=drug_id, drug_name=drug_name,
                            field=f"animal_specific.{animal}",
                            vetvoice_value=None,
                            source_value={
                                "dose_per_kg": vp_dose,
                                "dose_unit": vp_info.get("dose_unit", "мг/кг"),
                                "frequency": vp_info.get("frequency", ""),
                                "method": vp_info.get("route", ""),
                                "course_days": vp_info.get("course_days", ""),
                                "notes": "; ".join(vp_info.get("indications", [])),
                            },
                            source="vetprotocol",
                            severity="warning",
                            suggested_fix={
                                "animal": animal,
                                "data": {
                                    "dose_per_kg": vp_dose,
                                    "dose_unit": vp_info.get("dose_unit", "мг/кг"),
                                    "frequency": vp_info.get("frequency", ""),
                                    "method": vp_info.get("route", ""),
                                    "course_days": vp_info.get("course_days", ""),
                                    "notes": "; ".join(vp_info.get("indications", [])),
                                },
                            },
                            notes=f"vetvoice.animals содержит '{animal}', "
                                  f"но в animal_specific его нет. "
                                  f"vetprotocol указывает дозу {vp_dose} "
                                  f"{vp_info.get('dose_unit', '')}.",
                        ))
                else:
                    # Этого животного вообще нет в vv_animals, но vetprotocol
                    # знает его дозу — возможно, vetvoice неполный
                    if is_realistic and unit_has_per_kg:
                        discrepancies.append(Discrepancy(
                            drug_id=drug_id, drug_name=drug_name,
                            field=f"animals.missing.{animal}",
                            vetvoice_value=vv_animals,
                            source_value=animal,
                            source="vetprotocol",
                            severity="info",
                            suggested_fix=None,
                            notes=f"vetprotocol знает дозу для {animal} "
                                  f"({vp_dose} {vp_info.get('dose_unit', '')}), "
                                  f"но в vetvoice.animals этого вида нет.",
                        ))

    # 3) Парсим vetlek на видовые дозы
    if vl_drug and vl_drug.get("dosage"):
        vl_by_animal = _extract_vl_dosages_by_animal(vl_drug["dosage"])
        for animal, doses in vl_by_animal.items():
            if not doses:
                continue
            vp_dose_max = max(doses)
            vv_spec = vv_animal_specific.get(animal)
            if vv_spec and vv_spec.get("dose_per_kg") in (None, 0):
                # В animal_specific доза пустая — предложим
                discrepancies.append(Discrepancy(
                    drug_id=drug_id, drug_name=drug_name,
                    field=f"animal_specific.{animal}.dose_per_kg",
                    vetvoice_value=vv_spec.get("dose_per_kg"),
                    source_value=vp_dose_max,
                    source="vetlek",
                    severity="warning",
                    suggested_fix={
                        "dose_per_kg": vp_dose_max,
                        "dose_unit": "мг/кг",
                    },
                    notes=f"vetlek указывает дозу для {animal}: "
                          f"~{vp_dose_max} мг/кг "
                          f"(из текста: {vl_drug['dosage'][:150]})",
                ))
            elif not vv_spec and animal in vv_animals:
                # В animal_specific нет этого животного — предложим добавить
                discrepancies.append(Discrepancy(
                    drug_id=drug_id, drug_name=drug_name,
                    field=f"animal_specific.{animal}",
                    vetvoice_value=None,
                    source_value={
                        "dose_per_kg": vp_dose_max,
                        "dose_unit": "мг/кг",
                    },
                    source="vetlek",
                    severity="warning",
                    suggested_fix={
                        "animal": animal,
                        "data": {
                            "dose_per_kg": vp_dose_max,
                            "dose_unit": "мг/кг",
                        },
                    },
                    notes=f"vetvoice.animals содержит '{animal}', "
                          f"но в animal_specific его нет. "
                          f"vetlek указывает ~{vp_dose_max} мг/кг.",
                ))


def check_dosage(
    vv_drug: dict, vp_drug: Optional[dict], vl_drug: Optional[dict],
    discrepancies: List[Discrepancy], drug_id: int, drug_name: str,
) -> None:
    """Проверить дозировки."""
    vv_dose = vv_drug.get("dose_per_kg")
    vv_min = vv_drug.get("dose_min")
    vv_max = vv_drug.get("dose_max")
    vv_unit = vv_drug.get("dose_unit", "")

    # vetprotocol: есть список doses с dose_per_kg
    if vp_drug:
        for vp_d in vp_drug.get("doses", []):
            vp_dose = vp_d.get("dose_per_kg")
            vp_unit = vp_d.get("dose_unit", "")
            if vp_dose is None:
                continue
            # Проверяем реалистичность: если доза > 100 мг/кг, скорее всего
            # это разовая доза (мг), а не на кг — пропускаем как fix
            is_realistic = _is_realistic_dose(vp_dose)
            # И если unit не содержит /кг, тоже пропускаем fix
            unit_has_per_kg = "/" in vp_unit and "кг" in vp_unit

            # Если в vetvoice нет дозы, но vetprotocol её знает — warning
            if vv_dose in (None, 0) and vp_dose:
                # Применяем только если доза реалистична и unit = мг/кг
                if is_realistic and unit_has_per_kg:
                    severity = "warning"
                    suggested = vp_dose
                else:
                    severity = "info"
                    suggested = None  # не применяем
                discrepancies.append(Discrepancy(
                    drug_id=drug_id, drug_name=drug_name,
                    field="dose_per_kg",
                    vetvoice_value=vv_dose,
                    source_value=vp_dose,
                    source="vetprotocol",
                    severity=severity,
                    suggested_fix=suggested,
                    notes=f"vetprotocol указывает дозу {vp_dose} "
                          f"{vp_unit} для "
                          f"{vp_d.get('animal', '?')}"
                          + ("" if is_realistic else " (нереалистичная, не применяем)"),
                ))
                break
            # Если дозы значительно различаются
            if vv_dose and not dose_values_close(vv_dose, vp_dose, 0.3):
                discrepancies.append(Discrepancy(
                    drug_id=drug_id, drug_name=drug_name,
                    field="dose_per_kg",
                    vetvoice_value=vv_dose,
                    source_value=vp_dose,
                    source="vetprotocol",
                    severity="info" if not is_realistic else "warning",
                    suggested_fix=None,  # не перезаписываем существующее
                    notes=f"Расхождение в дозе: vetvoice={vv_dose}, "
                          f"vetprotocol={vp_dose} для "
                          f"{vp_d.get('animal', '?')} — требуется ручная проверка",
                ))
                break

    # vetlek: ищем числа в тексте dosage
    if vl_drug and vl_drug.get("dosage"):
        vl_doses = extract_dose_numbers(vl_drug["dosage"])
        if vl_doses and vv_dose in (None, 0):
            # Берём только реалистичные дозы (≤ 100 мг/кг)
            realistic = [d for d in vl_doses if _is_realistic_dose(d)]
            if realistic:
                suggested = max(realistic)
                severity = "warning"
            else:
                suggested = None
                severity = "info"
            discrepancies.append(Discrepancy(
                drug_id=drug_id, drug_name=drug_name,
                field="dose_per_kg",
                vetvoice_value=vv_dose,
                source_value=max(vl_doses) if vl_doses else None,
                source="vetlek",
                severity=severity,
                suggested_fix=suggested,
                notes=f"vetlek указывает дозу: {vl_drug['dosage'][:200]}",
            ))


def check_side_effects(
    vv_drug: dict, vp_drug: Optional[dict], vl_drug: Optional[dict],
    discrepancies: List[Discrepancy], drug_id: int, drug_name: str,
) -> None:
    """Проверить побочные действия."""
    vv_se = vv_drug.get("side_effects", []) or []

    # vetprotocol: warnings часто содержат побочки
    if vp_drug:
        vp_warnings = vp_drug.get("warnings", []) or []
        vp_all = vp_warnings
        # Ищем слова «побочн» / «возможны» / «может вызывать»
        vp_side_effects = [
            w for w in vp_all
            if re.search(r"(?i)побочн|возможны|может вызывать|вызывает",
                         w)
        ]
        if vp_side_effects and not vv_se:
            discrepancies.append(Discrepancy(
                drug_id=drug_id, drug_name=drug_name,
                field="side_effects",
                vetvoice_value=vv_se,
                source_value=vp_side_effects,
                source="vetprotocol",
                severity="warning",
                suggested_fix=vp_side_effects[:5],
                notes="vetvoice не указывает побочные действия, "
                      "vetprotocol их перечисляет",
            ))

    # vetlek: секция side_effects
    if vl_drug and vl_drug.get("side_effects"):
        vl_se = vl_drug["side_effects"]
        if len(vl_se) > 100 and not vv_se:
            # Извлечь ключевые фразы
            sentences = re.split(r"(?<=[.;])\s+", vl_se)
            sentences = [s.strip() for s in sentences if s.strip()][:5]
            discrepancies.append(Discrepancy(
                drug_id=drug_id, drug_name=drug_name,
                field="side_effects",
                vetvoice_value=vv_se,
                source_value=sentences,
                source="vetlek",
                severity="warning",
                suggested_fix=sentences,
                notes="vetlek описывает побочные действия, "
                      "vetvoice — нет",
            ))


def check_contraindications(
    vv_drug: dict, vp_drug: Optional[dict], vl_drug: Optional[dict],
    discrepancies: List[Discrepancy], drug_id: int, drug_name: str,
) -> None:
    """Проверить противопоказания (беременность, лактация)."""
    vv_c = vv_drug.get("contraindications", {}) or {}
    vv_pregnancy = vv_c.get("pregnancy", False)
    vv_lactation = vv_c.get("lactation", False)

    # vetlek: секция contraindications
    if vl_drug and vl_drug.get("contraindications"):
        text = vl_drug["contraindications"].lower()
        # если vetlek явно говорит про беременность/лактацию
        if "беремен" in text or "стельн" in text or "сукотн" in text:
            if not vv_pregnancy:
                # проверим, что это действительно противопоказание
                if re.search(r"(?i)противопоказан.*беремен|беремен.*противопоказан|"
                             r"запрещ.*беремен|беремен.*запрещ", text):
                    discrepancies.append(Discrepancy(
                        drug_id=drug_id, drug_name=drug_name,
                        field="contraindications.pregnancy",
                        vetvoice_value=vv_pregnancy,
                        source_value=True,
                        source="vetlek",
                        severity="error",
                        suggested_fix=True,
                        notes="vetlek прямо указывает противопоказание при "
                              "беременности, vetvoice — нет",
                    ))
        if "лактаци" in text or "лактин" in text:
            if not vv_lactation:
                if re.search(r"(?i)противопоказан.*лактаци|лактаци.*противопоказан|"
                             r"запрещ.*лактаци|лактаци.*запрещ", text):
                    discrepancies.append(Discrepancy(
                        drug_id=drug_id, drug_name=drug_name,
                        field="contraindications.lactation",
                        vetvoice_value=vv_lactation,
                        source_value=True,
                        source="vetlek",
                        severity="error",
                        suggested_fix=True,
                        notes="vetlek прямо указывает противопоказание при "
                              "лактации, vetvoice — нет",
                    ))

    # vetprotocol: warnings могут содержать упоминания беременности
    if vp_drug:
        for w in vp_drug.get("warnings", []):
            wl = w.lower()
            if ("беремен" in wl or "стельн" in wl or "сукотн" in wl) \
                    and not vv_pregnancy:
                if re.search(r"(?i)противопоказан|запрещ|не\s+примен", w):
                    discrepancies.append(Discrepancy(
                        drug_id=drug_id, drug_name=drug_name,
                        field="contraindications.pregnancy",
                        vetvoice_value=vv_pregnancy,
                        source_value=True,
                        source="vetprotocol",
                        severity="warning",
                        suggested_fix=True,
                        notes=f"vetprotocol предупреждает: {w[:200]}",
                    ))
                    break


def check_withdrawal(
    vv_drug: dict, vl_drug: Optional[dict],
    discrepancies: List[Discrepancy], drug_id: int, drug_name: str,
) -> None:
    """Проверить каренцию (withdrawal days)."""
    vv_wd = vv_drug.get("withdrawal_days")
    if vv_wd in (None, 0) and vl_drug and vl_drug.get("special_notes"):
        text = vl_drug["special_notes"].lower()
        # Ищем «убой на мясо разрешается не ранее чем через N дней»
        m = re.search(
            r"(?i)(?:убой|забой).{0,40}?не\s*ранее.{0,20}?через\s+(\d+)\s+(дн|сут)",
            text,
        )
        if m:
            suggested = int(m.group(1))
            discrepancies.append(Discrepancy(
                drug_id=drug_id, drug_name=drug_name,
                field="withdrawal_days",
                vetvoice_value=vv_wd,
                source_value=suggested,
                source="vetlek",
                severity="warning",
                suggested_fix=suggested,
                notes=f"vetlek указывает каренцию {suggested} дней",
            ))


def check_fsvps(
    vv_drug: dict, fsvps_drug: Optional[dict],
    discrepancies: List[Discrepancy], drug_id: int, drug_name: str,
) -> None:
    """Проверка по госреестру Россельхознадзора (Открытые данные).

    fsvps содержит 2347 препаратов с полными структурированными полями,
    включая иммунобиологические (вакцины, сыворотки) — то, чего нет
    в vetprotocol/vetlek.

    Проверяем:
    1. Совпадение МНН (если различается — warning)
    2. Совпадение лекарственной формы
    3. Заполнение пустых показаний/противопоказаний/побочек из fsvps
    4. Извлечение срока годности / условий хранения
    """
    if not fsvps_drug:
        return

    # 1. МНН
    vv_inn = vv_drug.get("inn", "")
    fsvps_inn = fsvps_drug.get("inn", "")
    if vv_inn and fsvps_inn and vv_inn.lower().strip() != fsvps_inn.lower().strip():
        # МНН различается — возможно, разные формы или ошибка
        # Не предлагаем fix, только warning
        if normalize_name(vv_inn) != normalize_name(fsvps_inn):
            discrepancies.append(Discrepancy(
                drug_id=drug_id, drug_name=drug_name,
                field="inn",
                vetvoice_value=vv_inn,
                source_value=fsvps_inn,
                source="fsvps",
                severity="info",
                suggested_fix=None,
                notes=f"МНН различается: vetvoice='{vv_inn[:50]}', "
                      f"fsvps='{fsvps_inn[:50]}' — возможна ошибка",
            ))

    # 2. Лекарственная форма — если в vetvoice пусто, заполним из fsvps
    vv_form = vv_drug.get("form", "")
    fsvps_form = fsvps_drug.get("form", "")
    if not vv_form and fsvps_form:
        discrepancies.append(Discrepancy(
            drug_id=drug_id, drug_name=drug_name,
            field="form",
            vetvoice_value=vv_form,
            source_value=fsvps_form,
            source="fsvps",
            severity="warning",
            suggested_fix=fsvps_form[:200],
            notes=f"fsvps указывает форму: {fsvps_form[:120]}",
        ))

    # 3. Показания — если в vetvoice пусто
    vv_ind = vv_drug.get("indications", "")
    fsvps_ind = fsvps_drug.get("indications", "")
    if (not vv_ind or vv_ind in ("—", "-", "")) and fsvps_ind:
        discrepancies.append(Discrepancy(
            drug_id=drug_id, drug_name=drug_name,
            field="indications",
            vetvoice_value=vv_ind,
            source_value=fsvps_ind,
            source="fsvps",
            severity="warning",
            suggested_fix=fsvps_ind[:500],
            notes=f"fsvps указывает показания: {fsvps_ind[:120]}",
        ))

    # 4. Побочные действия — если в vetvoice пусто
    vv_se = vv_drug.get("side_effects", []) or []
    fsvps_se = fsvps_drug.get("side_effects", "")
    if not vv_se and fsvps_se and len(fsvps_se) > 20:
        # Разобьём на предложения
        sentences = re.split(r"(?<=[.;])\s+", fsvps_se)
        sentences = [s.strip() for s in sentences if s.strip()][:5]
        discrepancies.append(Discrepancy(
            drug_id=drug_id, drug_name=drug_name,
            field="side_effects",
            vetvoice_value=vv_se,
            source_value=sentences,
            source="fsvps",
            severity="warning",
            suggested_fix=sentences,
            notes=f"fsvps указывает побочные действия",
        ))

    # 5. Противопоказания — проверим беременность/лактацию
    fsvps_contra = fsvps_drug.get("contraindications", "")
    if fsvps_contra:
        text = fsvps_contra.lower()
        vv_c = vv_drug.get("contraindications", {}) or {}
        vv_preg = vv_c.get("pregnancy", False)
        vv_lact = vv_c.get("lactation", False)

        # Если в fsvps явно написано про беременность как противопоказание
        if not vv_preg and re.search(
            r"(?i)противопоказан.*беремен|беремен.*противопоказан|"
            r"запрещ.*беремен|беремен.*запрещ|"
            r"не\s+примен.*беремен|беремен.*не\s+примен",
            text,
        ):
            discrepancies.append(Discrepancy(
                drug_id=drug_id, drug_name=drug_name,
                field="contraindications.pregnancy",
                vetvoice_value=vv_preg,
                source_value=True,
                source="fsvps",
                severity="error",
                suggested_fix=True,
                notes="fsvps (госреестр) прямо указывает противопоказание "
                      "при беременности, vetvoice — нет",
            ))
        if not vv_lact and re.search(
            r"(?i)противопоказан.*лактаци|лактаци.*противопоказан|"
            r"запрещ.*лактаци|лактаци.*запрещ|"
            r"не\s+примен.*лактаци|лактаци.*не\s+примен",
            text,
        ):
            discrepancies.append(Discrepancy(
                drug_id=drug_id, drug_name=drug_name,
                field="contraindications.lactation",
                vetvoice_value=vv_lact,
                source_value=True,
                source="fsvps",
                severity="error",
                suggested_fix=True,
                notes="fsvps (госреестр) прямо указывает противопоказание "
                      "при лактации, vetvoice — нет",
            ))


def check_fsvps_dosage(
    vv_drug: dict, fsvps_drug: Optional[dict],
    discrepancies: List[Discrepancy], drug_id: int, drug_name: str,
) -> None:
    """Извлечь дозировку из поля fsvps.dosage.

    Поле 'dosage' в fsvps содержит текст вроде:
      '2 мл (100 прививных доз)' для вакцин
      '50 мг/мл' для лекарств
      '1000 МЕ/г' для антибиотиков

    Для вакцин — это разовая доза, не мг/кг. Для обычных лекарств —
    концентрация действующего вещества.
    """
    if not fsvps_drug:
        return

    fsvps_dosage = fsvps_drug.get("dosage", "")
    if not fsvps_dosage:
        return

    # Если в vetvoice dose_per_kg пусто, и в fsvps есть концентрация мг/мл
    # — заполним как hint (не fix, потому что это не мг/кг)
    vv_dose = vv_drug.get("dose_per_kg")
    if vv_dose in (None, 0):
        # Ищем мг/кг в fsvps dosage
        m = re.search(
            r"(\d+(?:[.,]\d+)?)\s*(мг|мл|мкг|МЕ|г)\s*/\s*(кг)",
            fsvps_dosage, re.I,
        )
        if m:
            try:
                val = float(m.group(1).replace(",", "."))
                if _is_realistic_dose(val):
                    discrepancies.append(Discrepancy(
                        drug_id=drug_id, drug_name=drug_name,
                        field="dose_per_kg",
                        vetvoice_value=vv_dose,
                        source_value=val,
                        source="fsvps",
                        severity="warning",
                        suggested_fix=val,
                        notes=f"fsvps указывает дозу {val} {m.group(2)}/{m.group(3)}: "
                              f"{fsvps_dosage[:120]}",
                    ))
            except ValueError:
                pass


# ---------------------------------------------------------------------------
# Главный цикл валидации
# ---------------------------------------------------------------------------

def validate(
    drugs_calc_path: str,
    vetprotocol_path: Optional[str] = None,
    vetlek_path: Optional[str] = None,
    vidal_path: Optional[str] = None,
    galen_path: Optional[str] = None,
    fsvps_path: Optional[str] = None,
) -> ValidationReport:
    """Провалидировать drugs_calc.json по источникам."""
    report = ValidationReport()

    # Загрузить vetvoice
    with open(drugs_calc_path, "r", encoding="utf-8") as f:
        vv_data = json.load(f)
    vv_drugs = vv_data.get("drugs_calc", [])
    report.total_drugs = len(vv_drugs)
    log.info("Загружено %d препаратов vetvoice", report.total_drugs)

    # Загрузить источники
    vp_idx, vl_idx, fsvps_idx = {}, {}, {}
    if vetprotocol_path:
        with open(vetprotocol_path, "r", encoding="utf-8") as f:
            vp_data = json.load(f)
        vp_idx = build_vetprotocol_index(vp_data.get("drugs", []))
        report.sources_used.append(f"vetprotocol ({len(vp_idx)} записей)")
        log.info("vetprotocol: %d препаратов", len(vp_data.get("drugs", [])))
    if vetlek_path:
        with open(vetlek_path, "r", encoding="utf-8") as f:
            vl_data = json.load(f)
        vl_idx = build_vetlek_index(vl_data.get("directions", []))
        report.sources_used.append(f"vetlek ({len(vl_idx)} записей)")
        log.info("vetlek: %d инструкций", len(vl_data.get("directions", [])))
    if vidal_path:
        # TODO
        pass
    if galen_path:
        # TODO
        pass
    if fsvps_path:
        with open(fsvps_path, "r", encoding="utf-8") as f:
            fsvps_data = json.load(f)
        fsvps_idx = build_fsvps_index(fsvps_data.get("drugs", []))
        report.sources_used.append(
            f"fsvps ({len(fsvps_data.get('drugs', []))} записей)"
        )
        log.info("fsvps: %d препаратов", len(fsvps_data.get("drugs", [])))

    # Проходим по каждому препарату vetvoice
    for vv in vv_drugs:
        report.checked_drugs += 1
        drug_id = vv.get("id", 0)
        drug_name = vv.get("name", "")
        inn = vv.get("inn", "")

        vp_match = find_in_vetprotocol(vv, vp_idx) if vp_idx else None
        vl_match = find_in_vetlek(vv, vl_idx) if vl_idx else None
        fsvps_match = find_in_fsvps(vv, fsvps_idx) if fsvps_idx else None

        if vp_match or vl_match or fsvps_match:
            report.matched_drugs += 1

        # Проверяем поля
        check_dosage(vv, vp_match, vl_match,
                     report.discrepancies, drug_id, drug_name)
        # ⭐ Видовая разница доз — главное
        check_dosage_by_animal(vv, vp_match, vl_match,
                                report.discrepancies, drug_id, drug_name)
        check_side_effects(vv, vp_match, vl_match,
                           report.discrepancies, drug_id, drug_name)
        check_contraindications(vv, vp_match, vl_match,
                                report.discrepancies, drug_id, drug_name)
        check_withdrawal(vv, vl_match,
                         report.discrepancies, drug_id, drug_name)
        # ⭐ Проверка по госреестру FSVPS (включая вакцины!)
        if fsvps_match:
            check_fsvps(vv, fsvps_match,
                        report.discrepancies, drug_id, drug_name)
            check_fsvps_dosage(vv, fsvps_match,
                               report.discrepancies, drug_id, drug_name)

    log.info(
        "Валидация завершена: проверено %d, совпало %d, расхождений %d",
        report.checked_drugs, report.matched_drugs, len(report.discrepancies),
    )
    return report


# ---------------------------------------------------------------------------
# Применение исправлений
# ---------------------------------------------------------------------------

def apply_fixes(
    drugs_calc_path: str,
    report: ValidationReport,
    output_path: str,
    severity_filter: Tuple[str, ...] = ("error", "warning"),
    aggressive: bool = False,
) -> int:
    """Применить предложенные исправления к drugs_calc.json.

    Возвращает количество применённых исправлений.

    Args:
        aggressive: если True — перезаписывает существующие значения,
                    если они кажутся кривыми (нереалистичные дозы,
                    расхождения >50%). По умолчанию False (только
                    безопасные исправления пустых полей).
    """
    with open(drugs_calc_path, "r", encoding="utf-8") as f:
        vv_data = json.load(f)
    vv_drugs = vv_data.get("drugs_calc", [])
    by_id = {d.get("id"): d for d in vv_drugs}

    fixes_applied = 0
    fixed_drug_ids = set()
    auto_corrections_log: List[str] = []  # детальный лог для metadata

    def _log_change(drug_id: int, drug_name: str, field: str,
                    old_val, new_val, source: str, reason: str) -> None:
        """Записать изменение в лог metadata.corrections."""
        auto_corrections_log.append(
            f"#{drug_id} {drug_name[:30]}: {field} "
            f"{old_val!r} -> {new_val!r} "
            f"(src={source}, reason={reason})"
        )

    for d in report.discrepancies:
        if d.severity not in severity_filter:
            continue
        if d.suggested_fix is None:
            # В aggressive режиме: для dose_per_kg без suggested_fix
            # (расхождение существующего значения) — перезаписать
            if aggressive and d.field == "dose_per_kg":
                drug = by_id.get(d.drug_id)
                if not drug:
                    continue
                vv_dose = drug.get("dose_per_kg")
                source_dose = d.source_value
                if vv_dose and source_dose:
                    # Проверяем, безопасно ли перезаписать
                    if not _is_safe_to_overwrite(vv_dose, source_dose):
                        continue  # различие > 10x — пропускаем (false positive)
                    # Если в vetvoice доза нереалистичная — заменить
                    if not _is_realistic_dose(vv_dose):
                        drug["dose_per_kg"] = source_dose
                        drug["dose_min"] = round(source_dose * 0.8, 2)
                        drug["dose_max"] = round(source_dose * 1.2, 2)
                        fixes_applied += 1
                        fixed_drug_ids.add(d.drug_id)
                        _log_change(d.drug_id, d.drug_name, "dose_per_kg",
                                    vv_dose, source_dose, d.source,
                                    "vetvoice dose unrealistic")
                    # Если расхождение >50% (но <10x) — доверяем источнику
                    elif not dose_values_close(vv_dose, source_dose, 0.5):
                        drug["dose_per_kg"] = source_dose
                        drug["dose_min"] = round(source_dose * 0.8, 2)
                        drug["dose_max"] = round(source_dose * 1.2, 2)
                        fixes_applied += 1
                        fixed_drug_ids.add(d.drug_id)
                        _log_change(d.drug_id, d.drug_name, "dose_per_kg",
                                    vv_dose, source_dose, d.source,
                                    f"divergence >50% ({abs(vv_dose-source_dose)/max(vv_dose,source_dose)*100:.0f}%)")
            continue

        drug = by_id.get(d.drug_id)
        if not drug:
            continue

        field_path = d.field.split(".")
        changed = False

        if len(field_path) == 1:
            f_name = field_path[0]
            if f_name == "dose_per_kg":
                vv_dose = drug.get("dose_per_kg")
                new_dose = d.suggested_fix
                # Безопасный режим: только если пусто
                # Aggressive: также если существующая нереалистична или
                #             отличается >50% (но не более 10x)
                should_apply = False
                reason = ""
                if vv_dose in (None, 0):
                    should_apply = True
                    reason = "was empty"
                elif aggressive:
                    if not _is_safe_to_overwrite(vv_dose, new_dose):
                        pass  # пропускаем (различие > 10x — false positive)
                    elif not _is_realistic_dose(vv_dose):
                        should_apply = True
                        reason = f"was unrealistic ({vv_dose})"
                    elif not dose_values_close(vv_dose, new_dose, 0.5):
                        should_apply = True
                        pct = abs(vv_dose - new_dose) / max(vv_dose, new_dose) * 100
                        reason = f"divergence {pct:.0f}%"

                if should_apply and _is_realistic_dose(new_dose):
                    drug["dose_per_kg"] = new_dose
                    drug["dose_min"] = round(new_dose * 0.8, 2)
                    drug["dose_max"] = round(new_dose * 1.2, 2)
                    changed = True
                    _log_change(d.drug_id, d.drug_name, "dose_per_kg",
                                vv_dose, new_dose, d.source, reason)
            elif f_name == "side_effects":
                if (not drug.get("side_effects") or
                    (aggressive and isinstance(d.suggested_fix, list)
                     and len(d.suggested_fix) > len(drug.get("side_effects") or []))):
                    drug["side_effects"] = d.suggested_fix
                    changed = True
            elif f_name == "withdrawal_days":
                if drug.get("withdrawal_days") in (None, 0):
                    drug["withdrawal_days"] = d.suggested_fix
                    changed = True
        elif len(field_path) == 2 and field_path[0] == "contraindications":
            if not drug.get("contraindications") or not isinstance(
                drug.get("contraindications"), dict
            ):
                drug["contraindications"] = {
                    "warnings": [],
                    "pregnancy": False,
                    "lactation": False,
                    "young": False,
                    "old": False,
                }
            c = drug["contraindications"]
            # Нормализуем warnings в список (в исходных данных бывает строка)
            w = c.get("warnings")
            if isinstance(w, str):
                # Попытка распарсить как JSON-сериализованный список
                import json as _json
                try:
                    parsed = _json.loads(w.replace("'", '"'))
                    if isinstance(parsed, list):
                        c["warnings"] = [str(x) for x in parsed]
                    else:
                        c["warnings"] = [w] if w else []
                except Exception:
                    # Не JSON — кладём всю строку как один элемент
                    c["warnings"] = [w] if w else []
            elif w is None:
                c["warnings"] = []
            elif not isinstance(w, list):
                c["warnings"] = [str(w)]
            sub = field_path[1]
            if sub in ("pregnancy", "lactation", "young", "old"):
                if not c.get(sub):
                    c[sub] = True
                    # Добавим warning
                    warn_text = {
                        "pregnancy": "Противопоказан при беременности.",
                        "lactation": "Противопоказан в период лактации.",
                        "young":     "Противопоказан молодым животным.",
                        "old":       "Противопоказан пожилым животным.",
                    }[sub]
                    if warn_text not in c.get("warnings", []):
                        c.setdefault("warnings", []).append(warn_text)
                    changed = True
        elif len(field_path) == 2 and field_path[0] == "animal_specific":
            # ⭐ Добавление нового животного в animal_specific
            # suggested_fix = {"animal": "Собаки", "data": {...}}
            animal = field_path[1]
            if isinstance(d.suggested_fix, dict) and "animal" in d.suggested_fix:
                animal = d.suggested_fix["animal"]
                data = d.suggested_fix.get("data", {})
            else:
                data = d.suggested_fix if isinstance(d.suggested_fix, dict) else {}
            if not drug.get("animal_specific"):
                drug["animal_specific"] = {}
            if animal not in drug["animal_specific"]:
                drug["animal_specific"][animal] = data
                changed = True
                _log_change(d.drug_id, d.drug_name, f"animal_specific.{animal}",
                            None, data, d.source, "added new animal")
        elif (len(field_path) == 3 and field_path[0] == "animal_specific"
              and field_path[2] == "dose_per_kg"):
            # ⭐ Заполняем/перезаписываем дозу внутри animal_specific[animal]
            animal = field_path[1]
            if not drug.get("animal_specific"):
                drug["animal_specific"] = {}
            spec = drug["animal_specific"].get(animal) or {}
            old_dose = spec.get("dose_per_kg")
            new_dose = (d.suggested_fix.get("dose_per_kg")
                        if isinstance(d.suggested_fix, dict)
                        else d.suggested_fix)
            should_apply = False
            reason = ""
            if old_dose in (None, 0):
                should_apply = True
                reason = "was empty"
            elif aggressive and new_dose and _is_realistic_dose(new_dose):
                if not _is_safe_to_overwrite(old_dose, new_dose):
                    pass  # различие > 10x — пропускаем
                elif not _is_realistic_dose(old_dose):
                    should_apply = True
                    reason = f"was unrealistic ({old_dose})"
                elif not dose_values_close(old_dose, new_dose, 0.5):
                    should_apply = True
                    pct = abs(old_dose - new_dose) / max(old_dose, new_dose) * 100
                    reason = f"divergence {pct:.0f}%"

            if should_apply:
                if isinstance(d.suggested_fix, dict):
                    for k, v in d.suggested_fix.items():
                        if v:
                            spec[k] = v
                else:
                    spec["dose_per_kg"] = d.suggested_fix
                drug["animal_specific"][animal] = spec
                changed = True
                _log_change(d.drug_id, d.drug_name,
                            f"animal_specific.{animal}.dose_per_kg",
                            old_dose, new_dose, d.source, reason)

        if changed:
            fixes_applied += 1
            fixed_drug_ids.add(d.drug_id)

    # Обновим версию и метаданные
    vv_data["version"] = str(
        float(vv_data.get("version", "1.0")) + 0.1
    )
    vv_data["last_updated"] = "2026-08-13"
    meta = vv_data.setdefault("metadata", {})
    corrections = meta.setdefault("corrections", [])
    mode_label = "aggressive" if aggressive else "safe"
    corrections.append(
        f"validate_vetvoice.py ({mode_label} mode): применено "
        f"{fixes_applied} исправлений в {len(fixed_drug_ids)} препаратах "
        f"на основе vetprotocol/vetlek"
    )
    # В aggressive режиме добавляем детальный лог
    if aggressive and auto_corrections_log:
        corrections.append(
            f"validate_vetvoice.py: детальный лог изменений "
            f"({len(auto_corrections_log)} записей):"
        )
        # Добавляем первые 200 записей (чтобы не раздуло JSON)
        for entry in auto_corrections_log[:200]:
            corrections.append(f"  - {entry}")
        if len(auto_corrections_log) > 200:
            corrections.append(
                f"  ... и ещё {len(auto_corrections_log) - 200} записей "
                f"(см. validation_report.json)"
            )
    vv_data["total_drugs"] = len(vv_drugs)

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(vv_data, f, ensure_ascii=False, indent=2)

    report.fixed_drugs = len(fixed_drug_ids)
    log.info(
        "Применено %d исправлений (mode=%s), затронуто %d препаратов, "
        "сохранено в %s",
        fixes_applied, "aggressive" if aggressive else "safe",
        len(fixed_drug_ids), output_path,
    )
    return fixes_applied


# ---------------------------------------------------------------------------
# Экспорт спорных расхождений в CSV для ручной проверки
# ---------------------------------------------------------------------------

def export_discrepancies_csv(
    report: ValidationReport,
    output_path: str,
    severity_filter: Tuple[str, ...] = ("error", "warning", "info"),
) -> int:
    """Экспортировать все расхождения в CSV для ручной проверки.

    Возвращает количество строк.
    """
    import csv

    rows = 0
    with open(output_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            "drug_id", "drug_name", "field", "severity", "source",
            "vetvoice_value", "source_value", "suggested_fix",
            "auto_applied", "notes",
        ])
        for d in report.discrepancies:
            if d.severity not in severity_filter:
                continue
            writer.writerow([
                d.drug_id,
                d.drug_name,
                d.field,
                d.severity,
                d.source,
                json.dumps(d.vetvoice_value, ensure_ascii=False)
                    if d.vetvoice_value is not None else "",
                json.dumps(d.source_value, ensure_ascii=False)
                    if d.source_value is not None else "",
                json.dumps(d.suggested_fix, ensure_ascii=False)
                    if d.suggested_fix is not None else "",
                "yes" if d.suggested_fix is not None else "review_needed",
                d.notes[:500],
            ])
            rows += 1
    log.info("CSV-отчёт (%d строк) сохранён в %s", rows, output_path)
    return rows


def export_review_markdown(
    report: ValidationReport,
    output_path: str,
    vv_data_path: Optional[str] = None,
) -> int:
    """Экспортировать человекочитаемый Markdown-отчёт для ручной проверки.

    Группирует расхождения по drug_id, показывает все проблемы одного
    препарата вместе. Удобно для пошагового разбора.
    """
    by_drug: Dict[int, List[Discrepancy]] = {}
    for d in report.discrepancies:
        by_drug.setdefault(d.drug_id, []).append(d)

    # Загружаем vetvoice для контекста
    vv_drugs: Dict[int, dict] = {}
    if vv_data_path:
        try:
            with open(vv_data_path, "r", encoding="utf-8") as f:
                vv = json.load(f)
            vv_drugs = {d.get("id"): d for d in vv.get("drugs_calc", [])}
        except Exception:
            pass

    with open(output_path, "w", encoding="utf-8") as f:
        f.write("# VetVoice — Отчёт для ручной проверки\n\n")
        f.write(f"**Всего препаратов с расхождениями:** {len(by_drug)}\n\n")
        f.write(f"**Всего расхождений:** {len(report.discrepancies)}\n\n")
        f.write("## Статистика по severity\n\n")
        for sev in ("error", "warning", "info"):
            n = sum(1 for d in report.discrepancies if d.severity == sev)
            f.write(f"- **{sev}**: {n}\n")
        f.write("\n---\n\n")

        # Сортируем: сначала error, потом по drug_id
        def _sort_key(item):
            did, disc_list = item
            min_sev = min(
                ({"error": 0, "warning": 1, "info": 2}[d.severity]
                 for d in disc_list),
                default=3,
            )
            return (min_sev, did)

        for did, disc_list in sorted(by_drug.items(), key=_sort_key):
            drug = vv_drugs.get(did, {})
            name = disc_list[0].drug_name
            f.write(f"## #{did} {name}\n\n")
            if drug:
                f.write(f"- **Категория:** {drug.get('category', '?')}\n")
                f.write(f"- **МНН:** {drug.get('inn', '?')}\n")
                f.write(f"- **Животные:** {', '.join(drug.get('animals', []))}\n")
                f.write(f"- **Текущая доза:** {drug.get('dose_per_kg', '?')} "
                        f"{drug.get('dose_unit', '')}\n")
                if drug.get("animal_specific"):
                    f.write("- **Видовые дозы:**\n")
                    for animal, spec in drug["animal_specific"].items():
                        d_pk = spec.get("dose_per_kg", "?")
                        f.write(f"  - {animal}: {d_pk} "
                                f"{spec.get('dose_unit', '')} "
                                f"({spec.get('method', '?')})\n")
                f.write("\n")
            f.write("### Расхождения\n\n")
            f.write("| Поле | Severity | Источник | VetVoice | Источник | "
                    "Действие |\n")
            f.write("|------|----------|----------|----------|-----------|"
                    "----------|\n")
            for d in disc_list:
                vv_s = str(d.vetvoice_value)[:60] if d.vetvoice_value is not None else "—"
                src_s = str(d.source_value)[:60] if d.source_value is not None else "—"
                action = ("✅ auto" if d.suggested_fix is not None
                          else "🔍 ручная")
                f.write(f"| {d.field} | {d.severity} | {d.source} | "
                        f"{vv_s} | {src_s} | {action} |\n")
            f.write("\n")
            # notes первого расхождения
            f.write(f"**Заметки:** {disc_list[0].notes[:300]}\n\n")
            f.write("---\n\n")

    log.info("Markdown-отчёт (%d препаратов) сохранён в %s",
             len(by_drug), output_path)
    return len(by_drug)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    p = argparse.ArgumentParser(description="Валидатор vetvoice drugs_calc.json")
    p.add_argument("--drugs-calc", required=True, help="Путь к drugs_calc.json")
    p.add_argument("--vetprotocol", help="Путь к vetprotocol.json")
    p.add_argument("--vetlek", help="Путь к vetlek.json")
    p.add_argument("--vidal", help="Путь к vidal.json")
    p.add_argument("--galen", help="Путь к galen.json")
    p.add_argument("--fsvps", help="Путь к fsvps.json (Открытые данные Россельхознадзора)")
    p.add_argument("--output", default="validation_report.json",
                   help="Куда сохранить отчёт")
    p.add_argument("--apply-fixes", metavar="OUTPUT_JSON",
                   help="Применить исправления и сохранить в указанный файл")
    p.add_argument("--aggressive", action="store_true",
                   help="Aggressive mode: перезаписывать существующие "
                        "кривые дозы (нереалистичные или расхождение >50%). "
                        "По умолчанию применяются только безопасные fix "
                        "(заполнение пустых полей).")
    p.add_argument("--export-csv", metavar="OUTPUT_CSV",
                   help="Экспорт всех расхождений в CSV для ручной проверки")
    p.add_argument("--export-md", metavar="OUTPUT_MD",
                   help="Экспорт человекочитаемого Markdown-отчёта "
                        "для ручной проверки")
    p.add_argument("--severity", default="error,warning",
                   help="Фильтр severity для apply-fixes (через запятую)")
    args = p.parse_args()

    report = validate(
        drugs_calc_path=args.drugs_calc,
        vetprotocol_path=args.vetprotocol,
        vetlek_path=args.vetlek,
        vidal_path=args.vidal,
        galen_path=args.galen,
        fsvps_path=args.fsvps,
    )

    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(report.to_dict(), f, ensure_ascii=False, indent=2)
    log.info("Отчёт сохранён в %s", args.output)

    # Краткий print-вывод
    print()
    print("=" * 60)
    print(f"VetVoice Validation Report{' (AGGRESSIVE MODE)' if args.aggressive else ''}")
    print("=" * 60)
    print(f"Total drugs:        {report.total_drugs}")
    print(f"Checked:            {report.checked_drugs}")
    print(f"Matched:            {report.matched_drugs}")
    print(f"Discrepancies:      {len(report.discrepancies)}")
    by_sev = report.to_dict()["discrepancies_by_severity"]
    for sev, cnt in by_sev.items():
        print(f"  {sev:10s}:      {cnt}")
    print()
    print("Top fields:")
    for f, cnt in list(report.to_dict()["discrepancies_by_field"].items())[:10]:
        print(f"  {f:40s}: {cnt}")
    print()

    # CSV экспорт
    if args.export_csv:
        n = export_discrepancies_csv(report, args.export_csv)
        print(f"CSV-отчёт: {n} строк -> {args.export_csv}")

    # Markdown экспорт
    if args.export_md:
        n = export_review_markdown(report, args.export_md,
                                    vv_data_path=args.drugs_calc)
        print(f"Markdown-отчёт: {n} препаратов -> {args.export_md}")

    if args.apply_fixes:
        severity = tuple(s.strip() for s in args.severity.split(","))
        n = apply_fixes(args.drugs_calc, report, args.apply_fixes,
                        severity, aggressive=args.aggressive)
        mode = "AGGRESSIVE" if args.aggressive else "safe"
        print(f"Режим: {mode}")
        print(f"Применено исправлений: {n}")
        print(f"Исправлено препаратов: {report.fixed_drugs}")
        print(f"Исправленный файл:    {args.apply_fixes}")
        # Пересохраняем отчёт — теперь с правильным fixed_drugs
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(report.to_dict(), f, ensure_ascii=False, indent=2)
        log.info("Отчёт обновлён (fixed_drugs=%d) и сохранён в %s",
                 report.fixed_drugs, args.output)


if __name__ == "__main__":
    main()
