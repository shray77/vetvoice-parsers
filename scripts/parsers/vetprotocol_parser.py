"""
Парсер vetprotocol.ru — открытый ветеринарный справочник препаратов.

Структура сайта:
  * https://vetprotocol.ru/drug/        — список всех препаратов (по МНН)
  * https://vetprotocol.ru/drug/{slug}  — страница конкретного препарата

На каждой странице препарата есть:
  * Название + синонимы (МНН и торговые)
  * Показания
  * Дозировки (по видам животных, мг/кг, кратность, путь введения)
  * Предупреждения (противопоказания, побочки) — помечены «(!)»

Этот парсер извлекает структурированные данные и приводит их к формату,
совместимому с drugs_calc.json из репозитория vetvoice.

Конфигурация: см. config.yaml (секция vetprotocol).
Все настройки можно переопределить через env vars:
    VETVOICE_VETPROTOCOL_DELAY=2.0
    VETVOICE_GLOBAL_USER_AGENT="MyBot/1.0"
"""

from __future__ import annotations

import logging
import os
import re
import sys
import time
from dataclasses import dataclass, field, asdict
from typing import Iterator, List, Optional, Dict
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

# Подключаем общий модуль конфигурации
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_PARENT_DIR = os.path.dirname(_SCRIPT_DIR)  # scripts/
if _PARENT_DIR not in sys.path:
    sys.path.insert(0, _PARENT_DIR)
from config import get_config, make_session, can_fetch, RateLimiter  # noqa: E402

log = logging.getLogger("vetprotocol_parser")


# Дефолты (если config.yaml не найден)
BASE_URL = "https://vetprotocol.ru"
DRUG_LIST_URL = f"{BASE_URL}/drug/"
DEFAULT_TIMEOUT = 30
DEFAULT_DELAY = 0.6


# ---------------------------------------------------------------------------
# Модели данных
# ---------------------------------------------------------------------------

@dataclass
class VetprotocolDose:
    """Дозировка для конкретного вида животных."""
    animal: str = ""           # Собаки, Кошки, КРС, МРС, ...
    dose_text: str = ""        # полный текст дозы
    dose_per_kg: Optional[float] = None
    dose_unit: str = ""        # мг/кг
    frequency: str = ""        # каждые 12 ч
    route: str = ""            # перорально, внутримышечно, ...
    course_days: str = ""      # 3 дней, 10-14 дней
    indication: str = ""       # для чего (Giardia, респираторные паразиты, ...)


@dataclass
class VetprotocolDrug:
    """Препарат с vetprotocol.ru."""
    slug: str = ""
    name: str = ""
    synonyms: List[str] = field(default_factory=list)
    indications: List[str] = field(default_factory=list)
    doses: List[VetprotocolDose] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)  # помечены «(!)»
    source_url: str = ""
    raw_text: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("raw_text", None)
        return d


# ---------------------------------------------------------------------------
# HTTP-сессия
# ---------------------------------------------------------------------------

