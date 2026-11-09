#!/usr/bin/env python3
"""
Task 227 (2026-11-10): РУ-актуальность канона vetvoice против госреестра.

Открытые данные Россельхознадзора (дамп реестра ЛС) не содержат ни доз, ни
способа применения (проверено по схеме SOAP FMPRegistryService, CSV-дампу и
веб-морде Гален — поля дозирования в реестре отсутствуют как класс), НО
содержат регистрационные удостоверения: номер, дату регистрации, дату
окончания, учётную серию.

Сверка канона drugs_calc.json с реестром:
  * записи канона, у которых РУ препарата истёк (end_date < сегодня);
  * записи канона, не найденные в реестре (снятие/переименование/др. держатель);
  * свежие номера РУ (для справки: перерегистрации).

Канон не несёт полей РУ (schema — только фармакология), поэтому результат —
отчёт для владельца, НЕ правка базы. Поле «Дозировка» реестра — сила состава
(«150 мг, 184 мг, 15 мг» на шприц-тубу), не режим дозирования.

Usage:
  python3 scripts/ru_actuality.py --fsvps /tmp/fsvps.json \
      --drugs-calc ../vetvoice/assets/data/drugs_calc.json \
      --output data/validation/ru_actuality_2026-11-10.md
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path


def norm(name: str) -> str:
    s = (name or "").lower().replace("ё", "е")
    s = re.sub(r"[®™]", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def parse_date(s: str) -> datetime | None:
    s = (s or "").strip()
    for fmt in ("%d.%m.%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fsvps", required=True, help="JSON-дамп fsvps_parser.py")
    ap.add_argument("--drugs-calc", required=True)
    ap.add_argument("--output", required=True)
    ap.add_argument("--today", default=None, help="YY-mm-dd, дефолт — сегодня")
    args = ap.parse_args()

    today = datetime.strptime(args.today, "%Y-%m-%d") if args.today else datetime.now()

    fsvps = json.loads(Path(args.fsvps).read_text(encoding="utf-8"))
    entries = fsvps["drugs"] if isinstance(fsvps, dict) and "drugs" in fsvps else fsvps
    dump_date = fsvps.get("dump_date", "?") if isinstance(fsvps, dict) else "?"

    canon = json.loads(Path(args.drugs_calc).read_text(encoding="utf-8"))
    drugs = canon["drugs_calc"] if "drugs_calc" in canon else canon

    # индекс реестра по нормализованному имени (может быть несколько записей)
    ridx: dict[str, list] = {}
    for e in entries:
        ridx.setdefault(norm(e.get("trade_name", "")), []).append(e)

    matched, unmatched, expired, renewed = 0, [], [], []
    for d in drugs:
        key = norm(d.get("name", ""))
        hits = ridx.get(key)
        if not hits:
            unmatched.append(d)
            continue
        matched += 1
        # истёкшие РУ среди всех записей с этим именем
        live = []
        for h in hits:
            end = parse_date(h.get("registration_end_date", ""))
            if end and end < today and not (h.get("registration_end_date") or "").strip() == "":
                expired.append((d, h, end))
            else:
                live.append(h)
        # перерегистрация: в реестме есть запись с датой новее 2025 (смена РУ)
        for h in hits:
            reg = parse_date(h.get("registration_date", ""))
            if reg and reg.year >= 2025:
                renewed.append((d, h))

    lines = []
    lines.append("# РУ-актуальность канона против госреестра ЛС (открытые данные РСХН)")
    lines.append("")
    lines.append(f"- Дамп реестра: **{dump_date}** ({len(entries)} препаратов)")
    lines.append(f"- Канон vetvoice: **{len(drugs)}** записей")
    lines.append(f"- Сматчено по торговому наименованию: **{matched}**")
    lines.append(f"- Не найдено в реестре: **{len(unmatched)}**")
    lines.append(f"- РУ с истёкшим сроком: **{len(expired)}**")
    lines.append(f"- Перерегистрации 2025+ (свежие РУ): **{len(renewed)}**")
    lines.append("")
    lines.append("> Реестр не содержит способов применения и доз — поле «Дозировка»")
    lines.append("> это сила состава. Сверка РУ — единственная законная польза дампа")
    lines.append("> для канона помимо видовых показаний (см. Task 227).")
    lines.append("")

    lines.append("## Истёкшие РУ (дата окончания в прошлом)")
    lines.append("")
    if expired:
        lines.append("| Препарат (канон) | №РУ в реестре | Окончание | Статус в реестре |")
        lines.append("|---|---|---|---|")
        seen = set()
        for d, h, end in sorted(expired, key=lambda x: x[2]):
            row = (d["id"], h.get("registration_number", ""))
            if row in seen:
                continue
            seen.add(row)
            lines.append(
                f"| #{d['id']} {d['name']} | {h.get('registration_number','')} "
                f"| {h.get('registration_end_date','')} | {h.get('dump_date','')} |"
            )
    else:
        lines.append("— не найдено.")
    lines.append("")

    lines.append("## Перерегистрации 2025+ (свежие РУ — справка)")
    lines.append("")
    if renewed:
        lines.append("| Препарат (канон) | №РУ | Зарегистрирован | Действует до |")
        lines.append("|---|---|---|---|")
        seen = set()
        for d, h in renewed:
            row = (d["id"], h.get("registration_number", ""))
            if row in seen:
                continue
            seen.add(row)
            lines.append(
                f"| #{d['id']} {d['name']} | {h.get('registration_number','')} "
                f"| {h.get('registration_date','')} | {h.get('registration_end_date','')} |"
            )
    else:
        lines.append("— не найдено.")
    lines.append("")

    lines.append("## Не найдено в реестре (первые 60, алфавит)")
    lines.append("")
    lines.append("Кандидаты на проверку: снятие с регистрации, переименование,")
    lines.append("импортные регистрации вне дампа, записи-дубли канона.")
    lines.append("")
    for d in sorted(unmatched, key=lambda x: x["name"])[:60]:
        lines.append(f"- #{d['id']} {d['name']}")
    if len(unmatched) > 60:
        lines.append(f"- … и ещё {len(unmatched) - 60}")
    lines.append("")

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"отчёт: {out}")
    print(f"матч {matched}/{len(drugs)}, не найдено {len(unmatched)}, истекло {len(expired)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
