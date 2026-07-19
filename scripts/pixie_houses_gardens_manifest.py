#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def parse_catalog(path: Path, kind: str) -> dict:
    root = ET.parse(path).getroot()
    copy = root.find('copy')
    items: dict[str, dict] = {}
    descriptions: dict[str, str] = {}
    if copy is not None:
        for node in copy:
            m = re.fullmatch(r'item(\d+)', node.tag)
            if m:
                item_id = m.group(1)
                items[item_id] = {
                    'id': int(item_id),
                    'name': (node.text or '').strip(),
                    'attributes': dict(node.attrib),
                    'catalog': kind,
                }
            m = re.fullmatch(r'desc(\d+)', node.tag)
            if m:
                descriptions[m.group(1)] = (node.text or '').strip()
    for item_id, row in items.items():
        row['description'] = descriptions.get(item_id, '')
        num = int(item_id)
        if kind == 'home':
            if 6500 <= num < 7000:
                row['category'] = 'furniture'
            elif 7000 <= num < 7500:
                row['category'] = 'lamps'
            elif 7500 <= num < 8000:
                row['category'] = 'decorations'
            else:
                row['category'] = 'home_other'
            row['decorating_scale_rule'] = 'home_placeable_60_to_150_percent'
        else:
            if 89000 <= num < 89500:
                row['category'] = 'seeds'
            elif 89500 <= num < 89600:
                row['category'] = 'plants'
            elif 89600 <= num < 89700:
                row['category'] = 'plant_variants_or_rewards'
            else:
                row['category'] = 'garden_other'
            row['decorating_scale_rule'] = None
    return {
        'source_file': path.as_posix(),
        'catalog': kind,
        'item_count': len(items),
        'items': dict(sorted(items.items(), key=lambda kv: int(kv[0]))),
    }


def is_relevant_swf(rel: str) -> tuple[bool, str, str]:
    low = rel.lower()
    name = Path(rel).name.lower()

    if re.match(r'^meadows/(home\d+|garden\d+|gardenshared|gardentalent)/', low):
        return True, 'backgrounds_and_scenes', 'first_frames'
    if low in {
        'swf/home.swf',
        'swf/homeupgradeassets.swf',
        'swf/libhome.swf',
    }:
        return True, 'home_models_and_base_items', 'all_frames'
    if low == 'swf/gardenassets.swf' or low.startswith('swf/items/gardens/'):
        return True, 'garden_plants_and_items', 'all_frames'
    if low.startswith('swf/meadow/') and name.startswith('home'):
        return True, 'home_effects', 'all_frames'
    if low.startswith('swf/hotspots/') and 'garden' in name:
        return True, 'garden_effects', 'all_frames'
    if low.startswith('swf/tutorial/') and ('decorat' in name or 'storage' in name):
        return True, 'decorating_ui', 'all_frames'
    if low.startswith('swf/panel_content/') and any(x in name for x in ('decorate', 'storage', 'homeviewer', 'garden')):
        return True, 'decorating_ui', 'all_frames'
    if low.startswith('swf/panel_code/') and any(x in name for x in ('decorate', 'storage', 'homeviewer', 'garden')):
        return True, 'decorating_ui', 'first_frames'
    if low.startswith('swf/') and '/' not in low[len('swf/'):]:
        if 'storage' in name:
            return True, 'furniture_and_decorations', 'all_frames'
    return False, '', ''


def primary_score(rel: str) -> tuple:
    low = rel.lower()
    penalty = 0
    if low.startswith('pixiehollow/game3/') or low.startswith('pixiehollow/game2/'):
        penalty += 1000
    if low.startswith('publish/'):
        penalty += 800
    if low.startswith('swf/') or low.startswith('meadows/'):
        penalty -= 100
    return penalty, len(rel), rel


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()

    source = Path(args.source).resolve()
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)

    home_catalog = parse_catalog(source / 'xml/homeAssets.xml', 'home')
    garden_catalog = parse_catalog(source / 'xml/gardenAssets.xml', 'garden')
    (output / 'home_catalog.json').write_text(json.dumps(home_catalog, indent=2, ensure_ascii=False), encoding='utf-8')
    (output / 'garden_catalog.json').write_text(json.dumps(garden_catalog, indent=2, ensure_ascii=False), encoding='utf-8')

    selected_paths: list[tuple[Path, str, str]] = []
    for path in source.rglob('*.swf'):
        rel = path.relative_to(source).as_posix()
        relevant, category, sprite_mode = is_relevant_swf(rel)
        if relevant:
            selected_paths.append((path, category, sprite_mode))

    groups: dict[str, list[tuple[str, str, str, int]]] = defaultdict(list)
    for path, category, sprite_mode in selected_paths:
        rel = path.relative_to(source).as_posix()
        digest = sha256_file(path)
        groups[digest].append((rel, category, sprite_mode, path.stat().st_size))

    rows = []
    for digest, entries in sorted(groups.items()):
        entries.sort(key=lambda x: primary_score(x[0]))
        primary, category, sprite_mode, size = entries[0]
        if any(row[2] == 'all_frames' for row in entries):
            sprite_mode = 'all_frames'
        all_paths = [row[0] for row in entries]
        all_categories = sorted({row[1] for row in entries})
        rows.append({
            'sha256': digest,
            'primary_source_path': primary,
            'bytes': size,
            'category': category,
            'all_categories_json': json.dumps(all_categories),
            'sprite_mode': sprite_mode,
            'all_duplicate_paths_json': json.dumps(all_paths),
        })

    fields = ['sha256', 'primary_source_path', 'bytes', 'category', 'all_categories_json', 'sprite_mode', 'all_duplicate_paths_json']
    with (output / 'selected_swfs.tsv').open('w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fields, delimiter='\t')
        w.writeheader()
        w.writerows(rows)

    loose = []
    for path in source.rglob('*'):
        if not path.is_file() or path.suffix.lower() not in {'.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp', '.svg'}:
            continue
        rel = path.relative_to(source).as_posix()
        low = rel.lower()
        if re.search(r'(^|/)(home|homes|garden|gardens|decorat|furniture|storage)(/|_|\.|$)', low):
            loose.append(rel)
        elif low.startswith('images/tutorials/') and any(x in low for x in ('decorat', 'home', 'garden', 'storage')):
            loose.append(rel)
    (output / 'selected_loose_images.txt').write_text('\n'.join(sorted(set(loose))) + '\n', encoding='utf-8')

    configs = []
    for path in source.glob('meadows/*/config.xml'):
        rel = path.relative_to(source).as_posix()
        if re.match(r'^meadows/(home\d+|garden\d+|gardenshared|gardentalent)/config\.xml$', rel.lower()):
            configs.append(rel)
    (output / 'selected_scene_configs.txt').write_text('\n'.join(sorted(configs)) + '\n', encoding='utf-8')

    summary = {
        'source_commit': '',
        'unique_selected_swfs': len(rows),
        'all_selected_swf_paths_including_duplicates': sum(len(json.loads(row['all_duplicate_paths_json'])) for row in rows),
        'home_catalog_items': home_catalog['item_count'],
        'garden_catalog_items': garden_catalog['item_count'],
        'loose_images': len(set(loose)),
        'scene_configs': len(configs),
        'categories': {},
    }
    for row in rows:
        summary['categories'][row['category']] = summary['categories'].get(row['category'], 0) + 1
    (output / 'selection_summary.json').write_text(json.dumps(summary, indent=2), encoding='utf-8')


if __name__ == '__main__':
    main()
