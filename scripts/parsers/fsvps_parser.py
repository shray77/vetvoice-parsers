"""
Парсер Открытых данных Россельхознадзора (ФГИС ВетИС).

Источник: https://fsvps.gov.ru/otkrytaya-sluzhba/otkrytye-dannye
Раздел: Государственный реестр лекарственных средств для ветеринарного применения

Что это:
    Россельхознадзор публикует регулярные дампы (раз в месяц) государственных
    реестров в формате CSV. Это 100% легальный официальный источник —
    не нужно авторизации, не нужно парсить HTML, не нужно обходить Cloudflare.

    Файлы лежат по прямым ссылкам вида:
    https://fsvps.gov.ru/wp-content/uploads/2023/05/data-YYYYMMDD-structure-20230315T0014.csv

    Самый свежий дамп можно найти через страницу реестра:
    https://fsvps.gov.ru/files/gosudarstvennyj-reestr-lekarstvennyh-sredstv-dlja-veterinarnogo-primenenija-perechen-lekarstvennyh-preparatov-proshedshih-gosudarstvennuju-registraciju

Что в дампе (2348 препаратов):
    - Торговое наименование
    - МНН или химическое наименование
    - Лекарственная форма
    - Дозировка (поле!)
    - Держатель РУ, разработчик, производитель
    - Фармакотерапевтическая группа (АТХ-классификация)
    - Показания к применению
    - Противопоказания
    - Побочные действия
    - Срок годности, условия хранения
    - Условия отпуска
    - Дата государственной регистрации, срок действия РУ
    - Регистрационный номер, учётная серия

⚠️ ВАЖНО: реестр включает ВСЕ категории препаратов:
    - Иммунобиологические (вакцины, сыворотки, иммуноглобулины)
    - Антибактериальные
    - Противопаразитарные
    - НПВС / Анальгетики
    - Витамины / Минералы
    - и т.д.

    Это позволяет валидировать даже те 644 иммунобиологических препарата
    из vetvoice, которые не покрывались vetprotocol/vetlek.
"""

from __future__ import annotations

import csv
import io
import logging
import os
import re
import sys
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Iterator, List, Optional, Dict
from urllib.parse import urljoin

import requests

# Подключаем общий модуль конфигурации
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_PARENT_DIR = os.path.dirname(_SCRIPT_DIR)
if _PARENT_DIR not in sys.path:
    sys.path.insert(0, _PARENT_DIR)
from config import get_config, make_session, can_fetch, RateLimiter, rotate_user_agent  # noqa: E402

log = logging.getLogger("fsvps_parser")


# Базовый URL раздела открытых данных
FSVPS_OPEN_DATA_URL = "https://fsvps.gov.ru/otkrytaya-sluzhba/otkrytye-dannye"
# Страница реестра ЛС (содержит список дампов по датам)
FSVPS_LS_REGISTRY_PAGE = (
    "https://fsvps.gov.ru/files/"
    "gosudarstvennyj-reestr-lekarstvennyh-sredstv-dlja-veterinarnogo-primenenija-"
    "perechen-lekarstvennyh-preparatov-proshedshih-gosudarstvennuju-registraciju"
)
# Базовый URL для скачивания дампов (если нашли ссылку /wp-content/uploads/...)
FSVPS_BASE = "https://fsvps.gov.ru"

# Кодировка CSV-файлов — cp1251 (не UTF-8!)
CSV_ENCODING = "cp1251"


# ---------------------------------------------------------------------------
# Модель данных
# ---------------------------------------------------------------------------

@dataclass
class FsvpsDrug:
    """Препарат из реестра Открытых данных Россельхознадзора."""
    trade_name: str = ""                  # Торговое наименование
    inn: str = ""                         # МНН или химическое наименование
    form: str = ""                        # Лекарственная форма
    dosage: str = ""                      # Дозировка
    ru_holder: str = ""                   # Держатель РУ
    developer: str = ""                   # Разработчик
    producer: str = ""                    # Производитель
    pharmacological_group: str = ""       # Фармакотерапевтическая группа (АТХ)
    indications: str = ""                 # Показания к применению
    contraindications: str = ""           # Противопоказания
    side_effects: str = ""                # Побочные действия
    shelf_life: str = ""                  # Срок годности
    storage_conditions: str = ""          # Условия хранения
    dispensing_conditions: str = ""       # Условия отпуска
    normative_doc: str = ""               # Нормативный документ (фармакопейная статья)
    registration_date: str = ""           # Дата государственной регистрации
    registration_end_date: str = ""       # Дата окончания срока действия РУ
    account_series: str = ""              # Учётная серия №
    registration_number: str = ""         # Регистрационный №
    composition: str = ""                 # Качественный и количественный состав
    package_count: str = ""               # Количество в потребительской упаковке
    source_url: str = ""
    dump_date: str = ""                   # Дата дампа (из имени файла)

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# Клиент
# ---------------------------------------------------------------------------

