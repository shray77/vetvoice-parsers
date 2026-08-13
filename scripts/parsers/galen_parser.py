"""
Парсер компонента «Гален» (ФГИС ВетИС, Россельхознадзор).

Гален — государственная информационная система учёта ветеринарных
лекарственных препаратов, фармацевтических субстанций, кормовых добавок
и побочных реакций. Расположена по адресу https://galen.vetrf.ru/.

Доступ к данным реестра осуществляется через SOAP-сервис Exportcenter
(FMPRegistryService v2.3):

    Endpoint: https://api.vetrf.ru/platform/exportcenter/services/2.3/FMPRegistryService
    WSDL:     https://api.vetrf.ru/schema/platform/exportcenter/v2.3b-20240808/FMPRegistryService_v2.3.wsdl

Доступные операции:
  * GetMedicineRegistryEntryList      — список записей реестра ЛС/ФС (постранично)
  * GetRegistryEntryByGuid            — запись по GUID
  * GetFodderRegistryEntryList        — список кормовых добавок

⚠️ Для вызова сервиса требуется API-ключ (регистрация ХС в ВетИС).
   Установите переменные окружения VETRF_API_USER / VETRF_API_KEY
   или передайте их в конструктор GalenClient.

Возвращаемые данные (по каждому препарату):
  * tradeName          — торговое наименование
  * chemicalName       — МНН (международное непатентованное)
  * producer           — производитель
  * developer          — разработчик
  * formOfIssue        — лекарственная форма
  * productPurpose     — назначение
  * componentComposition — компонентный состав
  * pharmaceuticalType — тип (VACCINE / PHARMACEUTICAL / FEED_ADDITIVE / ...)
  * registrationCertificate:
      - issueNumber    — номер РУ
      - beginDate      — дата регистрации
      - endDate        — дата окончания
      - unlimited      — признак бессрочного РУ
      - certificateOwner — держатель РУ
  * registryStatus     — ACTIVE / CANCELLED / SUSPENDED

Эти данные используются для валидации drugs_registry.json / drugs_calc.json:
  * проверка актуальности РУ (не отменено ли),
  * сверка МНН, формы выпуска, производителя,
  * сверка номера РУ и сроков действия.
"""

from __future__ import annotations

import logging
import os
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field, asdict
from typing import Iterator, List, Optional
from urllib.parse import quote

import requests

log = logging.getLogger("galen_parser")
if not log.handlers:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


# ---------------------------------------------------------------------------
# Константы
# ---------------------------------------------------------------------------

GALEN_WEB_URL = "https://galen.vetrf.ru"
GALEN_HELP_URL = "https://help.vetrf.ru/wiki/Компонент_Гален"

# SOAP endpoint для публичного реестра ЛС
FMP_REGISTRY_ENDPOINT = (
    "https://api.vetrf.ru/platform/exportcenter/services/2.3/FMPRegistryService"
)
FMP_REGISTRY_WSDL = (
    "https://api.vetrf.ru/schema/platform/exportcenter/"
    "v2.3b-20240808/FMPRegistryService_v2.3.wsdl"
)

# XML-неймспейсы, используемые в запросах/ответах
NS = {
    "soapenv": "http://schemas.xmlsoap.org/soap/envelope/",
    "ws": "http://api.vetrf.ru/schema/cdm/registry/ws-definitions",
    "bs": "http://api.vetrf.ru/schema/cdm/base",
    "vd": "http://api.vetrf.ru/schema/cdm/registry",
    "dt": "http://api.vetrf.ru/schema/cdm/dictionary",
    "xsi": "http://www.w3.org/2001/XMLSchema-instance",
}

DEFAULT_TIMEOUT = 30
DEFAULT_PAGE_SIZE = 1000  # максимум по спецификации
DEFAULT_DELAY = 0.5  # задержка между запросами, сек


# ---------------------------------------------------------------------------
# Модель данных
# ---------------------------------------------------------------------------

