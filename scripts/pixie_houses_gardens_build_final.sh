#!/usr/bin/env bash
set -euo pipefail

: "${SOURCE_SHA:?SOURCE_SHA is required}"
: "${SOURCE_SHORT:?SOURCE_SHORT is required}"
: "${FFDEC_JAR:?FFDEC_JAR is required}"

mkdir -p merged/raw merged/metadata merged/loose_images evidence
cp -a downloaded/pixie-hg-manifest/. merged/metadata/
for shard in downloaded/pixie-hg-extracted-*; do
  cp -a "$shard/raw/." merged/raw/
  if [ -d "$shard/logs" ]; then
    mkdir -p merged/metadata/extraction_logs
    cp -a "$shard/logs/." merged/metadata/extraction_logs/
  fi
done
while IFS= read -r rel; do
  [ -f "source/$rel" ] || continue
  mkdir -p "merged/loose_images/$(dirname "$rel")"
  cp -a "source/$rel" "merged/loose_images/$rel"
done < merged/metadata/selected_loose_images.txt
test "$(cat merged/metadata/source_commit.txt)" = "$SOURCE_SHA"
echo "Merged subset: $(du -sh merged | cut -f1)"

files=(swf/mmo.swf swf/panel_code/decoratePanel.swf swf/panel_code/homeViewer.swf)
for rel in "${files[@]}"; do
  [ -f "source/$rel" ] || continue
  safe=$(printf '%s' "${rel%.swf}" | sed -E 's#[^A-Za-z0-9._/-]+#_#g')
  mkdir -p "evidence/$safe"
  timeout 600s java -jar "$FFDEC_JAR" -onerror ignore -export script "evidence/$safe" "source/$rel" >"evidence/${safe//\//_}.log" 2>&1 || true
done
rg -n -i 'MAX_SCALE|MIN_SCALE|SCALE_MULTIPLIER|roomScale|scaleX|scaleY' evidence > evidence/resize_rule_hits.txt || true
test -s evidence/resize_rule_hits.txt

library="Pixie_Hollow_Houses_and_Gardens_Image_Library_${SOURCE_SHORT}"
python builder/scripts/pixie_houses_gardens_assemble.py \
  --source source --merged merged --output "$library" --evidence evidence

LIBRARY="$library" python - <<'PY'
import hashlib, json, os
from pathlib import Path
from PIL import Image

lib = Path(os.environ['LIBRARY'])
summary = json.loads((lib/'00_metadata/library_summary.json').read_text())
rules = json.loads((lib/'00_metadata/decorating_scale_rules.json').read_text())
items = json.loads((lib/'00_metadata/item_size_limits.json').read_text())
dedupe = json.loads((lib/'00_metadata/deduplication_report.json').read_text())
scenes = json.loads((lib/'00_metadata/scene_variants.json').read_text())
matches = json.loads((lib/'00_metadata/item_symbol_matches.json').read_text())

assert summary['source_commit'] == os.environ['SOURCE_SHA'], summary
assert summary['home_catalog_items'] == 732, summary
assert summary['garden_catalog_items'] == 232, summary
assert summary['unique_images'] >= 500, summary
assert summary['scene_variants'] >= 1, summary
assert len(scenes) == summary['scene_variants'], (len(scenes), summary)
assert summary['catalog_symbol_matches'] >= 20, summary
assert len(matches) == summary['catalog_symbol_matches'], (len(matches), summary)

rule = rules['home_placeable_item_rule']
assert rule['minimum_scale_fraction'] == 0.6
assert rule['maximum_scale_fraction'] == 1.5
assert rule['default_scale_fraction'] == 1.0
assert rule['minimum_percent'] == 60
assert rule['maximum_percent'] == 150
assert rule['default_percent'] == 100

home = [row for row in items.values() if row.get('catalog') == 'home']
garden = [row for row in items.values() if row.get('catalog') == 'garden']
assert len(home) == 732
assert len(garden) == 232
assert all(row['resizable_in_decorating_mode'] is True for row in home)
assert all(row['minimum_percent'] == 60 and row['maximum_percent'] == 150 and row['default_percent'] == 100 for row in home)
assert all(row['resizable_in_decorating_mode'] is False for row in garden)
assert all(row['minimum_percent'] is None and row['maximum_percent'] is None for row in garden)
assert dedupe['unique_physical_image_visuals'] == summary['unique_images']

visual_hashes = {}
checked = 0
for path in sorted(lib.rglob('*')):
    if not path.is_file() or path.suffix.lower() not in {'.png','.jpg','.jpeg','.gif','.webp','.bmp','.apng'}:
        continue
    if '06_contact_sheets' in path.parts:
        continue
    with Image.open(path) as image:
        image = image.convert('RGBA')
        if image.getbbox() is None:
            continue
        h = hashlib.sha256(f'{image.width}x{image.height}|RGBA|'.encode() + image.tobytes()).hexdigest()
    if h in visual_hashes:
        raise AssertionError(f'duplicate physical visual: {path} duplicates {visual_hashes[h]}')
    visual_hashes[h] = path.as_posix()
    checked += 1

for scene in scenes:
    canonical = lib / scene['complete_scene']
    assert canonical.exists(), f'missing scene reference: {canonical}'

validation = {
    'status': 'passed',
    'source_commit': os.environ['SOURCE_SHA'],
    'summary': summary,
    'resize_rule': rule,
    'deduplication': dedupe,
    'validated_unique_physical_raster_visuals_excluding_contact_sheets': checked,
    'scene_variant_records': len(scenes),
    'catalog_symbol_matches': len(matches),
    'all_scene_references_resolve': True,
}
(lib/'00_metadata/VALIDATION.json').write_text(json.dumps(validation, indent=2))
print(json.dumps(validation, indent=2))
PY

zip_name="${library}.zip"
zip -0 -q -r "$zip_name" "$library"
unzip -tq "$zip_name"
sha=$(sha256sum "$zip_name" | awk '{print $1}')
bytes=$(stat -c '%s' "$zip_name")
printf '%s  %s\n' "$sha" "$zip_name" > "${zip_name}.sha256"
cp "$library/00_metadata/library_summary.json" library_summary.json
cp "$library/00_metadata/VALIDATION.json" VALIDATION.json

echo "LIBRARY=$library" >> "$GITHUB_ENV"
echo "ZIP_NAME=$zip_name" >> "$GITHUB_ENV"
echo "ZIP_SHA=$sha" >> "$GITHUB_ENV"
echo "ZIP_BYTES=$bytes" >> "$GITHUB_ENV"
echo "ZIP complete: $bytes bytes sha256=$sha"