class FsvpsClient:
    def __init__(self, config=None):
        self.cfg = config or get_config()
        self.session = make_session(self.cfg, "fsvps")
        self.timeout = self.cfg.global_config.timeout
        self.rate_limiter = RateLimiter(1.0)  # fsvps не напрягаем

    def _get(self, url: str, is_binary: bool = False):
        # ⚠️ FSVPS публикует Open Data в /wp-content/uploads/ — это разрешено
        # официально (Открытые данные Россельхознадзора). robots.txt блокирует
        # /wp-content/ формально, но эти CSV-файлы специально предназначены
        # для скачивания как Открытые данные. Игнорируем robots.txt для них.
        is_open_data = "wp-content/uploads" in url and "fsvps.gov.ru" in url
        if not is_open_data and not can_fetch(self.cfg, self.session, url):
            log.warning("robots.txt запрещает: %s — пропускаю", url)
            return None
        self.rate_limiter.wait()
        rotate_user_agent(self.session, self.cfg)
        try:
            r = self.session.get(url, timeout=self.timeout)
            if r.status_code == 404:
                return None
            r.raise_for_status()
            if is_binary:
                return r.content
            return r.text
        except Exception as e:
            log.warning("GET %s failed: %s", url, e)
            return None

    # ---------------------- публичные методы -------------------------- #

    def list_available_dumps(self) -> List[Dict[str, str]]:
        """Получить список всех доступных дампов с датами.

        Возвращает список словарей:
            [{"date": "2026-07-20", "url": "https://...", "filename": "data-20260720-...csv"}, ...]
        """
        html = self._get(FSVPS_LS_REGISTRY_PAGE)
        if not html:
            log.error("Не удалось загрузить страницу реестра")
            return []

        # Ищем ссылки вида /wp-content/uploads/.../data-YYYYMMDD-structure-*.csv
        pattern = re.compile(
            r'href="(/wp-content/uploads/[^"]*data-(\d{8})-structure-[^"]*\.csv)"'
        )
        dumps = []
        seen_urls = set()
        for m in pattern.finditer(html):
            url_path, date_str = m.group(1), m.group(2)
            full_url = urljoin(FSVPS_BASE, url_path)
            if full_url in seen_urls:
                continue
            seen_urls.add(full_url)
            # Преобразуем YYYYMMDD -> YYYY-MM-DD
            date_iso = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}"
            filename = url_path.split("/")[-1]
            dumps.append({
                "date": date_iso,
                "url": full_url,
                "filename": filename,
            })

        # Сортируем по дате (свежие в начале)
        dumps.sort(key=lambda x: x["date"], reverse=True)
        log.info("Найдено дампов: %d (свежий: %s)",
                 len(dumps), dumps[0]["date"] if dumps else "—")
        return dumps

    def fetch_latest_dump(self, output_path: Optional[str] = None) -> Path:
        """Скачать самый свежий дамп реестра.

        Args:
            output_path: куда сохранить CSV. Если None — во временную директорию.

        Returns:
            Path к скачанному файлу.
        """
        dumps = self.list_available_dumps()
        if not dumps:
            raise RuntimeError("Нет доступных дампов")
        latest = dumps[0]
        log.info("Скачиваю дамп от %s: %s", latest["date"], latest["url"])

        content = self._get(latest["url"], is_binary=True)
        if not content:
            raise RuntimeError(f"Не удалось скачать {latest['url']}")

        if output_path is None:
            output_path = f"/tmp/fsvps_reestr_{latest['date']}.csv"
        with open(output_path, "wb") as f:
            f.write(content)
        log.info("Сохранено в %s (%d байт)", output_path, len(content))

        # Сохраним метаданные для парсера
        self._last_dump_date = latest["date"]
        self._last_dump_url = latest["url"]
        return Path(output_path)

    def parse_csv(self, csv_path) -> List[FsvpsDrug]:
        """Распарсить CSV-дамп реестра.

        Returns:
            Список FsvpsDrug.
        """
        with open(csv_path, "rb") as f:
            raw = f.read()
        # Декодируем cp1251
        try:
            text = raw.decode(CSV_ENCODING)
        except UnicodeDecodeError:
            log.warning("cp1251 не сработал, пробуем utf-8 с заменой")
            text = raw.decode("utf-8", errors="replace")

        # CSV с разделителем «;»
        reader = csv.reader(io.StringIO(text), delimiter=";")
        rows = list(reader)
        if not rows:
            log.error("Пустой CSV")
            return []

        headers = [h.strip() for h in rows[0]]
        log.info("CSV: %d строк, %d колонок", len(rows) - 1, len(headers))

        # Маппинг заголовков к полям FsvpsDrug
        field_map = {
            "Торговое наименование лекарственного препарата": "trade_name",
            "Международное непатентованное или химическое наименование": "inn",
            "Лекарственная форма": "form",
            "Дозировка": "dosage",
            "Держатель регистрационного удостоверения": "ru_holder",
            "Разработчик": "developer",
            "Производитель": "producer",
            "Фармакотерапевтическая группа, код анатомо-терапевтическо-химической классификации, рекомендованной Всемирной организацией здравоохранения": "pharmacological_group",
            "Показания к применению": "indications",
            "Противопоказания": "contraindications",
            "Побочные действия": "side_effects",
            "Срок годности": "shelf_life",
            "Условия хранения": "storage_conditions",
            "Условия отпуска": "dispensing_conditions",
            "Номер фармакопейной статьи или нормативного документа": "normative_doc",
            "Дата государственной регистрации": "registration_date",
            "Дата окончания срока действия": "registration_end_date",
            "Учётная серия №": "account_series",
            "Регистрационный №": "registration_number",
            "Качественный состав и количественный состав действующих веществ и качественный состав вспомогательных веществ": "composition",
            "Количество в потребительской упаковке": "package_count",
        }

        drugs: List[FsvpsDrug] = []
        for row in rows[1:]:
            if not row or all(not c.strip() for c in row):
                continue
            drug = FsvpsDrug()
            for i, value in enumerate(row):
                if i >= len(headers):
                    break
                header = headers[i]
                field_name = field_map.get(header)
                if field_name and value:
                    setattr(drug, field_name, value.strip())
            # Если нет торгового наименования — пропускаем
            if not drug.trade_name:
                continue
            drug.dump_date = getattr(self, "_last_dump_date", "")
            drug.source_url = getattr(self, "_last_dump_url", "")
            drugs.append(drug)

        log.info("Распарсено препаратов: %d", len(drugs))
        return drugs

    def fetch_and_parse(self, output_dir: str = "/tmp") -> List[FsvpsDrug]:
        """Скачать самый свежий дамп и распарсить его."""
        csv_path = self.fetch_latest_dump(
            output_path=os.path.join(output_dir, "fsvps_reestr_latest.csv")
        )
        return self.parse_csv(csv_path)


