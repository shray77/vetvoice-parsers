"""
Парсер vidal.ru/veterinar — справочник ветеринарных препаратов Видаль.

Структура сайта:
  * https://www.vidal.ru/veterinar                    — главная
  * https://www.vidal.ru/veterinar/{slug}             — карточка препарата

Возвращаемые данные:
  * Название (торговое и МНН)
  * Лекарственная форма, концентрация
  * Фармакологическая группа
  * Показания
  * Дозировка
  * Побочные действия
  * Противопоказания
  * Срок хранения / условия хранения

Этот парсер извлекает структурированные данные и приводит их к формату,
совместимому с drugs_calc.json.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field, asdict
from typing import Iterator, List, Optional, Dict
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

log = logging.getLogger("vidal_parser")
if not log.handlers:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


BASE_URL = "https://www.vidal.ru"
VET_BASE_URL = f"{BASE_URL}/veterinar"

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
}
DEFAULT_TIMEOUT = 30
DEFAULT_DELAY = 1.0  # vidal может банить, ставим задержку больше


# ---------------------------------------------------------------------------
# Модели
# ---------------------------------------------------------------------------

@dataclass
class VidalDrug:
    """Препарат с vidal.ru/veterinar."""
    slug: str = ""
    url: str = ""
    trade_name: str = ""
    inn: str = ""
    form: str = ""  # лекарственная форма
    composition: str = ""
    pharmacology: str = ""
    indications: str = ""
    dosage: str = ""
    contraindications: str = ""
    side_effects: str = ""
    overdose: str = ""
    interactions: str = ""
    special_notes: str = ""
    storage: str = ""
    raw_text: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("raw_text", None)
        return d


# ---------------------------------------------------------------------------
# Клиент
# ---------------------------------------------------------------------------

class VidalClient:
    def __init__(self, delay: float = DEFAULT_DELAY):
        self.session = requests.Session()
        self.session.headers.update(DEFAULT_HEADERS)
        self.delay = delay

    def _get(self, url: str) -> Optional[str]:
        try:
            r = self.session.get(url, timeout=DEFAULT_TIMEOUT)
            if r.status_code == 404:
                return None
            r.raise_for_status()
            return r.text
        except Exception as e:
            log.warning("GET %s failed: %s", url, e)
            return None

    # ---------------------- публичные методы -------------------------- #

    def list_drugs(self, max_pages: int = 100) -> List[str]:
        """Получить список slug-ов препаратов через алфавитный указатель."""
        slugs: List[str] = []
        # vidal использует алфавит: /veterinar?letter=X
        for letter in "АБВГДЕЖЗИЙКЛМНОПРСТУФХЦЧШЩЭЮЯ":
            for page in range(1, max_pages + 1):
                url = f"{VET_BASE_URL}?letter={letter}&p={page}"
                html = self._get(url)
                if not html:
                    break
                soup = BeautifulSoup(html, "html.parser")
                # Поиск ссылок на препараты — обычно /veterinar/{slug}-{id}
                page_slugs: List[str] = []
                for a in soup.find_all("a", href=True):
                    href = a["href"]
                    m = re.match(r"^/veterinar/([a-z0-9-]+-\d+)$", href)
                    if m:
                        page_slugs.append(m.group(1))
                if not page_slugs:
                    break
                slugs.extend(page_slugs)
                log.info("letter=%s page=%d: %d препаратов", letter, page, len(page_slugs))
                time.sleep(self.delay)
        # дедуп
        seen = set()
        unique = []
        for s in slugs:
            if s not in seen:
                seen.add(s)
                unique.append(s)
        log.info("Всего уникальных препаратов vidal: %d", len(unique))
        return unique

    def fetch_drug(self, slug: str) -> Optional[VidalDrug]:
        url = f"{VET_BASE_URL}/{slug}"
        html = self._get(url)
        if not html:
            return None
        return _parse_drug_page(html, slug, url)

    def iter_all_drugs(
        self, max_drugs: Optional[int] = None
    ) -> Iterator[VidalDrug]:
        slugs = self.list_drugs()
        if max_drugs:
            slugs = slugs[:max_drugs]
        for i, slug in enumerate(slugs, 1):
            log.info("[%d/%d] %s", i, len(slugs), slug)
            drug = self.fetch_drug(slug)
            if drug:
                yield drug
            time.sleep(self.delay)


# ---------------------------------------------------------------------------
# Парсинг карточки препарата
# ---------------------------------------------------------------------------

# vidal.ru использует разные классы для блоков инструкций
SECTION_SELECTORS = {
    "composition": ["Состав и форма выпуска", "Состав", "Описание"],
    "pharmacology": ["Фармакологическое действие", "Фармакология"],
    "indications": ["Показания", "Показания к применению"],
    "dosage": ["Способ применения и дозы", "Дозировка", "Режим дозирования"],
    "contraindications": ["Противопоказания"],
    "side_effects": ["Побочные действия", "Побочные эффекты"],
    "overdose": ["Передозировка"],
    "interactions": ["Лекарственное взаимодействие", "Взаимодействие"],
    "special_notes": ["Особые указания", "Меры предосторожности"],
    "storage": ["Условия хранения", "Срок годности", "Хранение"],
}


def _parse_drug_page(html: str, slug: str, url: str) -> VidalDrug:
    drug = VidalDrug(slug=slug, url=url)
    soup = BeautifulSoup(html, "html.parser")

    # Заголовок
    h1 = soup.find("h1")
    if h1:
        drug.trade_name = h1.get_text(" ", strip=True)

    # Поиск блоков по классу
    # vidal.ru хранит блоки в <div class="block">, заголовок в <h3> или <div class="block-header">
    blocks: Dict[str, str] = {}
    for block in soup.find_all("div", class_=re.compile("block|section", re.I)):
        # Ищем заголовок блока
        header = block.find(["h2", "h3", "h4"]) or block.find(
            "div", class_=re.compile("header|title", re.I)
        )
        if not header:
            continue
        header_text = header.get_text(" ", strip=True).rstrip(":.").strip()
        # Сопоставление с нашими секциями
        for field, names in SECTION_SELECTORS.items():
            if any(n.lower() in header_text.lower() for n in names):
                # Удаляем заголовок и берём остальной текст
                header.extract()
                text = block.get_text(" ", strip=True)
                text = re.sub(r"\s+", " ", text).strip()
                if text:
                    blocks[field] = text
                break

    for field, text in blocks.items():
        setattr(drug, field, text)

    # Форма выпуска и МНН — пытаемся извлечь из заголовка
    if drug.trade_name:
        # Часто «Препарат® 5% раствор» или «Препарат (МНН)»
        m = re.search(r"\(([^)]+)\)", drug.trade_name)
        if m and not drug.inn:
            drug.inn = m.group(1).strip()

    return drug


# ---------------------------------------------------------------------------
# Публичный интерфейс
# ---------------------------------------------------------------------------

def fetch_all_drugs(
    output_path: str, max_drugs: Optional[int] = None
) -> int:
    """Скачать все препараты и сохранить в JSON."""
    import json

    client = VidalClient()
    log.info("Начинаем выгрузку препаратов с vidal.ru/veterinar")
    items = []
    for drug in client.iter_all_drugs(max_drugs=max_drugs):
        items.append(drug.to_dict())

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "source": "vidal.ru/veterinar",
                "source_url": VET_BASE_URL,
                "total_drugs": len(items),
                "drugs": items,
            },
            f,
            ensure_ascii=False,
            indent=2,
        )
    log.info("Сохранено %d препаратов в %s", len(items), output_path)
    return len(items)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser(description="Парсер vidal.ru/veterinar")
    p.add_argument("--list", action="store_true", help="Вывести список slug-ов")
    p.add_argument("--fetch", metavar="SLUG", help="Скачать один препарат")
    p.add_argument("--fetch-all", metavar="OUTPUT_JSON", help="Скачать все препараты в JSON")
    p.add_argument("--max", type=int, default=None, help="Лимит препаратов")
    args = p.parse_args()

    if args.list:
        c = VidalClient()
        slugs = c.list_drugs()
        print(f"Всего slug-ов: {len(slugs)}")
        for s in slugs[:30]:
            print(f"  {s}")
    elif args.fetch:
        c = VidalClient()
        d = c.fetch_drug(args.fetch)
        if d:
            for k, v in d.to_dict().items():
                if v:
                    print(f"{k}: {str(v)[:300]}")
        else:
            print("Не найдено")
    elif args.fetch_all:
        fetch_all_drugs(args.fetch_all, args.max)
    else:
        p.print_help()
