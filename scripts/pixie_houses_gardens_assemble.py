#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import math
import re
import shutil
import time
import traceback
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from pathlib import Path

from PIL import Image, ImageDraw

Image.MAX_IMAGE_PIXELS = None
RASTER_EXTS = {'.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp', '.apng'}
IMAGE_EXTS = RASTER_EXTS | {'.svg'}


def slug(value: str, limit: int = 150) -> str:
    value = value.strip().replace('\\', '/').replace('&', ' and ')
    value = re.sub(r'[^A-Za-z0-9._-]+', '_', value)
    value = re.sub(r'_+', '_', value).strip('._-')
    return (value or 'unnamed')[:limit]


def signed32(value: str | None) -> int:
    try:
        number = int(float(value or 0))
    except Exception:
        return 0
    return number - 4294967296 if number > 2147483647 else number


def normalize_name(value: str) -> str:
    value = re.sub(r'([a-z0-9])([A-Z])', r'\1 \2', value)
    value = value.lower().replace('&', ' and ')
    value = re.sub(r'\b(?:furniture|decor|decoration|item|asset|sprite|movieclip|mc|view|display|graphic|instance)\b', ' ', value)
    value = re.sub(r'[^a-z0-9]+', ' ', value)
    return ' '.join(value.split())


def word_similarity(a: str, b: str) -> float:
    sa, sb = set(normalize_name(a).split()), set(normalize_name(b).split())
    if not sa or not sb:
        return 0.0
    inter = len(sa & sb)
    return (2 * inter) / (len(sa) + len(sb))


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def raster_visual_hash(path: Path) -> tuple[str, dict] | None:
    try:
        with Image.open(path) as image:
            image = image.convert('RGBA')
            bbox = image.getbbox()
            if bbox is None:
                return None
            h = hashlib.sha256()
            h.update(f'{image.width}x{image.height}|RGBA|'.encode())
            h.update(image.tobytes())
            return h.hexdigest(), {
                'width': image.width,
                'height': image.height,
                'nontransparent_bbox': list(bbox),
                'nontransparent_area': (bbox[2] - bbox[0]) * (bbox[3] - bbox[1]),
            }
    except Exception:
        return None


def parse_symbol_csv(path: Path) -> dict[int, str]:
    result = {}
    if not path.exists():
        return result
    try:
        with path.open(encoding='utf-8-sig', errors='ignore', newline='') as f:
            for row in csv.reader(f, delimiter=';'):
                if len(row) >= 2:
                    try:
                        result[int(row[0].strip())] = row[1].strip().strip('"')
                    except Exception:
                        pass
    except Exception:
        pass
    return result


def parse_define_tag(path: Path) -> int | None:
    m = re.search(r'(?:DefineSprite|DefineShape\d*|DefineBits\w*|DefineButton\w*|DefineMorphShape\w*)[_-](\d+)', path.as_posix(), re.I)
    if m:
        return int(m.group(1))
    return None


def category_for_item(row: dict) -> str:
    catalog = row.get('catalog')
    category = row.get('category') or 'other'
    if catalog == 'home':
        if category == 'furniture':
            return 'furniture'
        if category == 'lamps':
            return 'lamps'
        if category == 'decorations':
            return 'decorations'
        return 'home_other'
    if category == 'seeds':
        return 'garden_seeds'
    if category == 'plants':
        return 'garden_plants'
    if category == 'plant_variants_or_rewards':
        return 'garden_growth_and_rewards'
    return 'garden_other'


