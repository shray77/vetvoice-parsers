"""
Парсер reestrinform.ru — публичное зеркало реестра ветеринарных препаратов РФ.

Этот сайт отображает данные из государственного реестра ЛС (компонент «Гален»
ФГИС ВетИС, Россельхознадзор), но в открытом доступе без авторизации.

Структура сайта:
  * https://reestrinform.ru/reestr-veterinarnykh-preparatov-rf.html — список
  * https://reestrinform.ru/preparat/{slug}                          — карточка

Карточка содержит:
  * Торговое наименование
  * МНН (международное непатентованное)
  * Лекарственная форма
  * Производитель / держатель РУ
  * Номер и дата РУ
  * Срок действия РУ
  * Состав (если указан)
  * Показания
  * Дозировка
  * Побочные действия
  * Противопоказания
  * Условия хранения

⚠️ Сайт защищён Cloudflare/Browser-Check. Нужно использовать заголовки
   браузера и/или playwright для обхода.
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

log = logging.getLogger("reestrinform_parser")
if not log.handlers:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


BASE_URL = "https://reestrinform.ru"
LIST_URL = f"{BASE_URL}/reestr-veterinarnykh-preparatov-rf.html"

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Cache-Control": "max-age=0",
}
DEFAULT_TIMEOUT = 30
DEFAULT_DELAY = 1.0


# ---------------------------------------------------------------------------
# Модели
# ---------------------------------------------------------------------------

@dataclass
class ReestrInformEntry:
    """Запись из реестра reestrinform.ru."""
    slug: str = ""
    url: str = ""
    trade_name: str = ""
    inn: str = ""
    form: str = ""
    producer: str = ""
    certificate_owner: str = ""
    certificate_number: str = ""
    certificate_date: str = ""
    certificate_end_date: str = ""
    composition: str = ""
    indications: str = ""
    dosage: str = ""
    contraindications: str = ""
    side_effects: str = ""
    storage: str = ""
    raw_text: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("raw_text", None)
        return d


# ---------------------------------------------------------------------------
# Клиент
# ---------------------------------------------------------------------------

class ReestrInformClient:
    def __init__(self, delay: float = DEFAULT_DELAY):
        self.session = requests.Session()
        self.session.headers.update(DEFAULT_HEADERS)
        self.delay = delay

    def _get(self, url: str) -> Optional[str]:
        try:
            r = self.session.get(url, timeout=DEFAULT_TIMEOUT)
            if r.status_code == 404:
                return None
            if r.status_code == 503:
                log.warning("503 на %s (Cloudflare/Browser-Check)", url)
                return None
            r.raise_for_status()
            return r.text
        except Exception as e:
            log.warning("GET %s failed: %s", url, e)
            return None

    # ---------------------- публичные методы -------------------------- #

    def list_slugs(self) -> List[str]:
        """Получить список slug-ов препаратов со страницы реестра."""
        html = self._get(LIST_URL)
        if not html:
            log.error("Не удалось загрузить страницу реестра")
            return []
        soup = BeautifulSoup(html, "html.parser")
        slugs: List[str] = []
        for a in soup.find_all("a", href=True):
            href = a["href"]
            m = re.match(r"^/preparat/([a-z0-9-]+)/?$", href)
            if m:
                slugs.append(m.group(1))
        seen = set()
        unique = []
        for s in slugs:
            if s not in seen:
                seen.add(s)
                unique.append(s)
        log.info("Найдено %d препаратов на reestrinform.ru", len(unique))
        return unique

    def fetch_entry(self, slug: str) -> Optional[ReestrInformEntry]:
        url = f"{BASE_URL}/preparat/{slug}"
        html = self._get(url)
        if not html:
            return None
        return _parse_entry_page(html, slug, url)

    def iter_all_entries(
        self, max_entries: Optional[int] = None
    ) -> Iterator[ReestrInformEntry]:
        slugs = self.list_slugs()
        if max_entries:
            slugs = slugs[:max_entries]
        for i, slug in enumerate(slugs, 1):
            log.info("[%d/%d] %s", i, len(slugs), slug)
            entry = self.fetch_entry(slug)
            if entry:
                yield entry
            time.sleep(self.delay)


# ---------------------------------------------------------------------------
# Парсинг карточки препарата
# ---------------------------------------------------------------------------

# reestrinform: блоки с заголовками «Показания», «Дозировка», и т.д.
SECTION_KEYWORDS = {
    "composition": ["состав", "компонентный состав", "действующие вещества"],
    "indications": ["показани"],
    "dosage": ["дозировк", "способ применения", "дозы"],
    "contraindications": ["противопоказани"],
    "side_effects": ["побочн"],
    "storage": ["условия хранения", "срок годности", "хранени"],
}


def _parse_entry_page(html: str, slug: str, url: str) -> ReestrInformEntry:
    entry = ReestrInformEntry(slug=slug, url=url)
    soup = BeautifulSoup(html, "html.parser")

    # Заголовок
    h1 = soup.find("h1")
    if h1:
        entry.trade_name = h1.get_text(" ", strip=True)

    # Поиск характеристик в таблице (часто так)
    for row in soup.find_all("tr"):
        cells = row.find_all(["th", "td"])
        if len(cells) == 2:
            key = cells[0].get_text(" ", strip=True).lower().rstrip(":").strip()
            val = cells[1].get_text(" ", strip=True)
            if "мнн" in key or "международн" in key or "непатентован" in key:
                entry.inn = val
            elif "форм" in key and "выпуск" in key:
                entry.form = val
            elif "производ" in key:
                entry.producer = val
            elif "держател" in key and "регистрац" in key:
                entry.certificate_owner = val
            elif "номер" in key and ("регистрац" in key or "РУ" in key):
                entry.certificate_number = val
            elif "дата" in key and "регистрац" in key:
                entry.certificate_date = val
            elif "срок" in key and "действ" in key:
                entry.certificate_end_date = val

    # Поиск блоков с заголовками
    for header in soup.find_all(["h2", "h3", "h4", "b", "strong"]):
        header_text = header.get_text(" ", strip=True).rstrip(":.").strip()
        if not header_text or len(header_text) > 80:
            continue
        low = header_text.lower()
        for field, kws in SECTION_KEYWORDS.items():
            if any(k in low for k in kws):
                # Берём следующий соседний элемент с текстом
                next_el = header.find_next_sibling()
                text_parts = []
                while next_el and next_el.name not in ["h2", "h3", "h4"]:
                    if next_el.name in ["p", "div", "span", "ul", "ol", "li"]:
                        text_parts.append(next_el.get_text(" ", strip=True))
                    next_el = next_el.find_next_sibling()
                text = " ".join(text_parts).strip()
                text = re.sub(r"\s+", " ", text)
                if text:
                    setattr(entry, field, text)
                break

    return entry


# ---------------------------------------------------------------------------
# Публичный интерфейс
# ---------------------------------------------------------------------------

def fetch_all_entries(
    output_path: str, max_entries: Optional[int] = None
) -> int:
    """Скачать все записи и сохранить в JSON."""
    import json

    client = ReestrInformClient()
    log.info("Начинаем выгрузку с reestrinform.ru")
    items = []
    for entry in client.iter_all_entries(max_entries=max_entries):
        items.append(entry.to_dict())

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "source": "reestrinform.ru (зеркало реестра Гален)",
                "source_url": BASE_URL,
                "total_entries": len(items),
                "entries": items,
            },
            f,
            ensure_ascii=False,
            indent=2,
        )
    log.info("Сохранено %d записей в %s", len(items), output_path)
    return len(items)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser(description="Парсер reestrinform.ru")
    p.add_argument("--list", action="store_true", help="Вывести список slug-ов")
    p.add_argument("--fetch-all", metavar="OUTPUT_JSON", help="Скачать всё в JSON")
    p.add_argument("--max", type=int, default=None, help="Лимит")
    args = p.parse_args()

    if args.list:
        c = ReestrInformClient()
        slugs = c.list_slugs()
        print(f"Всего slug-ов: {len(slugs)}")
        for s in slugs[:30]:
            print(f"  {s}")
    elif args.fetch_all:
        fetch_all_entries(args.fetch_all, args.max)
    else:
        p.print_help()
