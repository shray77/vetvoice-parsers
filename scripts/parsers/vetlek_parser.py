"""
Парсер vetlek.ru — ветеринарная аптека с инструкциями к препаратам.

Структура сайта:
  * https://www.vetlek.ru/directions/         — список инструкций (алфавитный)
  * https://www.vetlek.ru/directions/?id=X     — страница инструкции
  * https://www.vetlek.ru/shop/?gid=X          — товар в магазине

Кодировка сайта: windows-1251.

На каждой странице инструкции есть разделы:
  * Состав
  * Фармакологические свойства
  * Показания к применению
  * Дозы и способ применения
  * Побочные действия
  * Противопоказания
  * Особые указания
  * Условия хранения

Эти данные используются для валидации drugs_calc.json: дозировки,
побочные действия, противопоказания, состав.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field, asdict
from typing import Iterator, List, Optional, Dict
from urllib.parse import urljoin, parse_qs, urlparse

import requests
from bs4 import BeautifulSoup

log = logging.getLogger("vetlek_parser")
if not log.handlers:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


BASE_URL = "https://www.vetlek.ru"
DIRECTIONS_URL = f"{BASE_URL}/directions/"

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
}
DEFAULT_TIMEOUT = 30
DEFAULT_DELAY = 0.5


# ---------------------------------------------------------------------------
# Модели
# ---------------------------------------------------------------------------

@dataclass
class VetlekInstruction:
    """Инструкция с vetlek.ru."""
    direction_id: str = ""
    title: str = ""
    url: str = ""
    composition: str = ""
    pharmacology: str = ""
    indications: str = ""
    dosage: str = ""
    contraindications: str = ""
    side_effects: str = ""
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

class VetlekClient:
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
            # vetlek отдаёт windows-1251
            if r.encoding and r.encoding.lower() in ("windows-1251", "cp1251", "iso-8859-1"):
                r.encoding = "cp1251"
            return r.text
        except Exception as e:
            log.warning("GET %s failed: %s", url, e)
            return None

    # ---------------------- публичные методы -------------------------- #

    def list_direction_ids(self) -> List[str]:
        """Получить все ID инструкций со страницы /directions/.

        vetlek отдаёт алфавитный указатель по буквам через ?char=X (cp1251).
        Проходим по всем буквам и собираем все ?id=N ссылки.
        """
        all_ids: List[str] = []
        # Главная страница + все буквы алфавита
        urls = [DIRECTIONS_URL]
        for ch in "АБВГДЕЖЗИЙКЛМНОПРСТУФХЦЧШЩЭЮЯ":
            enc = ch.encode("cp1251").hex().upper()
            enc_pct = "%" + "%".join(enc[i:i+2] for i in range(0, len(enc), 2))
            urls.append(f"{DIRECTIONS_URL}?char={enc_pct}")

        for url in urls:
            html = self._get(url)
            if not html:
                continue
            soup = BeautifulSoup(html, "html.parser")
            for a in soup.find_all("a", href=True):
                href = a["href"]
                # Ссылки бывают как /directions/?id=N, так и просто ?id=N
                m = re.search(r"(?:/directions/)?\?id=(\d+)", href)
                if m:
                    # отсеиваем char= и другие параметры — берём только чистые id
                    if "char=" not in href:
                        all_ids.append(m.group(1))
            time.sleep(self.delay)

        # дедуп, сохраняя порядок
        seen = set()
        unique = []
        for i in all_ids:
            if i not in seen:
                seen.add(i)
                unique.append(i)
        log.info("Найдено %d уникальных инструкций на vetlek.ru", len(unique))
        return unique

    def fetch_direction(self, direction_id: str) -> Optional[VetlekInstruction]:
        url = f"{DIRECTIONS_URL}?id={direction_id}"
        html = self._get(url)
        if not html:
            return None
        return _parse_direction_page(html, direction_id, url)

    def iter_all_directions(
        self, max_directions: Optional[int] = None
    ) -> Iterator[VetlekInstruction]:
        ids = self.list_direction_ids()
        if max_directions:
            ids = ids[:max_directions]
        for i, did in enumerate(ids, 1):
            log.info("[%d/%d] direction %s", i, len(ids), did)
            instr = self.fetch_direction(did)
            if instr:
                yield instr
            time.sleep(self.delay)


# ---------------------------------------------------------------------------
# Парсинг страницы инструкции
# ---------------------------------------------------------------------------

# Разделы в инструкциях vetlek — типовые заголовки.
# vetlek использует формат приказов Минсельхоза РФ:
#   I. Общие сведения
#   II. Фармакологические свойства
#   III. Порядок применения
#     11. Показания к применению
#     12. Противопоказания
#     13. Побочные действия
#     14. (что-то ещё)
#     15. Дозы и способ применения
#     16. Особые указания
SECTION_PATTERNS = {
    "composition": [
        r"^(?:I\.?\s+)?Состав(?:\s+препарата)?\s*$",
        r"^(?:I\.?\s+)?Описание\s+и\s+состав\s*$",
        r"^(?:I\.?\s+)?Состав\s+и\s+форма\s+выпуска\s*$",
        r"^I\.\s+Общие\s+сведения\s*$",  # блок «Общие сведения» часто содержит состав
        r"^Состав\s*:",
    ],
    "pharmacology": [
        r"^II\.\s+Фармакологические\s+свойства\s*$",
        r"^Фармакологические\s+свойства\s*$",
        r"^Фармакологическое\s+действие\s*$",
    ],
    "indications": [
        r"^\d+\.\s+Показания(?:\s+к\s+применению)?\s*$",
        r"^Показания(?:\s+к\s+применению)?\s*$",
        r"^III\.\s+Порядок\s+применения\s*$",  # начало блока «Порядок применения»
        r"^Порядок\s+применения\s*$",
    ],
    "contraindications": [
        r"^\d+\.\s+Противопоказания(?:\s+для\s+применения)?\s*$",
        r"^Противопоказания(?:\s+для\s+применения)?\s*$",
    ],
    "side_effects": [
        r"^\d+\.\s+Побочные\s+действия(?:\s+\(эффекты\))?\s*$",
        r"^\d+\.\s+Побочные\s+эффекты\s*$",
        r"^Побочные\s+(?:действия|эффекты)\s*$",
        r"^\d+\.\s+Осложнения\s*$",
    ],
    "dosage": [
        r"^\d+\.\s+Дозы?\s+и\s+способ\s+применения\s*$",
        r"^\d+\.\s+Способ\s+применения\s+и\s+дозы?\s*$",
        r"^\d+\.\s+Дозировка\s*$",
        r"^Дозы?\s+и\s+способ\s+применения\s*$",
    ],
    "special_notes": [
        r"^\d+\.\s+Особые\s+указания(?:\s+и\s+меры\s+предосторожности)?\s*$",
        r"^Особые\s+указания(?:\s+и\s+меры\s+предосторожности)?\s*$",
        r"^\d+\.\s+Меры\s+предосторожности\s*$",
    ],
    "storage": [
        r"^\d+\.\s+Условия\s+хранения(?:\s+\(сроки?\))?\s*$",
        r"^Условия\s+хранения\s*$",
        r"^\d+\.\s+Хранение\s*$",
    ],
}

# Компилируем
SECTION_RES = {
    field: [re.compile(p, re.I | re.M) for p in pats]
    for field, pats in SECTION_PATTERNS.items()
}


def _detect_section(line: str) -> Optional[str]:
    """Определить, является ли строка заголовком секции.

    vetlek размещает заголовок и текст параграфа в одной строке:
        «12. Противопоказанием к применению является...»
        «16. При использовании лекарственного препарата Левоксидин
           согласно настоящей инструкции побочных явлений...»

    Поэтому проверяем, не начинается ли строка с номера и ключевого слова.
    Дополнительно проверяем вхождение ключевых слов в первых 200 символах
    (для строк вроде «При использовании ... побочных явлений ... не наблюдается»).
    """
    line_stripped = line.strip()
    if not line_stripped:
        return None
    # Снимаем номер в начале: «12. », «III. », «I. »
    m = re.match(r"^(?:[IVXLC]+|\d+)\.\s+(.+)$", line_stripped)
    tail = m.group(1).strip() if m else line_stripped
    tail_no_punct = tail.rstrip(":.").strip()

    # 1) Полное совпадение с заголовком (для коротких строк-заголовков)
    if len(tail_no_punct) < 80:
        for field, res in SECTION_RES.items():
            for r in res:
                if r.match(tail_no_punct + "\n") or r.search(tail_no_punct):
                    return field

    # 2) Начинается с ключевого слова секции (для строк с параграфом)
    head = tail_no_punct[:80].lower()
    # Контекст — первые 300 символов для гибкого поиска
    ctx = tail_no_punct[:300].lower()

    section_starts = [
        ("contraindications", ["противопоказани"]),
        ("side_effects", ["побочные действи", "побочные эффект",
                          "побочных явлений", "побочных реакций",
                          "осложнени"]),
        ("dosage", ["дозы и способ применения", "способ применения и дозы",
                    "дозировка", "применяют наружно", "применяют внутримышечно",
                    "применяют внутривенно", "применяют подкожно",
                    "применяют перорально", "применяют внутрь"]),
        ("indications", ["показани", "применяют собакам", "применяют кошкам",
                         "применяют крупному", "применяют мелкому",
                         "применяют свин", "применяют лошад"]),
        ("special_notes", ["особые указания", "особенности действия",
                           "при работе с препаратом", "следует избегать",
                           "применение не исключает", "не предназначен",
                           "передозировк", "взаимодейств",
                           "особенностей действия"]),
        ("storage", ["условия хранения", "срок годности"]),
        ("composition", ["наименование лекарственного",
                         "лекарственная форма", "состав"]),
        ("pharmacology", ["фармакологическ", "относится к фармакотерапевтической",
                              "относится к группе"]),
    ]
    for field, starts in section_starts:
        for s in starts:
            if head.startswith(s):
                return field

    # 3) Для side_effects: иногда заголовок начинается с «При использовании ...»
    #    или «В случае ... побочных ...», проверяем вхождение в контекст
    side_effect_hints = [
        "побочных явлений", "побочных действий", "побочных эффектов",
        "побочных реакций", "не наблюдается",
    ]
    if any(h in ctx for h in side_effect_hints) and "применяют" not in head:
        return "side_effects"

    dosage_hints = [
        "применяют наружно", "применяют внутримышечно",
        "применяют внутривенно", "применяют подкожно",
        "применяют перорально", "применяют внутрь",
        "доза", "дозировк", "разовая доза",
    ]
    if any(h in ctx[:200] for h in dosage_hints) and "побочн" not in ctx[:100]:
        return "dosage"

    contraind_hints = ["противопоказани", "отсутствуют противопоказания"]
    if any(h in ctx[:200] for h in contraind_hints):
        return "contraindications"

    return None


def _parse_direction_page(
    html: str, direction_id: str, url: str
) -> VetlekInstruction:
    instr = VetlekInstruction(direction_id=direction_id, url=url)
    soup = BeautifulSoup(html, "html.parser")

    # Заголовок — ищем <h1> или <title>
    h1 = soup.find("h1")
    if h1:
        title_text = h1.get_text(" ", strip=True)
        # vetlek: "Левоксидин - наставления (инструкции) на сайте VetLek"
        m = re.match(r"^\s*(.+?)\s*-\s*наставления.*$", title_text, re.I)
        if m:
            instr.title = m.group(1).strip()
        else:
            instr.title = title_text

    # Основной контент — обычно в <table> с одним <td>, или в div с классом контента
    content_el = (
        soup.find("div", class_=re.compile("content|instruction|direction", re.I))
        or soup.find("td", class_=re.compile("content|text|main", re.I))
        or soup.find("article")
        or soup.find("main")
    )
    if content_el is None:
        # fallback: ищем самый большой текстовый блок
        biggest = max(
            soup.find_all(["div", "td"]),
            key=lambda el: len(el.get_text(strip=True)),
            default=None,
        )
        content_el = biggest
    if content_el is None:
        return instr

    # Удаляем служебное
    for tag in content_el.find_all(["script", "style", "nav", "footer", "header", "form", "table"]):
        tag.decompose()

    # Заменяем <br/> на переводы строк, <p> и <h*> на двойные
    for br in content_el.find_all("br"):
        br.replace_with("\n")
    for p in content_el.find_all(["p", "h1", "h2", "h3", "h4", "li"]):
        p.append("\n")

    raw_text = content_el.get_text()
    # Нормализуем
    lines = [l.strip() for l in raw_text.split("\n") if l.strip()]
    instr.raw_text = "\n".join(lines)

    # Проходим по строкам, находим секции и собираем их содержимое
    current_section: Optional[str] = None
    buffer: Dict[str, List[str]] = {k: [] for k in SECTION_PATTERNS}

    for line in lines:
        sec = _detect_section(line)
        if sec:
            current_section = sec
            # Если строка содержит и заголовок и текст параграфа (типично для vetlek),
            # добавляем текст в buffer, не пропуская строку.
            # Снимаем префикс «N. » и сам заголовок оставляем — он часто содержит
            # ключевые слова (например, «Противопоказанием к применению является...»).
            # Поэтому добавляем ВСЮ строку в buffer.
            buffer[sec].append(line)
            continue
        if current_section:
            buffer[current_section].append(line)

    for field, lines_list in buffer.items():
        text = " ".join(lines_list).strip()
        # Очистка от лишних пробелов
        text = re.sub(r"\s+", " ", text)
        setattr(instr, field, text)

    return instr


# ---------------------------------------------------------------------------
# Публичный интерфейс
# ---------------------------------------------------------------------------

def fetch_all_directions(
    output_path: str, max_directions: Optional[int] = None
) -> int:
    """Скачать все инструкции и сохранить в JSON."""
    import json

    client = VetlekClient()
    log.info("Начинаем выгрузку инструкций с vetlek.ru")
    items = []
    for instr in client.iter_all_directions(max_directions=max_directions):
        items.append(instr.to_dict())

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "source": "vetlek.ru",
                "source_url": BASE_URL,
                "total_directions": len(items),
                "directions": items,
            },
            f,
            ensure_ascii=False,
            indent=2,
        )
    log.info("Сохранено %d инструкций в %s", len(items), output_path)
    return len(items)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser(description="Парсер vetlek.ru")
    p.add_argument("--list", action="store_true", help="Вывести список ID инструкций")
    p.add_argument("--fetch", metavar="ID", help="Скачать одну инструкцию по ID")
    p.add_argument("--fetch-all", metavar="OUTPUT_JSON", help="Скачать все инструкции в JSON")
    p.add_argument("--max", type=int, default=None, help="Лимит инструкций")
    args = p.parse_args()

    if args.list:
        c = VetlekClient()
        ids = c.list_direction_ids()
        print(f"Всего ID: {len(ids)}")
        for i in ids[:30]:
            print(f"  {i}")
    elif args.fetch:
        c = VetlekClient()
        d = c.fetch_direction(args.fetch)
        if d:
            for k, v in d.to_dict().items():
                if v:
                    print(f"{k}: {str(v)[:300]}")
        else:
            print("Не найдено")
    elif args.fetch_all:
        fetch_all_directions(args.fetch_all, args.max)
    else:
        p.print_help()