class VetprotocolClient:
    def __init__(
        self,
        delay: Optional[float] = None,
        config=None,
    ):
        # Загрузить конфиг
        self.cfg = config or get_config()
        vp_cfg = self.cfg.vetprotocol

        # Параметры сессии
        self.session = make_session(self.cfg, "vetprotocol")
        self.base_url = vp_cfg.base_url
        self.list_url = vp_cfg.list_url
        self.delay = delay if delay is not None else vp_cfg.delay
        self.timeout = self.cfg.global_config.timeout
        self.on_429_wait = vp_cfg.on_429_wait_seconds
        self.rate_limiter = RateLimiter(self.delay)
        self._stopped = False  # флаг «нас попросили остановиться»

    def _get(self, url: str) -> Optional[str]:
        # Проверить robots.txt
        if not can_fetch(self.cfg, self.session, url):
            log.warning("robots.txt запрещает: %s — пропускаю", url)
            return None
        # Rate limit
        self.rate_limiter.wait()
        try:
            r = self.session.get(url, timeout=self.timeout)
            # 429 Too Many Requests — подождать и продолжить
            if r.status_code == 429:
                log.warning(
                    "429 Too Many Requests на %s — жду %d сек",
                    url, self.on_429_wait,
                )
                time.sleep(self.on_429_wait)
                self.rate_limiter.wait()
                r = self.session.get(url, timeout=self.timeout)
            if r.status_code == 404:
                return None
            if r.status_code == 403:
                log.warning("403 Forbidden на %s — возможно бан", url)
                self._stopped = True
                return None
            r.raise_for_status()
            return r.text
        except Exception as e:
            log.warning("GET %s failed: %s", url, e)
            return None

    # ---------------------- публичные методы -------------------------- #

    def list_drug_slugs(self) -> List[str]:
        """Получить список всех slug-ов препаратов со страницы /drug/."""
        html = self._get(self.list_url)
        if not html:
            return []
        soup = BeautifulSoup(html, "html.parser")
        slugs: List[str] = []
        for a in soup.find_all("a", href=True):
            href = a["href"]
            # vetprotocol использует относительные ссылки типа "albendazol"
            if href and not href.startswith(("http", "/", "#", "?")):
                # только если slug-формат (только буквы/цифры/дефис, без точки)
                if re.match(r"^[a-z0-9-]+$", href):
                    slugs.append(href)
        # дедуп
        seen = set()
        unique = []
        for s in slugs:
            if s not in seen:
                seen.add(s)
                unique.append(s)
        log.info("Найдено %d уникальных slug-ов на /drug/", len(unique))
        return unique

    def fetch_drug(self, slug: str) -> Optional[VetprotocolDrug]:
        """Получить и распарсить страницу препарата."""
        url = f"{self.base_url}/drug/{slug}"
        html = self._get(url)
        if not html:
            return None
        drug = _parse_drug_page(html, slug, url)
        return drug

    def iter_all_drugs(
        self, max_drugs: Optional[int] = None
    ) -> Iterator[VetprotocolDrug]:
        """Итеративно пройти все препараты."""
        if self._stopped:
            log.error("Парсер остановлен: получен 403 — возможно бан")
            return
        slugs = self.list_drug_slugs()
        if max_drugs is None:
            max_drugs = self.cfg.vetprotocol.max_drugs
        if max_drugs:
            slugs = slugs[:max_drugs]
        for i, slug in enumerate(slugs, 1):
            if self._stopped:
                log.error("Остановка по 403 на [%d/%d]", i, len(slugs))
                break
            log.info("[%d/%d] %s", i, len(slugs), slug)
            drug = self.fetch_drug(slug)
            if drug:
                yield drug


# ---------------------------------------------------------------------------
# Парсинг страницы препарата
# ---------------------------------------------------------------------------

# Регулярки для извлечения структуры из текста
_ANIMAL_PATTERNS = [
    r"собак(?:и|у)?",
    r"кошек(?:и|у)?",
    r"крс",
    r"крупн\w* рогат\w* скот\w*",
    r"мрс",
    r"мелк\w* рогат\w* скот\w*",
    r"свин(?:ей|ьи|ью)?",
    r"лошад(?:ей|и|ьми)?",
    r"птиц\w*",
    r"кролик\w*",
    r"пушн\w*",
    r"пч[её]л",
]
_ANIMAL_RE = re.compile("|".join(f"({p})" for p in _ANIMAL_PATTERNS), re.I)

# Нормализация названий животных (как в drugs_calc.json)
_ANIMAL_NORMALIZE = {
    "собак": "Собаки", "собаки": "Собаки", "собаку": "Собаки",
    "кошек": "Кошки", "кошки": "Кошки", "кошку": "Кошки",
    "крс": "КРС", "крупного рогатого скота": "КРС",
    "крупным рогатым скотом": "КРС",
    "мрс": "МРС", "мелкого рогатого скота": "МРС",
    "свиней": "Свиньи", "свиньи": "Свиньи", "свинью": "Свиньи",
    "лошадей": "Лошади", "лошади": "Лошади", "лошадь": "Лошади",
    "птиц": "Птица", "птицы": "Птица", "птицу": "Птица",
    "кроликов": "Кролики", "кролика": "Кролики",
    "пушных": "Пушные звери",
    "пчел": "Пчёлы", "пчёлы": "Пчёлы",
}