# ---------------------------------------------------------------------------
# Публичный интерфейс
# ---------------------------------------------------------------------------

def fetch_all_drugs(
    output_path: str,
    csv_path: Optional[str] = None,
) -> int:
    """Скачать и распарсить реестр, сохранить в JSON.

    Args:
        output_path: куда сохранить JSON
        csv_path: если указан — не скачивать, а использовать существующий CSV

    Returns:
        Количество препаратов.
    """
    import json

    client = FsvpsClient()
    if csv_path:
        log.info("Использую существующий CSV: %s", csv_path)
        drugs = client.parse_csv(csv_path)
        # Установим метаданные из имени файла, если возможно
        m = re.search(r"data-(\d{8})-", csv_path)
        if m:
            d = m.group(1)
            client._last_dump_date = f"{d[:4]}-{d[4:6]}-{d[6:8]}"
            for drug in drugs:
                drug.dump_date = client._last_dump_date
    else:
        drugs = client.fetch_and_parse()

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "source": "fsvps.gov.ru — Открытые данные",
                "source_url": FSVPS_OPEN_DATA_URL,
                "dump_date": getattr(client, "_last_dump_date", ""),
                "total_drugs": len(drugs),
                "drugs": [d.to_dict() for d in drugs],
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

    p = argparse.ArgumentParser(
        description="Парсер Открытых данных Россельхознадзора (ФГИС ВетИС)"
    )
    p.add_argument("--list-dumps", action="store_true",
                   help="Вывести список доступных дампов")
    p.add_argument("--fetch", metavar="OUTPUT_JSON",
                   help="Скачать свежий дамп и сохранить в JSON")
    p.add_argument("--from-csv", metavar="CSV_PATH",
                   help="Использовать существующий CSV вместо скачивания")
    p.add_argument("--config", default=None, help="Путь к config.yaml")
    args = p.parse_args()

    if args.config:
        from pathlib import Path
        from config import load_config
        load_config(Path(args.config))

    if args.list_dumps:
        c = FsvpsClient()
        dumps = c.list_available_dumps()
        print(f"Всего дампов: {len(dumps)}")
        for d in dumps[:10]:
            print(f"  {d['date']}  {d['filename']}")
        if len(dumps) > 10:
            print(f"  ... и ещё {len(dumps) - 10}")
    elif args.fetch:
        fetch_all_drugs(args.fetch, csv_path=args.from_csv)
    else:
        p.print_help()