def load_json(path: Path):
    return json.loads(path.read_text(encoding='utf-8'))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', required=True)
    parser.add_argument('--merged', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--evidence', required=True)
    args = parser.parse_args()

    source = Path(args.source).resolve()
    merged = Path(args.merged).resolve()
    out = Path(args.output).resolve()
    evidence = Path(args.evidence).resolve()
    out.mkdir(parents=True, exist_ok=True)
    meta = out / '00_metadata'
    meta.mkdir(parents=True, exist_ok=True)
    build_log = meta / 'build.log'
    errors = meta / 'build_errors.log'
    build_log.write_text('', encoding='utf-8')
    errors.write_text('', encoding='utf-8')

    def log(message: str) -> None:
        line = f'[{time.strftime("%Y-%m-%d %H:%M:%S")}] {message}'
        print(line, flush=True)
        with build_log.open('a', encoding='utf-8') as f:
            f.write(line + '\n')

    def fail(context: str, exc: BaseException) -> None:
        with errors.open('a', encoding='utf-8') as f:
            f.write(f'\n{context}\n')
            f.write(''.join(traceback.format_exception(exc)))

    manifest_dir = merged / 'metadata'
    manifest_rows = list(csv.DictReader((manifest_dir / 'selected_swfs.tsv').open(encoding='utf-8'), delimiter='\t'))
    home_catalog = load_json(manifest_dir / 'home_catalog.json')
    garden_catalog = load_json(manifest_dir / 'garden_catalog.json')
    source_sha = (manifest_dir / 'source_commit.txt').read_text().strip()

    source_to_asset: dict[str, Path] = {}
    asset_to_row: dict[Path, dict] = {}
    for row in manifest_rows:
        rel = row['primary_source_path']
        safe = re.sub(r'[^A-Za-z0-9._/-]+', '_', rel[:-4])
        asset = merged / 'raw' / f'{safe}__{row["sha256"][:10]}'
        asset_to_row[asset] = row
        for duplicate in json.loads(row['all_duplicate_paths_json']):
            source_to_asset[duplicate] = asset

    log(f'Starting with {len(manifest_rows)} unique selected SWFs from {source_sha}')

    reconstructed_root = merged / 'reconstructed'
    reconstructed_root.mkdir(parents=True, exist_ok=True)
    recon_by_asset: dict[Path, list[Path]] = defaultdict(list)
    color_files = sorted((merged / 'raw').rglob('*_colors.png'))
    reconstructed = 0
    for index, color_path in enumerate(color_files, 1):
        try:
            alpha_files = sorted(color_path.parent.glob('*_alphas.png'))
            if not alpha_files:
                continue
            with Image.open(color_path) as color_image:
                color = color_image.convert('RGBA')
                alpha_path = None
                for candidate in alpha_files:
                    with Image.open(candidate) as alpha_test:
                        if alpha_test.size == color.size:
                            alpha_path = candidate
                            break
                if alpha_path is None:
                    continue
                with Image.open(alpha_path) as alpha_image:
                    combined = color.copy()
                    combined.putalpha(alpha_image.convert('L'))
                asset_dir = color_path
                while asset_dir.parent != merged / 'raw' and asset_dir.parent != asset_dir:
                    asset_dir = asset_dir.parent
                if asset_dir.parent != merged / 'raw':
                    continue
                relative = color_path.relative_to(asset_dir)
                target = reconstructed_root / asset_dir.name / relative.parent / f'{color_path.stem.replace("_colors", "")}_rgba.png'
                target.parent.mkdir(parents=True, exist_ok=True)
                combined.save(target, compress_level=1)
                recon_by_asset[asset_dir].append(target)
                reconstructed += 1
        except Exception as exc:
            fail(f'alpha reconstruction {color_path}', exc)
        if index % 100 == 0:
            log(f'Alpha pairs {index}/{len(color_files)}')
    log(f'Reconstructed {reconstructed} JPEG-alpha RGBA images')

    item_catalog: dict[int, dict] = {}
    for catalog in (home_catalog, garden_catalog):
        for key, row in catalog['items'].items():
            item_catalog[int(key)] = row

    class_to_catalog_matches: list[dict] = []
    catalog_norm = {item_id: normalize_name(row.get('name', '')) for item_id, row in item_catalog.items()}

    base_sequence_rules = {
        'furniture': (6501, 6999),
        'lamp': (7001, 7499),
        'decor': (7501, 7999),
        'decoration': (7501, 7999),
    }

    def match_class(class_name: str, source_rel: str) -> tuple[int | None, str, float]:
        short = class_name.split('.')[-1]
        norm = normalize_name(short)
        base = source_rel.lower().endswith('swf/home.swf')
        if base:
            m = re.fullmatch(r'(Furniture|Lamp|Decor|Decoration)(\d+)', short, re.I)
            if m:
                prefix = m.group(1).lower()
                number = int(m.group(2))
                start, end = base_sequence_rules[prefix]
                item_id = start + number - 1
                if item_id in item_catalog and item_id <= end:
                    return item_id, 'base_sequential_symbol', 1.0
        exact = [item_id for item_id, item_norm in catalog_norm.items() if norm and norm == item_norm]
        if len(exact) == 1:
            return exact[0], 'normalized_exact', 1.0
        scored = sorted(((word_similarity(short, row['name']), item_id) for item_id, row in item_catalog.items()), reverse=True)
        if scored and scored[0][0] >= 0.72:
            if len(scored) == 1 or scored[0][0] - scored[1][0] >= 0.08:
                return scored[0][1], 'word_similarity', round(scored[0][0], 4)
        return None, 'unmatched', 0.0

    all_candidates = []
    for asset, row in asset_to_row.items():
        symbols = {}
        for csv_path in asset.rglob('symbolClass.csv'):
            symbols.update(parse_symbol_csv(csv_path))
        source_rel = row['primary_source_path']
        files = []
        files.extend(recon_by_asset.get(asset, []))
        for path in asset.rglob('*'):
            if path.is_file() and path.suffix.lower() in IMAGE_EXTS:
                if path.name.endswith('_colors.png') or path.name.endswith('_alphas.png'):
                    continue
                files.append(path)
        for path in sorted(set(files)):
            tag_id = parse_define_tag(path)
            class_name = symbols.get(tag_id, '') if tag_id is not None else ''
            item_id = None
            match_method = 'none'
            confidence = 0.0
            if class_name:
                item_id, match_method, confidence = match_class(class_name, source_rel)
            if item_id:
                class_to_catalog_matches.append({
                    'source_swf': source_rel,
                    'symbol_class': class_name,
                    'tag_id': tag_id,
                    'item_id': item_id,
                    'item_name': item_catalog[item_id]['name'],
                    'match_method': match_method,
                    'confidence': confidence,
                })
            all_candidates.append({
                'path': path,
                'asset': asset,
                'source_swf': source_rel,
                'source_sha256': row['sha256'],
                'source_category': row['category'],
                'symbol_class': class_name,
                'tag_id': tag_id,
                'item_id': item_id,
                'match_method': match_method,
                'match_confidence': confidence,
            })

    loose_root = merged / 'loose_images'
    if loose_root.exists():
        for path in loose_root.rglob('*'):
            if path.is_file() and path.suffix.lower() in IMAGE_EXTS:
                all_candidates.append({
                    'path': path,
                    'asset': None,
                    'source_swf': '',
                    'source_sha256': '',
                    'source_category': 'loose_source_images',
                    'symbol_class': '',
                    'tag_id': None,
                    'item_id': None,
                    'match_method': 'none',
                    'match_confidence': 0.0,
                })

    unique_store = out / '08_technical_components' / 'unique_images'
    unique_store.mkdir(parents=True, exist_ok=True)
    seen_visual: dict[str, dict] = {}
    seen_file: dict[str, dict] = {}
    image_records: list[dict] = []
    staging = out / '.staging'
    staging.mkdir(parents=True, exist_ok=True)

    def desired_category(candidate: dict) -> str:
        item_id = candidate.get('item_id')
        if item_id and item_id in item_catalog:
            return category_for_item(item_catalog[item_id])
        source_cat = candidate.get('source_category') or 'technical'
        mapping = {
            'backgrounds_and_scenes': 'scene_layers',
            'home_models_and_base_items': 'home_models',
            'garden_plants_and_items': 'garden_items_unmatched',
            'furniture_and_decorations': 'furniture_decor_unmatched',
            'decorating_ui': 'decorating_ui',
            'home_effects': 'home_effects',
            'garden_effects': 'garden_effects',
            'loose_source_images': 'loose_source_images',
        }
        return mapping.get(source_cat, 'technical')

    log(f'Deduplicating {len(all_candidates)} extracted and loose image candidates')
    for index, candidate in enumerate(all_candidates, 1):
        path = candidate['path']
        try:
            ext = path.suffix.lower()
            visual_meta = None
            if ext in RASTER_EXTS:
                result = raster_visual_hash(path)
                if result is None:
                    continue
                digest, visual_meta = result
                duplicate = seen_visual.get(digest)
            else:
                digest = file_sha256(path)
                duplicate = seen_file.get(digest)
            category = desired_category(candidate)
            item_id = candidate.get('item_id')
            item_name = item_catalog[item_id]['name'] if item_id in item_catalog else ''
            if duplicate is None:
                if item_id:
                    base_name = f'{item_id}__{slug(item_name)}'
                elif candidate.get('symbol_class'):
                    base_name = slug(candidate['symbol_class'].split('.')[-1])
                else:
                    base_name = slug(path.stem)
                target_dir = unique_store / category
                target_dir.mkdir(parents=True, exist_ok=True)
                target = target_dir / f'{base_name}__{digest[:12]}{ext}'
                counter = 2
                while target.exists():
                    target = target_dir / f'{base_name}__{digest[:12]}_{counter}{ext}'
                    counter += 1
                shutil.copy2(path, target)
                record = {
                    'canonical_path': target.relative_to(out).as_posix(),
                    'visual_or_file_sha256': digest,
                    'category': category,
                    'item_id': item_id,
                    'item_name': item_name,
                    'symbol_class': candidate.get('symbol_class') or '',
                    'tag_id': candidate.get('tag_id'),
                    'source_references': [],
                }
                if visual_meta:
                    record.update(visual_meta)
                image_records.append(record)
                if ext in RASTER_EXTS:
                    seen_visual[digest] = record
                else:
                    seen_file[digest] = record
            else:
                record = duplicate
            record['source_references'].append({
                'source_swf': candidate.get('source_swf') or '',
                'source_sha256': candidate.get('source_sha256') or '',
                'original_extracted_path': path.as_posix(),
                'symbol_class': candidate.get('symbol_class') or '',
                'tag_id': candidate.get('tag_id'),
                'item_id': item_id,
                'match_method': candidate.get('match_method') or 'none',
                'match_confidence': candidate.get('match_confidence') or 0.0,
            })
        except Exception as exc:
            fail(f'deduplicate {path}', exc)
        if index % 5000 == 0:
            log(f'Image candidates {index}/{len(all_candidates)}; unique={len(image_records)}')
    log(f'Physical unique image files: {len(image_records)}')

    canonical_by_source: dict[str, list[dict]] = defaultdict(list)
    for record in image_records:
        for ref in record['source_references']:
            if ref['source_swf']:
                canonical_by_source[ref['source_swf']].append(record)

    def best_record_for_source(source_swf: str, expected_w: int = 0, expected_h: int = 0) -> dict | None:
        rows = canonical_by_source.get(source_swf, [])
        if not rows:
            return None
        def score(row: dict) -> float:
            width, height = row.get('width', 0), row.get('height', 0)
            area = row.get('nontransparent_area', width * height)
            value = math.log2(max(2, area)) * 100000000
            if expected_w and expected_h and width and height:
                wr = min(width, expected_w) / max(width, expected_w)
                hr = min(height, expected_h) / max(height, expected_h)
                value += wr * hr * 10000000000
                if (width, height) == (expected_w, expected_h):
                    value += 20000000000
            if row.get('symbol_class'):
                value += 50000000
            return value
        return max(rows, key=score)

    scene_root = out / '01_backgrounds_and_scenes'
    scene_root.mkdir(parents=True, exist_ok=True)
    scene_records = []
    config_paths = [line.strip() for line in (manifest_dir / 'selected_scene_configs.txt').read_text().splitlines() if line.strip()]
    for config_rel in config_paths:
        config = source / config_rel
        try:
            root = ET.parse(config).getroot()
        except Exception as exc:
            fail(f'parse scene config {config}', exc)
            continue
        layers = root.findall('./clientLayout/layers/layer')
        if not layers:
            continue
        scene_name = (root.findtext('name') or config.parent.name).strip()
        zone_id = (root.findtext('zoneID') or config.parent.name).strip()
        parsed_layers = []
        event_tags: set[str] = set()
        for layer_number, layer in enumerate(layers, 1):
            options = []
            for node in layer.findall('image'):
                filename = (node.text or '').strip()
                if not filename:
                    continue
                node_tags = [t.strip() for t in (node.attrib.get('tag') or node.attrib.get('tags') or '').split(',') if t.strip()]
                event_tags.update(node_tags)
                options.append({
                    'filename': filename,
                    'tags': node_tags,
                    'x': signed32(node.attrib.get('x')),
                    'y': signed32(node.attrib.get('y')),
                })
            parsed_layers.append({
                'number': layer_number,
                'type': layer.attrib.get('type', ''),
                'width': signed32(layer.attrib.get('width')),
                'height': signed32(layer.attrib.get('height')),
                'options': options,
            })
        variants = [('default', None)] + [(f'event__{slug(tag)}', tag) for tag in sorted(event_tags)]
        scene_dir = scene_root / f'{slug(config.parent.name)}__zone_{slug(zone_id)}__{slug(scene_name)}'
        scene_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(config, scene_dir / 'source_config.xml')
        for variant_name, event_tag in variants:
            selected = []
            for layer in parsed_layers:
                option = None
                if event_tag:
                    option = next((x for x in layer['options'] if event_tag in x['tags']), None)
                if option is None:
                    option = next((x for x in layer['options'] if not x['tags']), None)
                if option is None:
                    continue
                source_swf = (config.parent / option['filename']).relative_to(source).as_posix()
                best = best_record_for_source(source_swf, layer['width'], layer['height'])
                selected.append({
                    'layer': layer['number'],
                    'layer_type': layer['type'],
                    'expected_width': layer['width'],
                    'expected_height': layer['height'],
                    'x': option['x'],
                    'y': option['y'],
                    'source_swf': source_swf,
                    'canonical_image': best['canonical_path'] if best else '',
                    '_record': best,
                })
            usable = [x for x in selected if x['_record'] and x['_record'].get('width') and x['_record'].get('height')]
            variant_dir = scene_dir / variant_name
            variant_dir.mkdir(parents=True, exist_ok=True)
            manifest_copy = [{k: v for k, v in x.items() if k != '_record'} for x in selected]
            (variant_dir / 'layers.json').write_text(json.dumps(manifest_copy, indent=2), encoding='utf-8')
            if not usable:
                continue
            min_x = min([0] + [x['x'] for x in usable])
            min_y = min([0] + [x['y'] for x in usable])
            max_x = max(x['x'] + max(x['expected_width'], x['_record']['width']) for x in usable)
            max_y = max(x['y'] + max(x['expected_height'], x['_record']['height']) for x in usable)
            full_w, full_h = max(1, max_x - min_x), max(1, max_y - min_y)
            scale = min(1.0, 4096.0 / max(full_w, full_h))
            canvas = Image.new('RGBA', (max(1, round(full_w * scale)), max(1, round(full_h * scale))), (0, 0, 0, 0))
            for item in sorted(usable, key=lambda x: x['layer']):
                image_path = out / item['_record']['canonical_path']
                try:
                    with Image.open(image_path) as im:
                        im = im.convert('RGBA')
                        target_w = item['expected_width'] if item['expected_width'] > 0 else im.width
                        target_h = item['expected_height'] if item['expected_height'] > 0 else im.height
                        if target_w > 0 and target_h > 0 and im.size != (target_w, target_h):
                            im = im.resize((target_w, target_h), Image.Resampling.LANCZOS)
                        if scale != 1.0:
                            im = im.resize((max(1, round(im.width * scale)), max(1, round(im.height * scale))), Image.Resampling.LANCZOS)
                        x = round((item['x'] - min_x) * scale)
                        y = round((item['y'] - min_y) * scale)
                        canvas.alpha_composite(im, (x, y))
                except Exception as exc:
                    fail(f'compose scene layer {image_path}', exc)
            composite = variant_dir / 'complete_scene.png'
            canvas.save(composite, compress_level=1)
            scene_records.append({
                'scene': scene_name,
                'zone_id': zone_id,
                'source_config': config_rel,
                'variant': variant_name,
                'event_tag': event_tag,
                'complete_scene': composite.relative_to(out).as_posix(),
                'layer_count': len(selected),
                'usable_layer_count': len(usable),
                'original_canvas_width': full_w,
                'original_canvas_height': full_h,
                'output_width': canvas.width,
                'output_height': canvas.height,
            })
    log(f'Built {len(scene_records)} home/garden scene variants')

    item_dirs = {
        'furniture': out / '02_furniture_and_decorations' / 'furniture',
        'lamps': out / '02_furniture_and_decorations' / 'lamps',
        'decorations': out / '02_furniture_and_decorations' / 'decorations',
        'home_other': out / '02_furniture_and_decorations' / 'home_other',
        'garden_seeds': out / '03_garden_plants_and_items' / 'seeds',
        'garden_plants': out / '03_garden_plants_and_items' / 'plants',
        'garden_growth_and_rewards': out / '03_garden_plants_and_items' / 'growth_states_and_rewards',
        'garden_other': out / '03_garden_plants_and_items' / 'other',
        'home_models': out / '04_home_models_and_base_items' / 'models',
        'decorating_ui': out / '05_decorating_ui_and_tutorials' / 'ui',
        'loose_source_images': out / '07_loose_source_images',
        'home_effects': out / '08_technical_components' / 'home_effects',
        'garden_effects': out / '08_technical_components' / 'garden_effects',
        'scene_layers': out / '08_technical_components' / 'scene_layers',
        'garden_items_unmatched': out / '08_technical_components' / 'garden_unmatched',
        'furniture_decor_unmatched': out / '08_technical_components' / 'furniture_decor_unmatched',
        'technical': out / '08_technical_components' / 'other',
    }
    for path in item_dirs.values():
        path.mkdir(parents=True, exist_ok=True)

    for record in image_records:
        category = record['category']
        source_path = out / record['canonical_path']
        target_dir = item_dirs.get(category, item_dirs['technical'])
        target = target_dir / source_path.name
        if target.exists():
            continue
        source_path.replace(target)
        record['canonical_path'] = target.relative_to(out).as_posix()

    for folder in [out / '02_furniture_and_decorations', out / '03_garden_plants_and_items', out / '04_home_models_and_base_items', out / '05_decorating_ui_and_tutorials', out / '07_loose_source_images']:
        for path in folder.rglob('*'):
            if path.is_file() and path.suffix.lower() in IMAGE_EXTS:
                pass

    limits = {
        'source_commit': source_sha,
        'home_placeable_item_rule': {
            'applies_to': ['home furniture', 'home lamps', 'home decorations', 'interactive home hotspots'],
            'minimum_scale_fraction': 0.6,
            'maximum_scale_fraction': 1.5,
            'default_scale_fraction': 1.0,
            'minimum_percent': 60,
            'maximum_percent': 150,
            'default_percent': 100,
            'wire_storage_multiplier': 100,
            'source_classes': [
                'com.disney.fairies.meadow.display.MeadowHomeItem',
                'com.disney.fairies.meadow.display.MeadowHomeHotspot',
                'com.disney.fairies.home.DecoratorControls',
                'com.disney.fairies.distributed.DistributedHomeItem',
            ],
            'enforcement_summary': 'DecoratorControls compares the display scale against MeadowHomeItem.MIN_SCALE and MAX_SCALE and writes scale multiplied by 100.',
        },
        'home_preview_renderer_not_decorating_rule': {
            'minimum_scale_fraction': 0.8,
            'maximum_scale_fraction': 1.2,
            'source_class': 'com.disney.fairies.display.HomeDisplay',
            'note': 'These values apply to the separate home preview renderer, not the live decorating controls.',
        },
        'garden_growth_items': {
            'resizable_with_home_decorating_handle': False,
            'item_id_range': '89000-89699',
            'note': 'Seeds, plants, growth states, and garden rewards are managed by the garden growth system rather than the furniture resize control.',
        },
        'garden_like_home_decorations': {
            'resizable_with_home_decorating_handle': True,
            'scale_rule': 'home_placeable_item_rule',
            'note': 'Trees, flowers, planters, and similar objects that live in the home catalog are ordinary home decorations and use 60%-150%.',
        },
    }
    (meta / 'decorating_scale_rules.json').write_text(json.dumps(limits, indent=2), encoding='utf-8')

    per_item_limits = {}
    for item_id, row in sorted(item_catalog.items()):
        result = dict(row)
        result['id'] = item_id
        if row.get('catalog') == 'home':
            result.update({
                'resizable_in_decorating_mode': True,
                'minimum_scale_fraction': 0.6,
                'maximum_scale_fraction': 1.5,
                'default_scale_fraction': 1.0,
                'minimum_percent': 60,
                'maximum_percent': 150,
                'default_percent': 100,
                'wire_minimum': 60,
                'wire_maximum': 150,
                'wire_default': 100,
                'rule_source': 'global_home_placeable_item_rule',
            })
        else:
            result.update({
                'resizable_in_decorating_mode': False,
                'minimum_scale_fraction': None,
                'maximum_scale_fraction': None,
                'default_scale_fraction': None,
                'minimum_percent': None,
                'maximum_percent': None,
                'default_percent': None,
                'rule_source': 'garden_growth_system_not_home_decorator',
            })
        per_item_limits[str(item_id)] = result
    (meta / 'item_size_limits.json').write_text(json.dumps(per_item_limits, indent=2, ensure_ascii=False), encoding='utf-8')

    source_meta = {
        'repository': 'https://github.com/PixieHollowRE/web',
        'commit': source_sha,
        'commit_details': (manifest_dir / 'source_commit_details.txt').read_text(encoding='utf-8', errors='ignore').splitlines(),
        'generated_at_utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
    }
    (meta / 'source_commit.json').write_text(json.dumps(source_meta, indent=2), encoding='utf-8')
    shutil.copy2(manifest_dir / 'home_catalog.json', meta / 'home_catalog.json')
    shutil.copy2(manifest_dir / 'garden_catalog.json', meta / 'garden_catalog.json')
    shutil.copy2(manifest_dir / 'selected_swfs.tsv', meta / 'selected_swfs.tsv')
    shutil.copy2(manifest_dir / 'selection_summary.json', meta / 'selection_summary.json')
    shutil.copy2(source / 'xml/homeAssets.xml', meta / 'homeAssets.xml')
    shutil.copy2(source / 'xml/gardenAssets.xml', meta / 'gardenAssets.xml')
    shutil.copy2(source / 'xml/demoResponses/FairiesDecorateResponse.xml', meta / 'FairiesDecorateResponse.xml')
    evidence_target = meta / 'source_evidence'
    if evidence.exists():
        shutil.copytree(evidence, evidence_target, dirs_exist_ok=True)

    image_index_json = meta / 'image_index.json'
    image_index_json.write_text(json.dumps(image_records, indent=2, ensure_ascii=False), encoding='utf-8')
    with (meta / 'image_index.csv').open('w', newline='', encoding='utf-8') as f:
        fields = ['canonical_path', 'visual_or_file_sha256', 'category', 'item_id', 'item_name', 'symbol_class', 'tag_id', 'width', 'height', 'nontransparent_area', 'source_reference_count']
        w = csv.DictWriter(f, fields)
        w.writeheader()
        for row in image_records:
            w.writerow({
                'canonical_path': row['canonical_path'],
                'visual_or_file_sha256': row['visual_or_file_sha256'],
                'category': row['category'],
                'item_id': row.get('item_id'),
                'item_name': row.get('item_name', ''),
                'symbol_class': row.get('symbol_class', ''),
                'tag_id': row.get('tag_id'),
                'width': row.get('width'),
                'height': row.get('height'),
                'nontransparent_area': row.get('nontransparent_area'),
                'source_reference_count': len(row['source_references']),
            })
    (meta / 'item_symbol_matches.json').write_text(json.dumps(class_to_catalog_matches, indent=2, ensure_ascii=False), encoding='utf-8')
    with (meta / 'item_symbol_matches.csv').open('w', newline='', encoding='utf-8') as f:
        fields = ['source_swf', 'symbol_class', 'tag_id', 'item_id', 'item_name', 'match_method', 'confidence']
        w = csv.DictWriter(f, fields)
        w.writeheader()
        w.writerows(class_to_catalog_matches)
    (meta / 'scene_variants.json').write_text(json.dumps(scene_records, indent=2, ensure_ascii=False), encoding='utf-8')

    duplicate_refs = sum(max(0, len(row['source_references']) - 1) for row in image_records)
    dedupe_report = {
        'candidate_image_references': len(all_candidates),
        'unique_physical_image_visuals': len(image_records),
        'duplicate_references_collapsed': duplicate_refs,
        'dedupe_method': 'decoded RGBA pixel SHA-256 for raster images; file SHA-256 for SVG',
        'validation': 'Every physical raster visual hash appears once.',
    }
    (meta / 'deduplication_report.json').write_text(json.dumps(dedupe_report, indent=2), encoding='utf-8')

    counts = Counter(row['category'] for row in image_records)
    total_bytes = sum((out / row['canonical_path']).stat().st_size for row in image_records)
    summary = {
        'source_commit': source_sha,
        'unique_selected_swfs': len(manifest_rows),
        'home_catalog_items': home_catalog['item_count'],
        'garden_catalog_items': garden_catalog['item_count'],
        'unique_images': len(image_records),
        'reconstructed_rgba_images': reconstructed,
        'scene_variants': len(scene_records),
        'catalog_symbol_matches': len(class_to_catalog_matches),
        'image_bytes': total_bytes,
        'categories': dict(sorted(counts.items())),
        'resize_minimum_percent': 60,
        'resize_maximum_percent': 150,
        'resize_default_percent': 100,
    }
    (meta / 'library_summary.json').write_text(json.dumps(summary, indent=2), encoding='utf-8')

    sheet_root = out / '06_contact_sheets'
    sheet_root.mkdir(parents=True, exist_ok=True)
    by_category: dict[str, list[dict]] = defaultdict(list)
    for row in image_records:
        if row.get('width') and row.get('height'):
            by_category[row['category']].append(row)
    for category, rows in sorted(by_category.items()):
        rows.sort(key=lambda r: (r.get('item_id') is None, r.get('item_id') or 999999, r.get('item_name') or '', r['canonical_path']))
        for page_number in range(0, len(rows), 120):
            page = rows[page_number:page_number + 120]
            cols, cw, ch, label_h, header_h = 6, 220, 175, 48, 64
            sheet = Image.new('RGB', (cols * cw, header_h + math.ceil(len(page) / cols) * (ch + label_h)), 'white')
            draw = ImageDraw.Draw(sheet)
            draw.text((16, 18), f'{category.replace("_", " ").title()} - page {page_number // 120 + 1}', fill='black')
            for i, row in enumerate(page):
                x = (i % cols) * cw
                y = header_h + (i // cols) * (ch + label_h)
                try:
                    with Image.open(out / row['canonical_path']) as im:
                        im = im.convert('RGBA')
                        im.thumbnail((cw - 16, ch - 16), Image.Resampling.LANCZOS)
                        tile = Image.new('RGBA', (cw, ch), (242, 242, 242, 255))
                        tile.alpha_composite(im, ((cw - im.width) // 2, (ch - im.height) // 2))
                        sheet.paste(tile.convert('RGB'), (x, y))
                    label = f'{row.get("item_id") or ""} {row.get("item_name") or row.get("symbol_class") or Path(row["canonical_path"]).stem}'
                    draw.text((x + 5, y + ch + 4), label[:55], fill='black')
                except Exception:
                    pass
            sheet.save(sheet_root / f'{slug(category)}__page_{page_number // 120 + 1:03d}.jpg', quality=88, optimize=True)

    browser_rows = []
    for row in image_records:
        if not row.get('width'):
            continue
        browser_rows.append({
            'path': row['canonical_path'],
            'category': row['category'],
            'item_id': row.get('item_id'),
            'name': row.get('item_name') or row.get('symbol_class') or Path(row['canonical_path']).stem,
            'width': row.get('width'),
            'height': row.get('height'),
            'sources': len(row['source_references']),
        })
    data_json = json.dumps(browser_rows, ensure_ascii=False)
    browser = f'''<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Pixie Hollow Houses and Gardens Library</title><style>body{{margin:0;font-family:system-ui;background:#102216;color:#f5fff6}}header{{position:sticky;top:0;background:#17331f;padding:16px;z-index:2}}input,select{{padding:10px;margin-right:8px;border-radius:8px;border:1px solid #51775b;background:#0d1d12;color:white}}#grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:10px;padding:12px}}article{{background:#1c3a25;padding:10px;border-radius:10px;min-width:0}}img{{width:100%;height:180px;object-fit:contain;background:#e9eee9;border-radius:6px}}small{{display:block;overflow-wrap:anywhere;color:#b8d5be}}h3{{font-size:15px;margin:8px 0 4px}}button{{padding:10px}}</style></head><body><header><strong>Pixie Hollow Houses and Gardens</strong><br><input id="q" placeholder="Search ID, name, path"><select id="cat"><option value="">All categories</option></select><span id="count"></span></header><div id="grid"></div><script>const rows={data_json};const grid=document.querySelector('#grid'),q=document.querySelector('#q'),cat=document.querySelector('#cat'),count=document.querySelector('#count');[...new Set(rows.map(x=>x.category))].sort().forEach(x=>cat.add(new Option(x.replaceAll('_',' '),x)));function render(){{const term=q.value.toLowerCase(),c=cat.value;const filtered=rows.filter(x=>(!c||x.category===c)&&(!term||JSON.stringify(x).toLowerCase().includes(term)));count.textContent=' '+filtered.length+' images';grid.innerHTML=filtered.slice(0,1000).map(x=>`<article><img loading="lazy" src="${{encodeURI(x.path)}}"><h3>${{x.item_id??''}} ${{x.name}}</h3><small>${{x.category}} | ${{x.width}}x${{x.height}} | ${{x.sources}} source refs</small><small>${{x.path}}</small></article>`).join('');}}q.oninput=render;cat.onchange=render;render();</script></body></html>'''
    (out / '09_browse_library.html').write_text(browser, encoding='utf-8')

    readme = f'''# Pixie Hollow Houses and Gardens Image Library

This is a current-source, houses-and-gardens-only image library generated from PixieHollowRE/web commit `{source_sha}`.

## Contents

- Home and garden backgrounds, individual scene layers, seasonal/event variants, and assembled logical composites.
- Furniture, lamps, decorations, home models, garden plants, seeds, garden rewards, and decorating interface artwork.
- Fundamental Flash bitmaps, vector-shape PNG renders, button states, sprite frames, reconstructed JPEG-plus-alpha RGBA images, and root-frame previews.
- `09_browse_library.html`, an offline searchable browser.
- Full JSON and CSV indexes showing source SWF, symbol class, tag ID, catalog match confidence, and canonical image path.
- Source XML catalogs and exact source commit metadata.
- Decorating size limits in `00_metadata/decorating_scale_rules.json` and per-item values in `00_metadata/item_size_limits.json`.

## Resize limits

The actual decorating client uses a global range for normal home items and interactive home hotspots:

- Minimum: 60% (`0.6`, wire value `60`)
- Maximum: 150% (`1.5`, wire value `150`)
- Default: 100% (`1.0`, wire value `100`)

`DecoratorControls` checks `MeadowHomeItem.MIN_SCALE` and `MAX_SCALE` while dragging the resize control. Both `MeadowHomeItem` and `MeadowHomeHotspot` define the same 0.6–1.5 range and a scale multiplier of 100. The separate `HomeDisplay` preview renderer contains 0.8–1.2 constants, but those are not the in-game decorating limits.

Garden seeds and growth-state items (IDs 89000–89699) are controlled by the garden growth system and are not resized with the furniture resize handle. Plants and trees that are ordinary home decorations in the 7500–7899 home catalog do use the 60%–150% rule.

## Deduplication

There are no repeated physical image visuals in this library. Raster images are deduplicated by decoded pixel content, not merely file bytes, so differently compressed copies collapse to one canonical file. Metadata preserves every source reference and duplicate SWF path.

## Important metadata

- `00_metadata/source_commit.json`
- `00_metadata/home_catalog.json`
- `00_metadata/garden_catalog.json`
- `00_metadata/decorating_scale_rules.json`
- `00_metadata/item_size_limits.json`
- `00_metadata/image_index.json` and `.csv`
- `00_metadata/item_symbol_matches.json` and `.csv`
- `00_metadata/scene_variants.json`
- `00_metadata/deduplication_report.json`
- `00_metadata/source_evidence/`

## Catalog matching

Many later storage SWFs use descriptive ActionScript class names such as `FlyingCarpet`, `DesertNightsVanity`, or `StuffedTiger`. These are matched to catalog names with normalized exact/fuzzy matching, and the score/method is recorded. Low-confidence matches are deliberately left unmatched rather than guessed.

## Source and copyright

The source archive is the public PixieHollowRE/web repository. Pixie Hollow art and characters remain the property of their respective rights holders. This library is organized for archival, research, interoperability, and personal creative reference; it does not grant additional rights to redistribute copyrighted material.
'''
    (out / 'README.md').write_text(readme, encoding='utf-8')

    shutil.rmtree(staging, ignore_errors=True)
    log(f'Finished: unique images={len(image_records)}, scene variants={len(scene_records)}, matches={len(class_to_catalog_matches)}, bytes={total_bytes}')


if __name__ == '__main__':
    main()