@dataclass
class RegistrationCertificate:
    """Регистрационное удостоверение."""
    issue_number: str = ""
    issue_series: str = ""
    begin_date: str = ""
    end_date: str = ""
    unlimited: bool = False
    certificate_owner: str = ""
    document_type: str = ""  # 25 = свид. о гос. регистрации, 38 = РУ ЛП/ФС


@dataclass
class GalenRegistryEntry:
    """Одна запись реестра ЛС/ФС из Галена."""
    guid: str = ""
    registry_status: str = ""  # ACTIVE / CANCELLED / SUSPENDED / etc.
    trade_name: str = ""
    chemical_name: str = ""  # МНН
    producer: str = ""
    developer: str = ""
    form_of_issue: str = ""
    product_purpose: str = ""
    component_composition: str = ""
    pharmaceutical_type: str = ""  # VACCINE / PHARMACEUTICAL / ...
    certificate: RegistrationCertificate = field(default_factory=RegistrationCertificate)
    raw_xml: str = ""  # сырой XML для отладки

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("raw_xml", None)
        return d


# ---------------------------------------------------------------------------
# SOAP-клиент
# ---------------------------------------------------------------------------

class GalenClient:
    """Клиент к SOAP-сервису FMPRegistryService (Гален).

    Для работы требует авторизацию ВетИС:
      * VETRF_API_USER — логин (обычно API-ключ из ЛК ВетИС.Паспорт)
      * VETRF_API_KEY  — пароль/ключ

    Если учётные данные не заданы, будет работать только WSDL-инспекция
    и пробные запросы (вернут 403, что нормально).
    """

    def __init__(
        self,
        api_user: Optional[str] = None,
        api_key: Optional[str] = None,
        endpoint: str = FMP_REGISTRY_ENDPOINT,
        timeout: int = DEFAULT_TIMEOUT,
        delay: float = DEFAULT_DELAY,
    ):
        self.api_user = api_user or os.environ.get("VETRF_API_USER", "")
        self.api_key = api_key or os.environ.get("VETRF_API_KEY", "")
        self.endpoint = endpoint
        self.timeout = timeout
        self.delay = delay
        self.session = requests.Session()
        # Базовая HTTP Basic auth, если предоставлены учётные данные
        if self.api_user and self.api_key:
            self.session.auth = (self.api_user, self.api_key)
        self.session.headers.update(
            {
                "User-Agent": "VetVoice-GalenParser/1.0",
                "Accept": "text/xml",
            }
        )

    # ---------------------- низкоуровневые методы ---------------------- #

    def _invoke(self, body_xml: str) -> str:
        """Отправляет SOAP-запрос, возвращает тело ответа как строку."""
        envelope = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/">'
            f'<soapenv:Header/>'
            f'<soapenv:Body>{body_xml}</soapenv:Body>'
            f'</soapenv:Envelope>'
        )
        resp = self.session.post(
            self.endpoint,
            data=envelope.encode("utf-8"),
            headers={
                "Content-Type": "text/xml;charset=UTF-8",
                'SOAPAction': '""',
            },
            timeout=self.timeout,
        )
        if resp.status_code == 403:
            raise PermissionError(
                "403 Forbidden — для доступа к Exportcenter требуется "
                "зарегистрироваться в ФГИС ВетИС и установить "
                "VETRF_API_USER / VETRF_API_KEY."
            )
        resp.raise_for_status()
        return resp.text

    # ---------------------- публичные операции ------------------------- #

    def get_medicine_registry_entry_list(
        self,
        count: int = DEFAULT_PAGE_SIZE,
        offset: int = 0,
    ) -> List[GalenRegistryEntry]:
        """GetMedicineRegistryEntryList — получить страницу реестра ЛС."""
        body = (
            '<ws:getMedicineRegistryEntryListRequest '
            'xmlns:ws="http://api.vetrf.ru/schema/cdm/registry/ws-definitions" '
            'xmlns:bs="http://api.vetrf.ru/schema/cdm/base">'
            f'<bs:listOptions><bs:count>{count}</bs:count>'
            f'<bs:offset>{offset}</bs:offset></bs:listOptions>'
            '</ws:getMedicineRegistryEntryListRequest>'
        )
        xml_text = self._invoke(body)
        return _parse_registry_entry_list(xml_text)

    def get_registry_entry_by_guid(self, guid: str) -> GalenRegistryEntry:
        """GetRegistryEntryByGuid — получить запись по GUID."""
        body = (
            '<ws:getRegistryEntryByGuidRequest '
            'xmlns:ws="http://api.vetrf.ru/schema/cdm/registry/ws-definitions" '
            'xmlns:bs="http://api.vetrf.ru/schema/cdm/base">'
            f'<bs:guid>{guid}</bs:guid>'
            '</ws:getRegistryEntryByGuidRequest>'
        )
        xml_text = self._invoke(body)
        entries = _parse_registry_entry_list(xml_text)
        return entries[0] if entries else GalenRegistryEntry()

    def iter_all_entries(
        self,
        page_size: int = DEFAULT_PAGE_SIZE,
        max_entries: Optional[int] = None,
    ) -> Iterator[GalenRegistryEntry]:
        """Итеративно проходить весь реестр постранично."""
        offset = 0
        total_seen = 0
        while True:
            log.info("Fetching page: offset=%d, count=%d", offset, page_size)
            try:
                entries = self.get_medicine_registry_entry_list(
                    count=page_size, offset=offset
                )
            except PermissionError as e:
                log.error("Auth required: %s", e)
                raise
            except Exception as e:
                log.error("Request failed at offset=%d: %s", offset, e)
                time.sleep(self.delay * 4)
                continue

            if not entries:
                log.info("No more entries at offset=%d", offset)
                break

            for e in entries:
                yield e
                total_seen += 1
                if max_entries and total_seen >= max_entries:
                    return

            if len(entries) < page_size:
                log.info("Last page reached (%d entries)", len(entries))
                break

            offset += page_size
            time.sleep(self.delay)

    # ---------------------- тестовые методы ---------------------------- #

    def check_wsdl(self) -> bool:
        """Проверить доступность WSDL (без авторизации)."""
        try:
            r = self.session.get(FMP_REGISTRY_WSDL, timeout=self.timeout)
            return r.status_code == 200 and b"wsdl:" in r.content.lower()
        except Exception as e:
            log.error("WSDL check failed: %s", e)
            return False

    def check_endpoint(self) -> bool:
        """Проверить доступность endpoint (тестовый пустой запрос)."""
        try:
            self._invoke("<x/>")
            return True
        except PermissionError:
            # 403 — endpoint жив, но нужна авторизация
            return False
        except Exception as e:
            log.error("Endpoint check failed: %s", e)
            return False