_DOSE_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*(мг|мл|мкг|МЕ|г)\s*/?\s*(кг|м²|м2)?", re.I)
_FREQ_RE = re.compile(
    r"(кажд\w*\s+\d+\s*(?:ч|час|часов|мин)|\d+\s*раз\s*в\s*\w+|1\s*раз\s*в\s*\w+|"
    r"сутки|ежедневно|через\s+\w+)",
    re.I,
)
_ROUTE_RE = re.compile(
    r"(перорально|внутривенно|внутримышечно|подкожно|субконъюнктивально|"
    r"интраазально|ингаляционно|ректально|внутрь|в/в|в/м|п/к|наружно)",
    re.I,
)
_COURSE_RE = re.compile(
    r"в\s*течение\s+([\d\-–]+\s*(?:дн\w*|сут\w*|нед\w*|месяц\w*))",
    re.I,
)


def _normalize_animal(raw: str) -> str:
    """Привести название животного к каноничному виду (как в vetvoice)."""
    raw = raw.lower().strip()
    for k, v in _ANIMAL_NORMALIZE.items():
        if k in raw:
            return v
    return raw


def _parse_drug_page(html: str, slug: str, url: str) -> VetprotocolDrug:
    drug = VetprotocolDrug(slug=slug, source_url=url)
    soup = BeautifulSoup(html, "html.parser")

    # <title>
    if soup.title:
        title = soup.title.get_text(strip=True)
        # часто "Альбендазол (Немозол)" — берём первую часть как МНН,
        # в скобках — торговое наименование
        m = re.match(r"^\s*([^()]+?)\s*(?:\(([^)]+)\))?\s*$", title)
        if m:
            drug.name = m.group(1).strip()
            if m.group(2):
                drug.synonyms = [s.strip() for s in m.group(2).split(",")]

    # vetprotocol: основной контент в <div id="content">, заголовок в <h1 id="name">
    # Причём </body> в HTML стоит раньше, чем <main> — это ошибка вёрстки,
    # но BeautifulSoup всё равно собирает DOM целиком.
    content_div = soup.find("div", id="content")
    name_h1 = soup.find("h1", id="name")
    if content_div is None:
        # fallback на <main>
        content_div = soup.find("main") or soup.body
    if content_div is None:
        return drug

    # Заголовок
    if name_h1:
        h1_text = name_h1.get_text(strip=True)
        # "Альбендазол (Немозол)" — переопределяем имя
        m = re.match(r"^\s*([^()]+?)\s*(?:\(([^)]+)\))?\s*$", h1_text)
        if m:
            drug.name = m.group(1).strip()
            if m.group(2):
                drug.synonyms = [s.strip() for s in m.group(2).split(",")]

    # Заменяем <br/> на переводы строк, затем извлекаем текст построчно
    for br in content_div.find_all("br"):
        br.replace_with("\n")

    # Удаляем служебные теги
    for tag in content_div.find_all(["script", "style", "nav", "footer", "header"]):
        tag.decompose()

    raw_text = content_div.get_text()
    # Сохраняем оригинальное разбиение по <br/>, без агрессивного склеивания
    raw_lines = [l.strip() for l in raw_text.split("\n") if l.strip()]

    # Разбиваем строки по ключевым маркерам секций, чтобы «(!)» и «ДОЗА:»
    # всегда начинали новый блок, даже если прилеплены к предыдущей строке.
    section_markers = [
        "ДОЗА:", "ДОЗА", "Дозировка:", "Дозировка",
        "Форма выпуска:", "Форма выпуска",
        "Использование:", "Использование",
        "Показания:", "Показания",
        "Противопоказания:", "Противопоказания",
        "Побочные эффекты:", "Побочные эффекты",
        "Побочные действия:", "Побочные действия",
        "(!)",
    ]
    lines: List[str] = []
    for line in raw_lines:
        # Разбиваем строку, если внутри неё есть маркер секции
        parts = [line]
        for marker in section_markers:
            new_parts: List[str] = []
            for p in parts:
                if marker in p and p.strip() != marker:
                    # разбиваем по маркеру, сохраняя маркер в начале новой части
                    chunks = re.split(
                        rf"({re.escape(marker)})", p
                    )
                    buf = ""
                    for c in chunks:
                        if c == marker:
                            if buf.strip():
                                new_parts.append(buf.strip())
                            new_parts.append(marker)
                            buf = ""
                        else:
                            buf += c
                    if buf.strip():
                        new_parts.append(buf.strip())
                else:
                    new_parts.append(p)
            parts = new_parts
        lines.extend(parts)

    # Теперь склеим слишком короткие строки (< 20 символов) с предыдущей,
    # кроме строк, начинающихся с маркеров секций.
    marker_set = set(section_markers)
    merged: List[str] = []
    for line in lines:
        if not line:
            continue
        is_marker = line in marker_set or any(
            line.startswith(m) for m in marker_set
        )
        if merged and not is_marker and len(line) < 20:
            merged[-1] = merged[-1] + " " + line
        else:
            merged.append(line)
    lines = merged
    drug.raw_text = "\n".join(lines)

    # Парсим структуру: блоки разделены заголовками «ДОЗА:», «(!)», и т.п.
    current_section = "intro"
    current_animal = ""
    for line in lines:
        line_stripped = line.strip()
        if not line_stripped:
            continue

        low = line_stripped.lower()

        # Маркер предупреждения «(!)» — может быть в начале строки
        if line_stripped.startswith("(!)"):
            warn_text = line_stripped[3:].strip()
            if warn_text:
                drug.warnings.append(warn_text)
            current_section = "warning"
            continue

        # Маркер начала раздела доз. Может быть вместе с указанием животного:
        # «ДОЗА: Собаки, кошки:» — разбиваем.
        dose_marker_match = re.match(
            r"^(ДОЗА|Дозировка)\s*:\s*(.*)$", line_stripped, re.I
        )
        if dose_marker_match:
            current_section = "dose"
            tail = dose_marker_match.group(2).strip()
            if tail:
                # Если после «ДОЗА:» сразу идёт указание животного — запомним
                if ":" in tail and _ANIMAL_RE.search(tail):
                    current_animal = tail.rstrip(":").strip()
                else:
                    # иначе это первая строка дозировки
                    if _DOSE_RE.search(tail):
                        drug.doses.append(_parse_dose_line(tail, current_animal))
            continue

        # Если это указание животного (отдельной строкой, обычно перед списком доз)
        if current_section == "dose":
            animal_match = _ANIMAL_RE.search(line_stripped)
            if animal_match and ":" in line_stripped and len(line_stripped) < 80:
                # Строка вида "Собаки, кошки:" — задаёт контекст животного
                current_animal = line_stripped.rstrip(":").strip()
                continue

            # Если строка выглядит как дозировка (есть цифры и единицы)
            if _DOSE_RE.search(line_stripped):
                dose = _parse_dose_line(line_stripped, current_animal)
                drug.doses.append(dose)
                continue

        # Иначе — это показания (только если короткий блок до ДОЗА)
        if current_section == "intro":
            # Пропускаем навигационные ссылки
            if len(line_stripped) > 10 and not line_stripped.startswith("©"):
                drug.indications.append(line_stripped)

    # Дедуп показаний
    seen = set()
    drug.indications = [
        i for i in drug.indications
        if not (i in seen or seen.add(i))
    ]

    return drug


