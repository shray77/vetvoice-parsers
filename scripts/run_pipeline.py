"""
VetVoice Pipeline — оркестратор полного цикла парсинга и валидации.

Запускает все парсеры по очереди, затем валидирует drugs_calc.json
по собранным данным и применяет исправления.

Использование:
    python run_pipeline.py --drugs-calc /path/to/drugs_calc.json \
                            --output-dir /home/z/my-project/download \
                            [--max-vetprotocol 434] \
                            [--max-vetlek 200]
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path

# Добавляем parsers в путь
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from parsers import galen_parser, vetprotocol_parser, vetlek_parser, vidal_parser, reestrinform_parser
import validate_vetvoice

log = logging.getLogger("vetvoice_pipeline")
if not log.handlers:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


# ---------------------------------------------------------------------------
# Шаги pipeline
# ---------------------------------------------------------------------------

def step_galen_check(output_dir: Path) -> dict:
    """Проверить доступность SOAP-сервиса Гален."""
    log.info("=== Шаг 1: Проверка Галена ===")
    client = galen_parser.GalenClient()
    wsdl_ok = client.check_wsdl()
    endpoint_ok = client.check_endpoint()
    result = {
        "wsdl_available": wsdl_ok,
        "endpoint_available": endpoint_ok,
        "note": (
            "Для прямого доступа к реестру Гален через SOAP требуется "
            "регистрация в ФГИС ВетИС (VETRF_API_USER/VETRF_API_KEY). "
            "Без учётки парсер не может скачать реестр. "
            "Используйте vetprotocol/vetlek как альтернативные источники."
        ),
    }
    log.info("  WSDL: %s, Endpoint: %s", wsdl_ok, endpoint_ok)
    return result


def step_vetprotocol_fetch(output_dir: Path, max_drugs: int) -> dict:
    """Скачать препараты с vetprotocol.ru."""
    log.info("=== Шаг 2: Парсинг vetprotocol.ru ===")
    out_path = output_dir / "vetprotocol.json"
    n = vetprotocol_parser.fetch_all_drugs(str(out_path), max_drugs=max_drugs)
    return {"file": str(out_path), "count": n}


def step_vetlek_fetch(output_dir: Path, max_directions: int) -> dict:
    """Скачать инструкции с vetlek.ru."""
    log.info("=== Шаг 3: Парсинг vetlek.ru ===")
    out_path = output_dir / "vetlek.json"
    n = vetlek_parser.fetch_all_directions(str(out_path), max_directions=max_directions)
    return {"file": str(out_path), "count": n}


def step_vidal_fetch(output_dir: Path, max_drugs: int) -> dict:
    """Скачать препараты с vidal.ru/veterinar (если нужно)."""
    log.info("=== Шаг 4: Парсинг vidal.ru/veterinar ===")
    out_path = output_dir / "vidal.json"
    n = vidal_parser.fetch_all_drugs(str(out_path), max_drugs=max_drugs)
    return {"file": str(out_path), "count": n}


def step_validate(
    drugs_calc: str,
    vetprotocol: str,
    vetlek: str,
    output_dir: Path,
    apply_fixes: bool = True,
) -> dict:
    """Валидировать drugs_calc.json по собранным источникам."""
    log.info("=== Шаг 5: Валидация vetvoice ===")
    report = validate_vetvoice.validate(
        drugs_calc_path=drugs_calc,
        vetprotocol_path=vetprotocol,
        vetlek_path=vetlek,
    )
    report_path = output_dir / "validation_report.json"
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report.to_dict(), f, ensure_ascii=False, indent=2)
    log.info("Отчёт: %s", report_path)

    fixed_path = None
    n_fixes = 0
    if apply_fixes:
        fixed_path = output_dir / "drugs_calc_fixed.json"
        n_fixes = validate_vetvoice.apply_fixes(
            drugs_calc, report, str(fixed_path)
        )
        log.info("Исправленный файл: %s (%d исправлений)", fixed_path, n_fixes)
    return {
        "report_file": str(report_path),
        "fixed_file": str(fixed_path) if fixed_path else None,
        "n_fixes": n_fixes,
        "n_discrepancies": len(report.discrepancies),
        "n_matched": report.matched_drugs,
        "n_total": report.total_drugs,
    }


# ---------------------------------------------------------------------------
# Главный
# ---------------------------------------------------------------------------

def main():
    p = argparse.ArgumentParser(description="VetVoice Pipeline")
    p.add_argument("--drugs-calc", required=True,
                   help="Путь к drugs_calc.json")
    p.add_argument("--output-dir", default="/home/z/my-project/download",
                   help="Куда сохранять результаты")
    p.add_argument("--max-vetprotocol", type=int, default=434,
                   help="Лимит препаратов vetprotocol (434 = все)")
    p.add_argument("--max-vetlek", type=int, default=200,
                   help="Лимит инструкций vetlek")
    p.add_argument("--skip-fetch", action="store_true",
                   help="Пропустить парсинг (использовать уже скачанные файлы)")
    p.add_argument("--no-apply-fixes", action="store_true",
                   help="Не применять исправления")
    args = p.parse_args()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    summary = {"started_at": time.strftime("%Y-%m-%d %H:%M:%S")}

    # 1) Гален
    summary["galen"] = step_galen_check(output_dir)

    vp_path = str(output_dir / "vetprotocol.json")
    vl_path = str(output_dir / "vetlek.json")

    if not args.skip_fetch:
        # 2) vetprotocol
        try:
            summary["vetprotocol"] = step_vetprotocol_fetch(
                output_dir, args.max_vetprotocol
            )
        except Exception as e:
            log.error("vetprotocol failed: %s", e)
            summary["vetprotocol"] = {"error": str(e)}

        # 3) vetlek
        try:
            summary["vetlek"] = step_vetlek_fetch(
                output_dir, args.max_vetlek
            )
        except Exception as e:
            log.error("vetlek failed: %s", e)
            summary["vetlek"] = {"error": str(e)}

    # 5) Валидация
    if os.path.exists(vp_path) and os.path.exists(vl_path):
        summary["validation"] = step_validate(
            args.drugs_calc, vp_path, vl_path, output_dir,
            apply_fixes=not args.no_apply_fixes,
        )
    else:
        log.warning("Пропускаю валидацию: нет vetprotocol/vetlek файлов")
        summary["validation"] = {"skipped": True, "reason": "no source files"}

    summary["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")

    # Сохранить итоговый summary
    summary_path = output_dir / "pipeline_summary.json"
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    log.info("=== Pipeline завершён ===")
    print()
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
