#!/bin/bash
# Скачивает все JSON-файлы базы данных из репозитория vetvoice (GitHub, main).
#
# Источник — https://github.com/shray77/vetvoice (main). GitLab-зеркало
# протухло и отключено (Task 187, 2026-10-29): база чистилась в GitHub
# (Task 182/186/187), гитлабовская копия дорефакторная. Качать её и тем
# более заливать обратно НЕЛЬЗЯ — откат чистки.
#
# Репо публичное — токен не нужен. (Прежняя версия тащила базу с GitLab
# API по вкоммиченному PAT-токену; токен удалён из кода — отозвать на
# стороне GitLab, если ещё жив.)
set -e

BRANCH="main"
REPO="shray77/vetvoice"
BASE_URL="https://raw.githubusercontent.com/${REPO}/${BRANCH}"
OUT_DIR="/home/z/my-project/vetvoice_data"
mkdir -p "$OUT_DIR/advanced"

files=(
  "assets/data/diseases.json"
  "assets/data/dosage_database.json"
  "assets/data/drugs.json"
  "assets/data/drugs_calc.json"
  "assets/data/drugs_registry.json"
  "assets/data/verified_dosages.json"
  "assets/data/disease_normative_docs.json"
  "assets/data/unofficial_protocols.json"
  "assets/data/advanced/antidotes.json"
  "assets/data/advanced/dose_adjustments.json"
  "assets/data/advanced/drug_interactions.json"
  "assets/data/advanced/emergency_protocols.json"
  "assets/data/advanced/fluid_therapy.json"
  "assets/data/advanced/side_effects.json"
  "assets/data/advanced/treatment_protocols.json"
  "assets/data/advanced/withdrawal_by_product.json"
)

for f in "${files[@]}"; do
  out_path="$OUT_DIR/$f"
  out_path="${out_path/assets\/data\//}"
  echo "Downloading $f -> $out_path"

  http_code=$(curl -sL -w '%{http_code}' -o "$out_path" "$BASE_URL/$f")

  if [ "$http_code" = "200" ] && [ -s "$out_path" ]; then
    size=$(wc -c < "$out_path")
    echo "  OK, size: $size bytes"
  else
    echo "  FAILED (HTTP $http_code)"
    rm -f "$out_path"
    exit 1
  fi
done

echo ""
echo "=== Downloaded files ==="
ls -lhR "$OUT_DIR"