def _parse_dose_line(text: str, animal_hint: str = "") -> VetprotocolDose:
    """Распарсить одну строку дозировки."""
    dose = VetprotocolDose()
    dose.dose_text = text

    # Животное
    if animal_hint:
        dose.animal = _normalize_animal(animal_hint)
    animal_match = _ANIMAL_RE.search(text)
    if not dose.animal and animal_match:
        dose.animal = _normalize_animal(animal_match.group(0))

    # Ограничим текст первыми 200 символами — иначе после «;» может быть
    # много лишнего, и парсер возьмёт дозу из следующего блока.
    parse_text = text[:200]

    # Доза (мг/кг, мл/кг, ...)
    m = _DOSE_RE.search(parse_text)
    if m:
        try:
            dose.dose_per_kg = float(m.group(1).replace(",", "."))
            unit = m.group(2) or ""
            per = m.group(3) or ""
            dose.dose_unit = f"{unit}/{per}".strip("/") if per else unit
        except (ValueError, IndexError):
            pass

    # Если в тексте диапазон «25-50 мг/кг», берём максимум
    range_m = re.search(r"(\d+(?:[.,]\d+)?)\s*[-–]\s*(\d+(?:[.,]\d+)?)\s*(мг|мл|мкг|МЕ|г)\s*/?\s*(кг)?", parse_text, re.I)
    if range_m:
        try:
            dose.dose_per_kg = float(range_m.group(2).replace(",", "."))
            unit = range_m.group(3)
            per = range_m.group(4) or "кг"
            dose.dose_unit = f"{unit}/{per}"
        except (ValueError, IndexError):
            pass

    # Кратность
    freq_m = _FREQ_RE.search(parse_text)
    if freq_m:
        dose.frequency = freq_m.group(1).strip()

    # Путь введения
    route_m = _ROUTE_RE.search(parse_text)
    if route_m:
        dose.route = route_m.group(1).lower()

    # Курс
    course_m = _COURSE_RE.search(parse_text)
    if course_m:
        dose.course_days = course_m.group(1).strip()

    # Показание (часть после двоеточия или "для ...")
    if ":" in parse_text:
        parts = parse_text.split(":", 1)
        if len(parts) == 2 and len(parts[1].strip()) < 200:
            dose.indication = parts[1].strip()
    m_for = re.search(r"\b(?:для|при)\s+([^,.;:]{5,80})", parse_text, re.I)
    if m_for and not dose.indication:
        dose.indication = m_for.group(1).strip()

    return dose


