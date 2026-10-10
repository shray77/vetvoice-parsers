"""
Vaccine Extractor — отдельная модель для вакцин и иммунобиологических препаратов.

Контекст:
  В оригинальной базе drugs_calc.json вакцины помечены как form_type='injection'
  или 'powder', но их дозировка — это не мг/кг, а:
    - разовая доза (например «1 мл подкожно»)
    - количество прививных доз во флаконе (например «100 прививных доз»)
    - схема вакцинации (например «ревакцинация через 21 день»)

  Если такие препараты прогонять через обычный калькулятор
  (dose_per_kg × weight) — пользователь получит абсурдный результат
  (например «20 мл» для собаки, хотя нужно «1 мл»).

Решение:
  1. Распознать вакцины по category='Иммунобиологические' или form_type='vaccine'
  2. Распарсить fsvps.dosage — там структурированная строка:
       "2 мл (100 прививных доз)"
       "4 см³ (2000, 4000, 5000 прививных доз)"
       "1 мл (1 доза)"
  3. Сложить в новое поле vaccine_specific в drugs_calc.json:
       {
         "form_type": "vaccine",
         "calculator_applicable": false,   // не считаем через мг/кг!
         "vaccine_specific": {
           "single_dose_ml": 1.0,           // разовая доза в мл
           "doses_per_vial": 100,           // доз во флаконе
           "doses_per_vial_options": [100, 500, 1000],  // варианты фасовки
           "route": "подкожно",             // путь введения
           "schedule": "1 доза, ревакцинация через 21 день",
           "animal": "Собаки"               // для кого
         }
       }
  4. В UI показать карточку вакцины отдельно — без поля ввода веса,
     с кнопкой «Рассчитать количество флаконов».

Использование:
  python vaccine_extractor.py \
    --drugs-calc path/to/drugs_calc.json \
    --fsvps path/to/fsvps.json \
    --output path/to/drugs_calc_with_vaccines.json
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Подключаем config
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_PARENT_DIR = os.path.dirname(_SCRIPT_DIR)
if _PARENT_DIR not in sys.path:
    sys.path.insert(0, _PARENT_DIR)

log = logging.getLogger("vaccine_extractor")
if not log.handlers:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")


# ---------------------------------------------------------------------------
# Модель
# ---------------------------------------------------------------------------

@dataclass
class VaccineSpecific:
    """Специфичные для вакцин поля."""
    single_dose_ml: Optional[float] = None       # разовая доза, мл
    single_dose_text: str = ""                   # исходный текст разовой дозы
    doses_per_vial: Optional[int] = None         # доз во флаконе (если одна фасовка)
    doses_per_vial_options: List[int] = field(default_factory=list)  # варианты фасовки
    route: str = ""                              # путь введения (подкожно, внутримышечно)
    schedule: str = ""                           # схема вакцинации
    animal: str = ""                             # для кого (если указано)
    vaccine_type: str = ""                       # живая/инактивированная/рекомбинантная
    notes: str = ""                              # доп. заметки

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# Распознавание вакцин
# ---------------------------------------------------------------------------

# Категории, которые считаем вакцинами/иммунобиологическими
VACCINE_CATEGORIES = {
    "Иммунобиологические",
    "Вакцины",
    "Иммуномодуляторы",  # часть из них — вакцины
}

# Слова в МНН/названии, по которым узнаём вакцины
VACCINE_KEYWORDS = [
    "вакцин", "сыворотк", "иммуноглобulin", "анатоксин",
    "бактериофаг", "фаг", "интерферон",
    "иммунокомплекс", "живая", "инактивированная",
    "рекомбинант", "субъединич", "лиофилизат",
]


def is_vaccine(drug: dict) -> bool:
    """Определить, является ли препарат вакциной/иммунобиологическим."""
    # По category
    cat = (drug.get("category") or "").strip()
    if cat in VACCINE_CATEGORIES:
        return True
    # По form_type
    if drug.get("form_type") == "vaccine":
        return True
    # По МНН/названию
    text = ((drug.get("inn") or "") + " " + (drug.get("name") or "")).lower()
    if any(kw in text for kw in VACCINE_KEYWORDS):
        return True
    return False


# ---------------------------------------------------------------------------
# Парсинг fsvps.dosage
# ---------------------------------------------------------------------------

# Паттерны дозировок вакцин:
#   "2 мл (100 прививных доз)"
#   "4 см³ (2000, 4000, 5000 прививных доз)"
#   "1 мл (1 доза)"
#   "0.5 мл (1 доза) — подкожно"
#   "2,0 см3 (100-5000 прививных доз)"
#   "10 мл (10 доз) и 50 мл (50 доз)"

# Разовая доза — число перед "мл" или "см³"/"см3"/"см?" (битая кодировка)
_SINGLE_DOSE_RE = re.compile(
    r"(\d+[.,]?\d*)\s*(мл|см[³3?])",
    re.I,
)

# Количество доз во флаконе
_DOSES_PER_VIAL_RE = re.compile(
    r"\(([^)]*?доз[^)]*?)\)",
    re.I,
)

# Путь введения
_ROUTE_PATTERNS = [
    (r"\bподкожно\b|\bп/?к\b", "подкожно"),
    (r"\bвнутримышечно\b|\bв/?м\b", "внутримышечно"),
    (r"\bвнутривенно\b|\bв/?в\b", "внутривенно"),
    (r"\bпероральн|\bвнутрь\b", "перорально"),
    (r"\bинтраназальн", "интраназально"),
    (r"\bконъюнктивальн", "конъюнктивально"),
    (r"\bаэрозольн|\bспрей", "аэрозольно"),
    (r"\bнакожно|\bнаружно", "накожно"),
]

# Схема вакцинации (когда указана)
_SCHEDULE_PATTERNS = [
    r"ревакцинаци\w*\s+(?:через\s+)?(\d+\s*(?:дн\w*|сут\w*|нед\w*|мес\w*))",
    r"повторн\w*\s+(?:через\s+)?(\d+\s*(?:дн\w*|сут\w*|нед\w*|мес\w*))",
    r"втор\w*\s+введен\w*\s+(?:через\s+)?(\d+\s*(?:дн\w*|сут\w*|нед\w*|мес\w*))",
    r"двукратн\w*.*?интервал\w*\s+(\d+\s*(?:дн\w*|сут\w*|нед\w*|мес\w*))",
    r"тр[её]хкратн\w*.*?интервал\w*\s+(\d+\s*(?:дн\w*|сут\w*|нед\w*|мес\w*))",
]

# Тип вакцины
_VACCINE_TYPE_PATTERNS = [
    (r"живая\s+(?:сухая\s+)?(?:вакцина|лиофилизат)", "живая"),
    (r"инактивированн\w*\s+(?:вакцина|суспенз\w*)", "инактивированная"),
    (r"рекомбинант\w*", "рекомбинантная"),
    (r"субъединич\w*", "субъединичная"),
    (r"анатоксин", "анатоксин"),
    (r"сыворотка", "сыворотка"),
    (r"иммуноглобulin", "иммуноглобулин"),
    (r"бактериофаг", "бактериофаг"),
]


def parse_vaccine_dosage(dosage_text: str) -> VaccineSpecific:
    """Распарсить поле fsvps.dosage для вакцины.

    Args:
        dosage_text: строка вроде "2 мл (100 прививных доз)"

    Returns:
        VaccineSpecific с извлечёнными полями.
    """
    if not dosage_text:
        return VaccineSpecific()

    result = VaccineSpecific()
    result.single_dose_text = dosage_text.strip()

    # 1. Разовая доза (мл)
    m = _SINGLE_DOSE_RE.search(dosage_text)
    if m:
        try:
            val = float(m.group(1).replace(",", "."))
            if 0 < val <= 100:  # реалистичная разовая доза 0.1-50 мл
                result.single_dose_ml = val
        except ValueError:
            pass

    # 2. Количество доз во флаконе
    m = _DOSES_PER_VIAL_RE.search(dosage_text)
    if m:
        inner = m.group(1)
        # Может быть "100", "2000, 4000, 5000", "100-5000"
        # Ищем все числа
        numbers = [int(x) for x in re.findall(r"\d+", inner)]
        # Фильтруем разумные (1-100000)
        numbers = [n for n in numbers if 1 <= n <= 100000]
        if numbers:
            if len(numbers) == 1:
                result.doses_per_vial = numbers[0]
            elif len(numbers) == 2 and "-" in inner:
                # диапазон "100-5000" — берём как варианты
                result.doses_per_vial_options = numbers
                result.doses_per_vial = max(numbers)
            else:
                # список "2000, 4000, 5000"
                result.doses_per_vial_options = sorted(set(numbers))
                result.doses_per_vial = max(numbers)

    # 3. Путь введения
    for pattern, route in _ROUTE_PATTERNS:
        if re.search(pattern, dosage_text, re.I):
            result.route = route
            break

    # 4. Схема вакцинации
    for pattern in _SCHEDULE_PATTERNS:
        m = re.search(pattern, dosage_text, re.I)
        if m:
            result.schedule = f"Повтор через {m.group(1)}"
            break

    # 5. Тип вакцины — попытка определить из МНН или формы
    # (это будет сделано позже из inn/form, не из dosage)

    return result


def determine_vaccine_type(drug: dict) -> str:
    """Определить тип вакцины (живая, инактивированная, и т.д.) из МНН/формы."""
    text = ((drug.get("inn") or "") + " " + (drug.get("form") or "")).lower()
    for pattern, vtype in _VACCINE_TYPE_PATTERNS:
        if re.search(pattern, text, re.I):
            return vtype
    return ""


# ---------------------------------------------------------------------------
# Главный pipeline
# ---------------------------------------------------------------------------

def extract_vaccines(
    drugs_calc_path: str,
    fsvps_path: str,
    output_path: str,
    dry_run: bool = False,
) -> Tuple[int, int]:
    """Распознать вакцины в drugs_calc.json и заполнить vaccine_specific.

    Returns:
        (total_vaccines_found, vaccines_with_dosage_parsed)
    """
    with open(drugs_calc_path, "r", encoding="utf-8") as f:
        vv_data = json.load(f)
    with open(fsvps_path, "r", encoding="utf-8") as f:
        fsvps_data = json.load(f)

    # Индекс fsvps по нормализованному названию
    def _norm(s):
        s = (s or "").lower().strip()
        s = re.sub(r"[®™]", "", s)
        s = re.sub(r"[^\w\s/-]", " ", s)
        s = re.sub(r"\s+", " ", s).strip()
        return s

    fsvps_by_name = {_norm(d["trade_name"]): d for d in fsvps_data.get("drugs", [])}

    drugs = vv_data.get("drugs_calc", [])
    total_vaccines = 0
    parsed_ok = 0

    for drug in drugs:
        if not is_vaccine(drug):
            continue
        total_vaccines += 1

        # Найдём соответствие в fsvps
        name_key = _norm(drug.get("name", ""))
        fsvps_match = fsvps_by_name.get(name_key)
        if not fsvps_match:
            # Fuzzy: подстрока
            for k, v in fsvps_by_name.items():
                if name_key and len(name_key) > 4 and (name_key in k or k in name_key):
                    fsvps_match = v
                    break

        # Если есть match по fsvps — парсим dosage
        if fsvps_match and fsvps_match.get("dosage"):
            vs = parse_vaccine_dosage(fsvps_match["dosage"])
            # Дополним типом из МНН
            vtype = determine_vaccine_type(drug) or determine_vaccine_type(fsvps_match)
            if vtype:
                vs.vaccine_type = vtype
            # Заполняем в drug
            if vs.single_dose_ml or vs.doses_per_vial:
                parsed_ok += 1

            if not dry_run:
                drug["vaccine_specific"] = vs.to_dict()
                drug["form_type"] = "vaccine"
                drug["calculator_applicable"] = False

    # Метаданные
    if not dry_run:
        vv_data["version"] = str(float(vv_data.get("version", "1.0")) + 0.1)
        vv_data["last_updated"] = "2026-08-13"
        meta = vv_data.setdefault("metadata", {})
        corrections = meta.setdefault("corrections", [])
        corrections.append(
            f"vaccine_extractor.py: распознано {total_vaccines} вакцин, "
            f"из них для {parsed_ok} извлечена разовая доза и фасовка из fsvps. "
            f"form_type изменён на 'vaccine', calculator_applicable=false."
        )

        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(vv_data, f, ensure_ascii=False, indent=2)

    log.info(
        "Найдено вакцин: %d, успешно распарсено дозировка: %d (%.0f%%)",
        total_vaccines, parsed_ok,
        parsed_ok * 100 / total_vaccines if total_vaccines else 0,
    )
    return total_vaccines, parsed_ok


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    p = argparse.ArgumentParser(
        description="Vaccine Extractor — отдельная модель для вакцин"
    )
    p.add_argument("--drugs-calc", required=True,
                   help="Путь к drugs_calc.json")
    p.add_argument("--fsvps", required=True,
                   help="Путь к fsvps.json (Открытые данные Россельхознадзора)")
    p.add_argument("--output", required=True,
                   help="Куда сохранить drugs_calc с vaccine_specific")
    p.add_argument("--dry-run", action="store_true",
                   help="Только статистика, без записи файла")
    args = p.parse_args()

    extract_vaccines(args.drugs_calc, args.fsvps, args.output, args.dry_run)
