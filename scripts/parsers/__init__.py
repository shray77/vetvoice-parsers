"""
Пакет парсеров ветеринарных источников для проекта VetVoice.

Модули:
  * galen_parser       — SOAP-клиент к Гален (ФГИС ВетИС, Россельхознадзор)
  * vetprotocol_parser — парсер vetprotocol.ru (по МНН)
  * vetlek_parser      — парсер vetlek.ru (по торговым наименованиям)
  * vidal_parser       — парсер vidal.ru/veterinar
  * reestrinform_parser — парсер reestrinform.ru (зеркало реестра Гален)

Все парсеры возвращают dataclass-ы с `.to_dict()` и имеют CLI.
"""