# ---------------------------------------------------------------------------
# Публичный интерфейс
# ---------------------------------------------------------------------------

def fetch_all_drugs(
    output_path: str, max_drugs: Optional[int] = None
) -> int:
    """Скачать все препараты и сохранить в JSON-файл."""
    import json

    client = VetprotocolClient()
    cfg = client.cfg.vetprotocol
    log.info(
        "Начинаем выгрузку препаратов с vetprotocol.ru (delay=%.2fs, UA=%s)",
        client.delay, client.cfg.global_config.user_agent[:60],
    )
    drugs = []
    for drug in client.iter_all_drugs(max_drugs=max_drugs):
        drugs.append(drug.to_dict())
        # Промежуточное сохранение каждые 20 препаратов
        if len(drugs) % 20 == 0:
            with open(output_path, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "source": "vetprotocol.ru",
                        "source_url": client.base_url,
                        "total_drugs": len(drugs),
                        "drugs": drugs,
                    },
                    f,
                    ensure_ascii=False,
                    indent=2,
                )
            log.info("Промежуточное сохранение: %d препаратов", len(drugs))

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "source": "vetprotocol.ru",
                "source_url": client.base_url,
                "total_drugs": len(drugs),
                "drugs": drugs,
            },
            f,
            ensure_ascii=False,
            indent=2,
        )
    log.info("Сохранено %d препаратов в %s", len(drugs), output_path)
    return len(drugs)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser(description="Парсер vetprotocol.ru")
    p.add_argument("--list", action="store_true", help="Вывести список slug-ов")
    p.add_argument("--fetch", metavar="SLUG", help="Скачать один препарат")
    p.add_argument("--fetch-all", metavar="OUTPUT_JSON", help="Скачать все препараты в JSON")
    p.add_argument("--max", type=int, default=None, help="Лимит препаратов")
    p.add_argument("--delay", type=float, default=None,
                   help="Переопределить delay (секунд между запросами)")
    p.add_argument("--config", default=None, help="Путь к config.yaml")
    args = p.parse_args()

    if args.config:
        from pathlib import Path
        from config import load_config
        load_config(Path(args.config))

    client_kwargs = {"delay": args.delay} if args.delay else {}

    if args.list:
        c = VetprotocolClient(**client_kwargs)
        slugs = c.list_drug_slugs()
        print(f"Всего slug-ов: {len(slugs)}")
        for s in slugs[:50]:
            print(f"  {s}")
    elif args.fetch:
        c = VetprotocolClient(**client_kwargs)
        d = c.fetch_drug(args.fetch)
        if d:
            print(d.to_dict())
        else:
            print("Не найдено")
    elif args.fetch_all:
        fetch_all_drugs(args.fetch_all, args.max)
    else:
        p.print_help()
