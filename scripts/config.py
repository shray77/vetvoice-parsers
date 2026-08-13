"""
Общий модуль конфигурации для всех парсеров VetVoice.

Читает config.yaml из корня проекта, поддерживает переопределение
через переменные окружения с префиксом VETVOICE_.

Использование:
    from config import get_config
    cfg = get_config()
    delay = cfg.vetprotocol.delay
    ua = cfg.global_config.user_agent
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml


log = logging.getLogger("vetvoice_config")


# ---------------------------------------------------------------------------
# Dataclasses (типизированный доступ)
# ---------------------------------------------------------------------------

@dataclass
class GlobalConfig:
    # Ротация браузерных User-Agent (сайты банят по ботоподобным UA).
    # При каждом запросе случайно выбирается один из списка.
    user_agents: List[str] = field(default_factory=lambda: [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
        "(KHTML, like Gecko) Version/17.4.1 Safari/605.1.15",
        "Mozilla/5.0 (X11; Linux x86_64; rv:124.0) Gecko/20100101 Firefox/124.0",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:124.0) Gecko/20100101 "
        "Firefox/124.0",
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    ])
    # Если задан один user_agent — приоритетнее списка (для отладки).
    user_agent: Optional[str] = None
    headers: Dict[str, str] = field(default_factory=lambda: {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
                  "image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Cache-Control": "max-age=0",
        "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", '
                     '"Not-A.Brand";v="99"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
    })
    timeout: int = 30
    max_retries: int = 3
    backoff_factor: float = 1.5
    retry_on_status: List[int] = field(default_factory=lambda: [500, 502, 503, 504])
    respect_robots_txt: bool = True
    cache_enabled: bool = True
    cache_dir: str = ".cache"
    cache_ttl_hours: int = 24
    log_level: str = "INFO"
    log_format: str = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"

    def get_user_agent(self) -> str:
        """Вернуть User-Agent для текущего запроса.

        Если задан self.user_agent — он приоритетнее (для отладки).
        Иначе случайно выбираем из списка user_agents.
        """
        import random
        if self.user_agent:
            return self.user_agent
        if self.user_agents:
            return random.choice(self.user_agents)
        return "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"


@dataclass
class GalenConfig:
    enabled: bool = True
    endpoint: str = (
        "https://api.vetrf.ru/platform/exportcenter/services/2.3/FMPRegistryService"
    )
    wsdl: str = (
        "https://api.vetrf.ru/schema/platform/exportcenter/"
        "v2.3b-20240808/FMPRegistryService_v2.3.wsdl"
    )
    api_user_env: str = "VETRF_API_USER"
    api_key_env: str = "VETRF_API_KEY"
    page_size: int = 1000
    delay: float = 1.0
    max_entries: Optional[int] = None


@dataclass
class VetprotocolConfig:
    enabled: bool = True
    base_url: str = "https://vetprotocol.ru"
    list_url: str = "https://vetprotocol.ru/drug/"
    delay: float = 0.6
    max_drugs: Optional[int] = None
    parse_animals: bool = True
    parse_warnings: bool = True
    on_429_wait_seconds: int = 60


@dataclass
class VetlekConfig:
    enabled: bool = True
    base_url: str = "https://www.vetlek.ru"
    directions_url: str = "https://www.vetlek.ru/directions/"
    delay: float = 0.7
    max_directions: Optional[int] = None
    alphabet: str = "АБВГДЕЖЗИЙКЛМНОПРСТУФХЦЧШЩЭЮЯ"
    encoding: str = "cp1251"
    sections: List[str] = field(default_factory=lambda: [
        "composition", "pharmacology", "indications",
        "contraindications", "side_effects", "dosage",
        "special_notes", "storage",
    ])


@dataclass
class VidalConfig:
    enabled: bool = True
    base_url: str = "https://www.vidal.ru"
    vet_base_url: str = "https://www.vidal.ru/veterinar"
    delay: float = 2.0
    max_drugs: Optional[int] = None
    alphabet: str = "АБВГДЕЖЗИЙКЛМНОПРСТУФХЦЧШЩЭЮЯ"
    max_pages_per_letter: int = 100
    on_403_wait_seconds: int = 300
    on_429_wait_seconds: int = 600


@dataclass
class ReestrinformConfig:
    enabled: bool = False
    base_url: str = "https://reestrinform.ru"
    list_url: str = "https://reestrinform.ru/reestr-veterinarnykh-preparatov-rf.html"
    use_playwright: bool = False
    playwright_fallback: bool = True
    delay: float = 2.0
    max_entries: Optional[int] = None


@dataclass
class ValidatorConfig:
    checks: Dict[str, bool] = field(default_factory=lambda: {
        "dose_per_kg": True,
        "dose_per_kg_by_animal": True,
        "side_effects": True,
        "contraindications_pregnancy": True,
        "contraindications_lactation": True,
        "withdrawal_days": True,
        "form_match": True,
        "inn_match": True,
    })
    dose_tolerance: float = 0.30
    realistic_dose_min: float = 0.001
    realistic_dose_max: float = 100.0
    apply_fixes: bool = True
    apply_severity: List[str] = field(
        default_factory=lambda: ["error", "warning"]
    )
    overwrite_existing: bool = False
    animal_specific_enabled: bool = True
    animals_to_check: List[str] = field(default_factory=lambda: [
        "Собаки", "Кошки", "КРС", "МРС", "Свиньи", "Лошади",
        "Птица", "Кролики", "Пушные звери", "Пчёлы",
    ])
    flag_single_dose_for_multi_animal: bool = True


@dataclass
class VetvoiceConfig:
    drugs_calc_path: Optional[str] = None
    output_dir: str = "download"


@dataclass
class AppConfig:
    global_config: GlobalConfig = field(default_factory=GlobalConfig)
    galen: GalenConfig = field(default_factory=GalenConfig)
    vetprotocol: VetprotocolConfig = field(default_factory=VetprotocolConfig)
    vetlek: VetlekConfig = field(default_factory=VetlekConfig)
    vidal: VidalConfig = field(default_factory=VidalConfig)
    reestrinform: ReestrinformConfig = field(default_factory=ReestrinformConfig)
    validator: ValidatorConfig = field(default_factory=ValidatorConfig)
    vetvoice: VetvoiceConfig = field(default_factory=VetvoiceConfig)


# ---------------------------------------------------------------------------
# Загрузка конфигурации
# ---------------------------------------------------------------------------

# Единый синглтон — конфиг читается один раз, при первом обращении.
_config_instance: Optional[AppConfig] = None
_config_path: Optional[Path] = None


def _find_config_yaml() -> Path:
    """Найти config.yaml: сначала в текущей директории, потом в родительских."""
    # 1. Явно указанный через env
    env_path = os.environ.get("VETVOICE_CONFIG_PATH")
    if env_path and Path(env_path).exists():
        return Path(env_path)

    # 2. Поиск вверх по дереву директорий
    current = Path(__file__).resolve().parent
    for _ in range(5):
        candidate = current / "config.yaml"
        if candidate.exists():
            return candidate
        current = current.parent

    # 3. Дефолтный путь рядом со scripts/
    return Path(__file__).resolve().parent.parent / "config.yaml"


def _coerce(dataclass_cls, data: dict):
    """Рекурсивно создать dataclass из dict, игнорируя неизвестные ключи."""
    if not isinstance(data, dict):
        return data
    fields = {f.name: f for f in dataclass_cls.__dataclass_fields__.values()}
    kwargs = {}
    for k, v in data.items():
        if k in fields:
            field_obj = fields[k]
            # Если поле — это dataclass, рекурсивно
            if hasattr(field_obj.type, "__dataclass_fields__") and isinstance(v, dict):
                kwargs[k] = _coerce(field_obj.type, v)
            else:
                kwargs[k] = v
    return dataclass_cls(**kwargs)


def _env_override(cfg: AppConfig) -> None:
    """Применить переопределения через env vars с префиксом VETVOICE_.

    Поддерживаются иерархические имена через __:
        VETVOICE_VETPROTOCOL_DELAY=2.0
        VETVOICE_GLOBAL__USER_AGENT="MyBot"
        VETVOICE_VALIDATOR__DOSE_TOLERANCE=0.20
    """
    prefix = "VETVOICE_"
    for env_key, env_val in os.environ.items():
        if not env_key.startswith(prefix):
            continue
        # иерархический путь
        path = env_key[len(prefix):].lower().replace("__", ".")
        parts = path.split(".")
        # маппинг имён секций в dataclass-поля
        section_map = {
            "global": cfg.global_config,
            "galen": cfg.galen,
            "vetprotocol": cfg.vetprotocol,
            "vetlek": cfg.vetlek,
            "vidal": cfg.vidal,
            "reestrinform": cfg.reestrinform,
            "validator": cfg.validator,
            "vetvoice": cfg.vetvoice,
        }
        if len(parts) == 1:
            # короткое имя — ищем в global_config
            target = cfg.global_config
            key = parts[0]
        elif len(parts) == 2:
            target = section_map.get(parts[0])
            key = parts[1]
        else:
            continue
        if target is None:
            continue
        if not hasattr(target, key):
            continue
        # Преобразование типов
        current = getattr(target, key)
        try:
            if isinstance(current, bool):
                setattr(target, key, env_val.lower() in ("1", "true", "yes", "on"))
            elif isinstance(current, int):
                setattr(target, key, int(env_val))
            elif isinstance(current, float):
                setattr(target, key, float(env_val))
            elif isinstance(current, list):
                setattr(target, key, [s.strip() for s in env_val.split(",")])
            else:
                setattr(target, key, env_val)
            log.debug("Env override: %s = %r", env_key, env_val)
        except ValueError:
            log.warning("Cannot parse env %s=%r as %s", env_key, env_val, type(current))


def load_config(config_path: Optional[Path] = None) -> AppConfig:
    """Загрузить конфиг из YAML. Кешируется."""
    global _config_instance, _config_path

    if _config_instance is not None and config_path is None:
        return _config_instance

    path = config_path or _find_config_yaml()
    _config_path = path

    cfg = AppConfig()
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = yaml.safe_load(f) or {}
        except Exception as e:
            log.error("Failed to read %s: %s", path, e)
            raw = {}

        # маппинг ключей yaml в dataclass-секции
        section_map = {
            "global": (cfg.global_config, GlobalConfig),
            "galen": (cfg.galen, GalenConfig),
            "vetprotocol": (cfg.vetprotocol, VetprotocolConfig),
            "vetlek": (cfg.vetlek, VetlekConfig),
            "vidal": (cfg.vidal, VidalConfig),
            "reestrinform": (cfg.reestrinform, ReestrinformConfig),
            "validator": (cfg.validator, ValidatorConfig),
            "vetvoice": (cfg.vetvoice, VetvoiceConfig),
        }
        for key, (target, cls) in section_map.items():
            if key in raw:
                updated = _coerce(cls, raw[key])
                for fname in cls.__dataclass_fields__:
                    setattr(target, fname, getattr(updated, fname))
    else:
        log.warning("config.yaml not found at %s, using defaults", path)

    # Применить env overrides
    _env_override(cfg)

    # Настроить логирование
    logging.basicConfig(
        level=getattr(logging, cfg.global_config.log_level.upper(), logging.INFO),
        format=cfg.global_config.log_format,
        force=False,  # не перетирать уже настроенные логгеры
    )

    _config_instance = cfg
    log.info("Config loaded from %s", path)
    return cfg


def get_config() -> AppConfig:
    """Получить синглтон-конфиг."""
    if _config_instance is None:
        load_config()
    return _config_instance


def get_config_path() -> Optional[Path]:
    """Вернуть путь к загруженному config.yaml."""
    return _config_path


# ---------------------------------------------------------------------------
# HTTP-сессия с rate-limiting, retries и robots.txt compliance
# ---------------------------------------------------------------------------

class RobotsTxtCache:
    """Простой кеш robots.txt — чтобы не запрашивать его каждый раз."""
    _cache: Dict[str, Any] = {}  # domain -> (fetched_at, parser)

    @classmethod
    def get(cls, base_url: str, session):
        from urllib.parse import urlparse
        from urllib.robotparser import RobotFileParser
        domain = urlparse(base_url).netloc
        now = time.time()
        if domain in cls._cache:
            fetched_at, parser = cls._cache[domain]
            if now - fetched_at < 3600:  # час
                return parser
        # Запросить
        parser = RobotFileParser()
        robots_url = f"{base_url.rstrip('/')}/robots.txt"
        try:
            r = session.get(robots_url, timeout=10)
            if r.status_code == 200:
                parser.parse(r.text.splitlines())
            else:
                # Если robots.txt нет — всё разрешено
                parser.parse([])
        except Exception:
            parser.parse([])
        cls._cache[domain] = (now, parser)
        return parser


def make_session(
    cfg: AppConfig,
    source_name: str = "",
) -> "requests.Session":
    """Создать requests.Session с настройками из конфига.

    Включает:
      * Браузерный User-Agent (случайный из списка)
      * Полный набор браузерных заголовков (sec-ch-ua, Sec-Fetch-*, и т.д.)
      * Retry стратегия для 5xx
    """
    import requests
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry

    session = requests.Session()
    # Случайный UA при создании сессии
    session.headers.update({"User-Agent": cfg.global_config.get_user_agent()})
    session.headers.update(cfg.global_config.headers)

    # Retry стратегия
    retry = Retry(
        total=cfg.global_config.max_retries,
        backoff_factor=cfg.global_config.backoff_factor,
        status_forcelist=cfg.global_config.retry_on_status,
        allowed_methods=["GET", "POST", "HEAD"],
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)

    return session


def rotate_user_agent(session, cfg: AppConfig) -> str:
    """Поменять User-Agent в существующей сессии (для ротации между запросами).

    Возвращает новый UA.
    """
    new_ua = cfg.global_config.get_user_agent()
    session.headers["User-Agent"] = new_ua
    return new_ua


def can_fetch(
    cfg: AppConfig,
    session,
    url: str,
    user_agent: Optional[str] = None,
) -> bool:
    """Проверить, разрешён ли URL согласно robots.txt."""
    if not cfg.global_config.respect_robots_txt:
        return True
    from urllib.parse import urlparse
    parsed = urlparse(url)
    base_url = f"{parsed.scheme}://{parsed.netloc}"
    parser = RobotsTxtCache.get(base_url, session)
    ua = user_agent or cfg.global_config.user_agent
    return parser.can_fetch(ua, url)


# ---------------------------------------------------------------------------
# Декоратор для rate-limiting между запросами
# ---------------------------------------------------------------------------

class RateLimiter:
    """Простой rate-limiter: держит паузу между запросами."""
    def __init__(self, delay: float):
        self.delay = delay
        self._last_request = 0.0

    def wait(self) -> None:
        elapsed = time.time() - self._last_request
        if elapsed < self.delay:
            time.sleep(self.delay - elapsed)
        self._last_request = time.time()
