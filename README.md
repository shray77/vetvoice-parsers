# VetVoice Parsers & Validator

Парсеры ветеринарных ресурсов и валидатор базы препаратов **vetvoice** (Flutter-приложение из репы [`shray77/vetvoice`](https://gitlab.com/shray77/vetvoice)) с учётом **видовой разницы доз**.

## Что это

Скрипты собирают данные из открытых ветеринарных источников, сравнивают их с базой `drugs_calc.json` (2401 препарат) и исправляют найденные расхождения:
- **Дозировки** — глобальные и по видам животных (Собаки/Кошки/КРС/МРС/Свиньи/Лошади/Птица/Кролики/Пушные/Пчёлы)
- **Побочные действия**
- **Противопоказания** — беременность, лактация, молодой/пожилой возраст
- **Каренция** (withdrawal days)
- **Соответствие МНН и лекарственной формы**

## ⭐ Видовая разница доз (главная фича)

Дозировка одного и того же препарата **сильно зависит от вида животного**. Например:
- Альбендазол: собаки 25-50 мг/кг, кошки 25 мг/кг, КРС 7.5 мг/кг, МРС 5 мг/кг
- Амоксициллин: собаки 10-20 мг/кг, КРС 7 мг/кг, птица 50-100 мг/кг

В `drugs_calc.json` у каждого препарата есть:
- `dose_per_kg` — глобальная доза (часто одна на все виды, что неправильно)
- `animals` — список применимых видов
- `animal_specific` — словарь доз по видам:
  ```json
  "animal_specific": {
    "Собаки": { "dose_per_kg": 2, "method": "Перорально", "frequency": "..." },
    "Кошки":  { "dose_per_kg": 1, "method": "подкожно", "frequency": "..." }
  }
  ```

Валидатор:
1. Парсит дозы по животным из vetprotocol/vetlek
2. Сравнивает с `animal_specific` в vetvoice
3. **Заполняет пробелы** — если vetvoice.animals содержит вид, но в animal_specific его нет → добавляет
4. **Помечает расхождения** — если доза в vetvoice отличается от источника >30% → warning (без перезаписи, ручная проверка)
5. **Флагирует подозрительные препараты** — если указано 2+ видов, но dose_per_kg один на все

## Конфигурация (config.yaml)

Все настройки в одном файле `config.yaml` в корне. Можно переопределить через env vars с префиксом `VETVOICE_`:

```bash
# Увеличить задержку для vetprotocol
export VETVOICE_VETPROTOCOL_DELAY=2.0

# Изменить User-Agent
export VETVOICE_GLOBAL_USER_AGENT="MyBot/1.0 (contact: me@example.com)"

# Изменить tolerance для сравнения доз
export VETVOICE_VALIDATOR_DOSE_TOLERANCE=0.20
```

### Этичный парсинг (чтобы не забанили)

В config.yaml включены по умолчанию:
- ✅ **Честный User-Agent** с указанием проекта и контакта (НЕ маскируемся под браузер)
- ✅ **robots.txt compliance** — парсер проверяет `/robots.txt` перед запросами
- ✅ **Rate limiting** — `delay: 0.6-2.0` сек между запросами (см. по источникам)
- ✅ **Retry с экспоненциальной задержкой** для 5xx ошибок
- ✅ **429 Too Many Requests handling** — ждём 60-600 сек и продолжаем
- ✅ **403 Forbidden detection** — останавливаемся, если сайт начал банить
- ✅ **Промежуточное сохранение** каждые 10-20 записей (не теряем прогресс при падении)

### Задержки по источникам

| Источник | Delay | Req/sec | Комментарий |
|----------|-------|---------|-------------|
| vetprotocol.ru | 0.6 с | ~1.6 | Лояльный сайт |
| vetlek.ru | 0.7 с | ~1.4 | Старый Apache, не нагружаем |
| vidal.ru | 2.0 с | ~0.5 | Строгий к ботам |
| galen.vetrf.ru | 1.0 с | ~1.0 | Государственный SOAP |
| reestrinform.ru | 2.0 с | ~0.5 | Cloudflare-защита |

## Структура

```
vetvoice-parsers/
├── config.yaml                 # ⭐ Все настройки (rate-limiting, UA, robots.txt)
├── README.md
├── LICENSE
├── .gitignore
├── examples/                   # Образцы данных для демонстрации
│   ├── vetprotocol_sample.json
│   ├── vetlek_sample.json
│   ├── validation_report_sample.json
│   └── drugs_calc_changes_sample.json
└── scripts/
    ├── config.py               # ⭐ Общий модуль конфигурации (dataclasses + YAML + env)
    ├── parsers/
    │   ├── __init__.py
    │   ├── galen_parser.py     # SOAP-клиент к Гален (ФГИС ВетИС)
    │   ├── vetprotocol_parser.py
    │   ├── vetlek_parser.py    # windows-1251
    │   ├── vidal_parser.py
    │   └── reestrinform_parser.py  # зеркало реестра Гален (Cloudflare-блокировка)
    ├── validate_vetvoice.py    # ⭐ Валидатор с видовой разницей доз
    ├── run_pipeline.py         # Оркестратор полного цикла
    └── download_vetvoice_jsons.sh
```

## Установка

```bash
pip install requests beautifulsoup4 lxml pyyaml
```

## Использование

### 1. Скачать базу vetvoice из GitLab

```bash
git clone https://gitlab.com/shray77/vetvoice.git
```

### 2. Проверить доступность Галена

```bash
python3 scripts/parsers/galen_parser.py --check
# WSDL доступен:        True
# Endpoint доступен:    False  ← нужно VETRF_API_USER/VETRF_API_KEY
```

### 3. Скачать препараты с vetprotocol.ru

```bash
# Все 434 препарата (с лимитом по умолчанию из config.yaml)
python3 scripts/parsers/vetprotocol_parser.py --fetch-all vetprotocol.json

# С лимитом
python3 scripts/parsers/vetprotocol_parser.py --fetch-all vetprotocol.json --max 100

# Переопределить delay
python3 scripts/parsers/vetprotocol_parser.py --fetch-all vetprotocol.json --delay 2.0

# Один препарат (для отладки)
python3 scripts/parsers/vetprotocol_parser.py --fetch albendazol
```

### 4. Скачать инструкции с vetlek.ru

```bash
python3 scripts/parsers/vetlek_parser.py --fetch-all vetlek.json --max 200
```

### 5. Валидировать базу vetvoice

```bash
python3 scripts/validate_vetvoice.py \
  --drugs-calc path/to/drugs_calc.json \
  --vetprotocol vetprotocol.json \
  --vetlek vetlek.json \
  --output validation_report.json \
  --apply-fixes drugs_calc_fixed.json
```

### 6. Полный pipeline одной командой

```bash
python3 scripts/run_pipeline.py \
  --drugs-calc path/to/drugs_calc.json \
  --output-dir output/ \
  --max-vetprotocol 434 \
  --max-vetlek 200
```

## Что проверяет валидатор

| Поле | Тип проверки | Severity | Применяет fix? |
|------|--------------|----------|----------------|
| `dose_per_kg` | vetvoice пусто → применяем из источника | warning | ✅ |
| `dose_per_kg` | расхождение >30% | warning | ❌ (ручная проверка) |
| `dose_per_kg` | доза >100 мг/кг (нереалистичная) | info | ❌ |
| **`animal_specific.{animal}.dose_per_kg`** | **⭐ видовая разница: vetvoice пусто → заполняем** | **warning** | **✅** |
| **`animal_specific.{animal}`** | **⭐ вид отсутствует в animal_specific, но есть в animals → добавляем** | **warning** | **✅** |
| **`animal_specific.missing`** | **⭐ 2+ вида, но dose_per_kg один → флаг** | **warning** | ❌ |
| `side_effects` | vetvoice пусто → заполняем | warning | ✅ |
| `contraindications.pregnancy` | vetlek прямо указывает противопоказание | **error** | ✅ |
| `contraindications.lactation` | vetlek прямо указывает противопоказание | **error** | ✅ |
| `withdrawal_days` | извлечение каренции из vetlek.special_notes | warning | ✅ |

## Безопасность исправлений

Валидатор применяет только безопасные исправления:
1. **Заполняет пустые поля** (`dose_per_kg=0/None`, `side_effects=[]`, пустые `animal_specific`)
2. **Не перезаписывает существующие значения** — расхождения помечаются в отчёте, но не применяются автоматически
3. **Фильтр реалистичности**: дозы >100 мг/кг не применяются (обычно это разовая доза, а не мг/кг)
4. **Логирует все исправления** в `metadata.corrections` в `drugs_calc.json`
5. **Инкрементирует версию** (7.4 → 7.5) при apply-fixes

## Схема drugs_calc.json (краткая)

```jsonc
{
  "version": "7.4",
  "total_drugs": 2401,
  "drugs_calc": [
    {
      "id": 1,
      "name": "Мариния®",
      "inn": "маропитант",
      "form": "Раствор для инъекций",
      "form_type": "injection",
      "concentration": 5.0,
      "concentration_unit": "мг/мл",
      "dose_per_kg": 1,        // глобальная доза
      "dose_min": 0.8,
      "dose_max": 1.2,
      "dose_unit": "мг/кг",
      "animals": ["Собаки"],
      "method": "внутримышечно",
      "frequency": "1 раз в день",
      "course_days": "до 5 дней",
      "withdrawal_days": 0,
      "contraindications": {
        "warnings": ["Гиперчувствительность к препарату."],
        "pregnancy": false,
        "lactation": false,
        "young": false,
        "old": false
      },
      "side_effects": ["Боль в месте инъекции", "Диарея"],
      "category": "НПВС / Анальгетики",
      "indications": "Применяют для предотвращения рвоты различного генеза у собак и кошек",
      "animal_specific": {              // ⭐ видовая разница доз
        "Собаки": {
          "dose_per_kg": 2,
          "dose_min": 1.6,
          "dose_max": 2.4,
          "dose_unit": "мг/кг",
          "method": "Перорально",
          "frequency": "1 раз в день",
          "notes": "Серемо, противорвотное"
        },
        "Кошки": {
          "dose_per_kg": 1,
          "dose_min": 0.5,
          "dose_max": 1,
          "method": "подкожно",
          "frequency": "1 раз в сутки"
        }
      }
    }
  ]
}
```

## Результаты тестового прогона

На выборке из 100 препаратов vetprotocol + 49 инструкций vetlek:
- **Проверено**: 2401 препарат
- **Совпало с источниками**: 294 (12%)
- **Найдено расхождений**: 383
  - 178 по `dose_per_kg` (глобальная доза)
  - **133 по `animal_specific.Собаки.dose_per_kg`** ⭐
  - **10 по `animal_specific.Свиньи.dose_per_kg`** ⭐
  - **4 по `animal_specific.Птица.dose_per_kg`** ⭐
  - **30 `animals.missing.Собаки`** — vetprotocol знает дозу, в vetvoice вида нет
  - **14 `animals.missing.Кролики`**
  - 4 `animal_specific.missing` — несколько видов, но доза одна
  - 3 `contraindications.pregnancy` (error)
  - 1 `contraindications.lactation` (error)
- **Применено исправлений**: 9 (7 препаратов)
- **Исправленная база**: версия 7.4 → 7.5

## Замечания

- **Гален**: для прямого доступа к SOAP-сервису `Exportcenter` нужна регистрация хозяйствующего субъекта в ФГИС ВетИС. Без учётки парсер только проверяет доступность WSDL. Если есть учётка — установите `VETRF_API_USER` / `VETRF_API_KEY` и запустите `python3 scripts/parsers/galen_parser.py --fetch galen_registry.json`.
- **reestrinform.ru**: публичное зеркало реестра Гален, но блокируется Cloudflare Browser-Check. Для обхода нужен playwright или прокси-сервис (см. `config.yaml: reestrinform.use_playwright`).
- **vetlek.ru**: кодировка windows-1251, парсер автоматически переключается.
- **drugs_calc_fixed.json**: при применении исправлений версия автоматически инкрементируется, в `metadata.corrections` добавляется запись о применённых правках.

## Лицензия

MIT — делай что хочешь, но без гарантий. Перед применением исправленных дозировок в реальной практике — консультируйся с ветврачом.