# ---------------------------------------------------------------------------
# Парсинг SOAP-ответа
# ---------------------------------------------------------------------------

def _text(el: Optional[ET.Element]) -> str:
    """Безопасно извлечь текст из XML-элемента."""
    if el is None:
        return ""
    return (el.text or "").strip()


def _parse_registry_entry(xml_el: ET.Element) -> GalenRegistryEntry:
    """Распарсить один <vd:fmpRegistryEntry>."""
    entry = GalenRegistryEntry()

    # GUID
    guid_el = xml_el.find("bs:guid", NS)
    entry.guid = _text(guid_el)

    # Статус
    status_el = xml_el.find("vd:registryStatus", NS)
    entry.registry_status = _text(status_el)

    # Препарат (vd:fmpProduct)
    product_el = xml_el.find("vd:fmpProduct", NS)
    if product_el is not None:
        entry.trade_name = _text(product_el.find("vd:tradeName", NS))
        entry.chemical_name = _text(product_el.find("vd:chemicalName", NS))
        entry.producer = _text(product_el.find("vd:producer", NS))
        entry.developer = _text(product_el.find("vd:developer", NS))
        entry.form_of_issue = _text(product_el.find("vd:formOfIssue", NS))
        entry.product_purpose = _text(product_el.find("vd:productPurpose", NS))
        entry.component_composition = _text(
            product_el.find("vd:componentComposition", NS)
        )
        entry.pharmaceutical_type = _text(
            product_el.find("vd:pharmaceuticalType", NS)
        )

    # Регистрационное удостоверение
    cert_el = xml_el.find("vd:registrationCertificate", NS)
    if cert_el is not None:
        cert = RegistrationCertificate()
        cert.issue_number = _text(cert_el.find("vd:issueNumber", NS))
        cert.issue_series = _text(cert_el.find("vd:issueSeries", NS))
        cert.begin_date = _text(cert_el.find("vd:beginDate", NS))
        cert.end_date = _text(cert_el.find("vd:endDate", NS))
        unlimited_el = cert_el.find("vd:unlimited", NS)
        cert.unlimited = _text(unlimited_el).lower() in ("true", "1", "yes")
        cert.certificate_owner = _text(cert_el.find("vd:certificateOwner", NS))
        type_el = cert_el.find("vd:type", NS)
        if type_el is not None:
            cert.document_type = _text(type_el.find("dt:name", NS)) or _text(
                type_el
            )
        entry.certificate = cert

    return entry


