#!/bin/bash
# Скачивает все JSON-файлы базы данных из репозитория vetvoice
set -e

TOKEN="glpat-kYDlfx64h8YDkcA1bO_rXWM6MQpvOjEKdTprc3RjNw8.01.1719onwqm"
PROJECT_ID="80282574"
OUT_DIR="/home/z/my-project/vetvoice_data"
mkdir -p "$OUT_DIR/advanced"

# URL-кодируем пути к файлам
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
  # URL-кодируем слэши как %2F
  encoded=$(echo "$f" | sed 's|/|%2F|g')
  out_path="$OUT_DIR/$f"
  out_path="${out_path/assets\/data\//}"
  echo "Downloading $f -> $out_path"
  
  # Получаем base64-контент через API
  resp=$(curl -s --header "PRIVATE-TOKEN: $TOKEN" \
    "https://gitlab.com/api/v4/projects/$PROJECT_ID/repository/files/$encoded?ref=main")
  
  # Декодируем и сохраняем
  echo "$resp" | python3 -c "
import json, sys, base64
d = json.load(sys.stdin)
if 'content' in d:
    sys.stdout.buffer.write(base64.b64decode(d['content']))
else:
    print('ERROR:', json.dumps(d, indent=2)[:300], file=sys.stderr)
    sys.exit(1)
" > "$out_path"
  
  if [ $? -eq 0 ] && [ -s "$out_path" ]; then
    size=$(wc -c < "$out_path")
    echo "  OK, size: $size bytes"
  else
    echo "  FAILED"
  fi
done

echo ""
echo "=== Downloaded files ==="
ls -lhR "$OUT_DIR"
