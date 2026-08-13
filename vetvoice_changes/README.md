# VetVoice Flutter Changes — UX/UI фичи

Готовые изменения для Flutter-приложения `shray77/vetvoice` (GitLab).
Чтобы применить — скопируй файлы из этой папки в соответствующие места
репозитория vetvoice.

## Что внутри

### 🆕 Новые файлы (8)

| Файл | Описание |
|------|----------|
| `lib/models/vaccine_specific.dart` | Модель `VaccineSpecific` + `enum VaccineType` |
| `lib/widgets/vaccine_card.dart` | Виджет `VaccineCard` для отображения вакцин |
| `lib/services/favorites_service.dart` | Сервис избранного (SharedPreferences) |
| `lib/services/history_service.dart` | Сервис истории расчётов (SharedPreferences) |
| `lib/services/theme_service.dart` | Сервис переключения темы (light/dark/system) |
| `lib/screens/favorites_screen.dart` | Экран избранного |
| `lib/screens/history_screen.dart` | Экран истории расчётов |
| `lib/screens/settings_screen.dart` | Экран настроек с переключателем темы |

### ✏️ Изменённые файлы (6)

| Файл | Что изменилось |
|------|----------------|
| `lib/main.dart` | Добавлен `ThemeService`, `darkTheme`, `themeMode` |
| `lib/utils/app_theme.dart` | Добавлена `darkTheme`, `categoryColors`, `getCategoryColor()`, `getCategoryIcon()` |
| `lib/models/calc_drug.dart` | Добавлено поле `vaccineSpecific`, геттер `isVaccine` |
| `lib/widgets/result_card.dart` | Если препарат-вакцина → `VaccineCard`, иначе обычный расчёт. Добавлены кнопки ⭐ «В избранное» и «В историю» |
| `lib/widgets/drug_dropdown.dart` | Добавлена цветовая полоска категории + иконка + бейдж |
| `lib/screens/home_screen.dart` | Добавлены кнопки ⭐ Избранное, 🕒 История, ⚙️ Настройки в TopBar |
| `pubspec.yaml` | Добавлен `shared_preferences: ^2.2.2`, версия bumped до `1.14.0+17` |

## Как применить

```bash
# Из корня репозитория vetvoice-parsers:
cp -r vetvoice_changes/lib/* path/to/vetvoice/lib/
cp vetvoice_changes/pubspec.yaml path/to/vetvoice/pubspec.yaml

# Затем в репозитории vetvoice:
cd path/to/vetvoice
flutter pub get
flutter run
```

## UX-фичи, которые добавляются

### 1. 💉 Vaccine-specific экран (Приоритет 1.1)
- Если препарат — вакцина (form_type=vaccine или category=Иммунобиологические),
  показывается `VaccineCard` вместо обычного калькулятора
- VaccineCard показывает:
  - Тип вакцины (живая/инактивированная/рекомбинантная/...) с цветной плашкой
  - Разовую дозу (мл)
  - Путь введения (подкожно/внутримышечно/...)
  - Схему вакцинации (если есть)
  - Фасовку (2000/4000/5000 доз во флаконе)
  - Калькулятор флаконов: «10 животных × 1 мл = 10 мл = 1 флакон по 100 доз»

### 2. 🎨 Цветовое кодирование по категориям (Приоритет 1.2)
В `drug_dropdown.dart` каждая запись в списке препаратов теперь имеет:
- Цветную полоску слева (🔴 антибиотики, 🔵 вакцины, 🟢 витамины, ...)
- Иконку категории (🦠 💉 🌱 💊 🐛 🕷️ 🧴 🍄 ⚗️ ❤️ 🧠 🛡️ 🩹 🫀 🔬)
- Бейдж с названием категории

### 3. ⭐ Избранное (Приоритет 1.3)
- Кнопка ⭐ в карточке результата → добавляет препарат в избранное
- Экран «Избранное» (через TopBar ⭐) показывает все избранные препараты
- С цветовой кодировкой по категориям
- Хранится в SharedPreferences (без сервера)

### 4. 🕒 История расчётов (Приоритет 1.4)
- Кнопка «В историю» в карточке результата → сохраняет расчёт
- Экран «История» (через TopBar 🕒) показывает последние 50 расчётов
- Свайп влево → удалить запись
- Каждая запись показывает: дату, название, животное, вес, результат

### 5. 🌓 Тёмная тема (Приоритет 2.4)
- Экран «Настройки» (через TopBar ⚙️)
- Три варианта: Системная / Светлая / Тёмная
- OLED-чёрный фон (#000000) в тёмной теме
- Выбор сохраняется в SharedPreferences

## Зависимости

В `pubspec.yaml` добавлен:
```yaml
shared_preferences: ^2.2.2
```

Запусти `flutter pub get` после применения изменений.

## Совместимость

- Flutter 3.x+ (Dart 3.x+)
- Минимальная версия vetvoice: 1.13.2+16 (текущая)
- После изменений: 1.14.0+17

## TODO (не вошло в этот патч)

- Интеграция `HistoryService.addCalculation()` в `VetProvider` (сейчас
  сохранение идёт через кнопку «В историю» в `ResultCard`)
- Интеграция `ThemeService` в `VetProvider` (сейчас только в `main.dart`)
- Поиск по симптомам (Приоритет 2.2)
- Протоколы лечения (Приоритет 2.1)
- Interactions checker как отдельный экран (Приоритет 2.3)
- Каренция-калькулятор (Приоритет 3.7)
