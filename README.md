# VetVoice Parsers & Validator

Парсеры ветеринарных ресурсов и валидатор базы препаратов **vetvoice** (Flutter-приложение из репы [`shray77/vetvoice`](https://gitlab.com/shray77/vetvoice)).

## Что это

Скрипты собирают данные из открытых ветеринарных источников, сравнивают их с базой `drugs_calc.json` (2401 препарат) и исправляют найденные расхождения: дозировки, побочные действия, противопоказания, каренцию.

## Источники данных

| Источник | Что даёт | Доступ |
|----------|----------|--------|
| **Гален** (`galen.vetrf.ru`) | Госреестр ветпрепаратов РФ (РУ, МНН, производитель, форма выпуска, статус) | SOAP `Exportcenter.FMPRegistryService` v2.3, **требует авторизацию ВетИС** |
| **vetprotocol.ru** | 434 препарата по МНН с дозами по видам животных и предупреждениями | открытый |
| **vetlek.ru** | 1337 инструкций в формате приказов Минсельхоза РФ | открытый |
| **vidal.ru/veterinar** | Справочник Видаль Ветеринар | открытый |
| **reestrinform.ru** | Публичное зеркало реестра Гален | ⚠️ Cloudflare Browser-Check |

## Структура

```
scripts/
├── parsers/
│   ├── __init__.py
│   ├── galen_parser.py         # SOAP-клиент к Гален (ФГИС ВетИС)
│   ├── vetprotocol_parser.py   # vetprotocol.ru
│   ├── vetlek_parser.py        # vetlek.ru (windows-1251)
│   ├── vidal_parser.py         # vidal.ru/veterinar
│   └── reestrinform_parser.py  # reestrinform.ru (зеркало реестра Гален)
├── validate_vetvoice.py        # Валидатор + apply_fixes
├── run_pipeline.py             # Оркестратор полного цикла
└── download_vetvoice_jsons.sh  # Скачивание JSON базы из GitLab API
```

## Установка

```bash
pip install requests beautifulsoup4 lxml
```

## Использование

### 1. Скачать базу vetvoice из GitLab

```bash
# Через git
git clone https://gitlab.com/shray77/vetvoice.git

# Или через API (если нужен только drugs_calc.json)
bash scripts/download_vetvoice_jsons.sh
```

### 2. Проверить доступность Галена

```bash
python3 scripts/parsers/galen_parser.py --check
# WSDL доступен:        True
# Endpoint доступен:    False  ← нужно VETRF_API_USER/VETRF_API_KEY
```

### 3. Скачать препараты с vetprotocol.ru

```bash
# Все 434 препарата
python3 scripts/parsers/vetprotocol_parser.py --fetch-all vetprotocol.json

# С лимитом
python3 scripts/parsers/vetprotocol_parser.py --fetch-all vetprotocol.json --max 100

# Один препарат
python3 scripts/parsers/vetprotocol_parser.py --fetch albendazol
```

### 4. Скачать инструкции с vetlek.ru

```bash
python3 scripts/parsers/vetlek_parser.py --fetch-all vetlek.json --max 200

# Список ID инструкций
python3 scripts/parsers/vetlek_parser.py --list

# Одна инструкция
python3 scripts/parsers/vetlek_parser.py --fetch 1512
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

| Поле | Тип проверки | Severity |
|------|--------------|----------|
| `dose_per_kg` | vetvoice пусто → применяем из источника | warning |
| `dose_per_kg` | vetvoice уже есть, расхождение >30% | warning (без fix, ручная проверка) |
| `dose_per_kg` | доза >100 мг/кг (нереалистичная) | info (не применяем) |
| `side_effects` | vetvoice пусто → заполняем | warning |
| `contraindications.pregnancy` | vetlek прямо указывает противопоказание | **error** |
| `contraindications.lactation` | vetlek прямо указывает противопоказание | **error** |
| `withdrawal_days` | извлечение каренции из vetlek.special_notes | warning |

## Безопасность исправлений

Валидатор применяет только безопасные исправления:
1. **Заполняет пустые поля** (`dose_per_kg=0/None`, `side_effects=[]`)
2. **Не перезаписывает существующие значения** — расхождения помечаются в отчёте, но не применяются автоматически
3. **Фильтр реалистичности**: дозы >100 мг/кг не применяются (обычно это разовая доза, а не мг/кг)
4. **Логирует все исправления** в `metadata.corrections` в `drugs_calc.json`

## Схема drugs_calc.json (краткая)

```jsonc
{
  "version": "7.4",
  "source": "db.xlsx - converted; merged from registry + dosage_db + verified",
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
      "dose_per_kg": 1,
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
      "side_effects": ["Боль в месте инъекции", "Диарея", "Анорексия", "Летаргия"],
      "category": "НПВС / Анальгетики",
      "indications": "Применяют для предотвращения рвоты различного генеза у собак и кошек",
      "animal_specific": {
        "Собаки": { "dose_per_kg": 2, "method": "Перорально" },
        "Кошки":  { "dose_per_kg": 1, "method": "подкожно" }
      }
    }
  ]
}
```

## Результаты тестового прогона

На выборке из 100 препаратов vetprotocol + 49 инструкций vetlek:
- **Проверено**: 2401 препарат
- **Совпало с источниками**: 294 (12%)
- **Найдено расхождений**: 182
  - 178 по `dose_per_kg`
  - 3 по `contraindications.pregnancy` (error)
  - 1 по `contraindications.lactation` (error)
- **Применено исправлений**: 6 (5 препаратов)
- **Исправленная база**: версия 7.4 → 7.5

## Замечания

- **Гален**: для прямого доступа к SOAP-сервису `Exportcenter` нужна регистрация хозяйствующего субъекта в ФГИС ВетИС. Без учётки парсер только проверяет доступность WSDL. Если есть учётка — установите `VETRF_API_USER` / `VETRF_API_KEY` и запустите `python3 scripts/parsers/galen_parser.py --fetch galen_registry.json`.
- **reestrinform.ru**: публичное зеркало реестра Гален, но блокируется Cloudflare Browser-Check. Для обхода нужен playwright или прокси-сервис.
- **vetlek.ru**: кодировка windows-1251, парсер автоматически переключается.
- **drugs_calc_fixed.json**: при применении исправлений версия автоматически инкрементируется, в `metadata.corrections` добавляется запись о применённых правках.

## Лицензия

MIT — делай что хочешь, но без гарантий. Перед применением исправленных дозировок в реальной практике — консультируйся с ветврачом.