def _parse_registry_entry_list(xml_text: str) -> List[GalenRegistryEntry]:
    """Распарсить SOAP-ответ с одной страницей реестра."""
    entries: List[GalenRegistryEntry] = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        log.error("XML parse error: %s\nFirst 500 chars: %s", e, xml_text[:500])
        return entries

    # Найти все <vd:fmpRegistryEntry> на любой глубине
    for el in root.iter():
        # Сопоставляем по локальному имени (неймспейс может не разрешиться)
        tag = el.tag.split("}")[-1] if "}" in el.tag else el.tag
        if tag == "fmpRegistryEntry":
            entries.append(_parse_registry_entry(el))
    return entries


# ---------------------------------------------------------------------------
# Публичный интерфейс
# ---------------------------------------------------------------------------

def fetch_full_registry(
    output_path: str,
    max_entries: Optional[int] = None,
    api_user: Optional[str] = None,
    api_key: Optional[str] = None,
) -> int:
    """Скачать весь реестр ЛС в JSON-файл.

    Возвращает количество скачанных записей.
    """
    import json

    client = GalenClient(api_user=api_user, api_key=api_key)

    if not client.check_wsdl():
        log.error("WSDL недоступен. Проверьте сеть.")
        return 0

    log.info("Начинаем выгрузку реестра ЛС из Галена...")
    entries = []
    try:
        for entry in client.iter_all_entries(max_entries=max_entries):
            entries.append(entry.to_dict())
    except PermissionError as e:
        log.error("Доступ запрещён: %s", e)
        log.error(
            "Установите VETRF_API_USER и VETRF_API_KEY для доступа к Exportcenter. "
            "Без учётки парсер не сможет скачать реестр напрямую, "
            "используйте reestrinform_parser как публичное зеркало."
        )
        return 0

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "source": "Гален (ФГИС ВетИС, Россельхознадзор)",
                "endpoint": FMP_REGISTRY_ENDPOINT,
                "total_entries": len(entries),
                "entries": entries,
            },
            f,
            ensure_ascii=False,
            indent=2,
        )
    log.info("Сохранено %d записей в %s", len(entries), output_path)
    return len(entries)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser(description="Парсер реестра ЛС компонента Гален")
    p.add_argument(
        "--check",
        action="store_true",
        help="Проверить доступность WSDL и endpoint",
    )
    p.add_argument(
        "--fetch",
        metavar="OUTPUT_JSON",
        help="Скачать весь реестр в JSON",
    )
    p.add_argument("--max", type=int, default=None, help="Лимит записей")
    p.add_argument("--user", default=None, help="VETRF API user")
    p.add_argument("--key", default=None, help="VETRF API key")
    args = p.parse_args()

    if args.check:
        c = GalenClient(api_user=args.user, api_key=args.key)
        print(f"WSDL доступен:        {c.check_wsdl()}")
        print(f"Endpoint доступен:    {c.check_endpoint()}")
    elif args.fetch:
        fetch_full_registry(args.fetch, args.max, args.user, args.key)
    else:
        p.print_help()
