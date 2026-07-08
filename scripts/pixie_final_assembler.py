#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import html
import json
import math
import os
import re
import shutil
import time
import traceback
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from pathlib import Path

from PIL import Image, ImageDraw

Image.MAX_IMAGE_PIXELS = None
IMAGE_EXTS = {'.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp', '.apng', '.svg'}


def slug(value: str, limit: int = 140) -> str:
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


def tags(node: ET.Element) -> list[str]:
    raw = node.attrib.get('tag') or node.attrib.get('tags') or ''
    return [item.strip() for item in raw.split(',') if item.strip()]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--library', required=True)
    args = parser.parse_args()
    lib = Path(args.library).resolve()
    assets = lib / '02_swf_assets'
    meta = lib / '00_metadata'
    xmlroot = meta / 'source_xml'
    recon = lib / '03_reconstructed_images'
    logical = lib / '04_logical_composites'
    browse = lib / '05_browse'
    build_log = meta / 'final_assembly.log'
    error_log = meta / 'final_assembly_errors.log'
    recon.mkdir(parents=True, exist_ok=True)
    logical.mkdir(parents=True, exist_ok=True)
    browse.mkdir(parents=True, exist_ok=True)
    build_log.write_text('', encoding='utf-8')
    error_log.write_text('', encoding='utf-8')

    def log(message: str) -> None:
        line = f'[{time.strftime("%Y-%m-%d %H:%M:%S")}] {message}'
        print(line, flush=True)
        with build_log.open('a', encoding='utf-8') as handle:
            handle.write(line + '\n')

    def fail(context: str, exc: BaseException) -> None:
        with error_log.open('a', encoding='utf-8') as handle:
            handle.write(f'\n{context}\n')
            handle.write(''.join(traceback.format_exception(exc)))

    manifest = list(csv.DictReader((meta / 'all_unique_swfs.tsv').open(encoding='utf-8'), delimiter='\t'))
    source_to_asset: dict[str, Path] = {}
    asset_to_source: dict[Path, str] = {}
    for row in manifest:
        rel = row['primary_source_path']
        safe = re.sub(r'[^A-Za-z0-9._/-]+', '_', rel[:-4])
        directory = assets / f'{safe}__{row["sha256"][:10]}'
        asset_to_source[directory] = rel
        for duplicate in json.loads(row['all_duplicate_paths_json']):
            source_to_asset[duplicate] = directory

    info_cache: dict[Path, dict | None] = {}
    recon_map: dict[Path, list[Path]] = defaultdict(list)
    candidate_cache: dict[Path, list[dict]] = {}

    def image_info(path: Path) -> dict | None:
        if path in info_cache:
            return info_cache[path]
        try:
            with Image.open(path) as image:
                image = image.convert('RGBA')
                bbox = image.getbbox()
                if bbox is None:
                    result = None
                else:
                    result = {
                        'path': path,
                        'width': image.width,
                        'height': image.height,
                        'area': (bbox[2] - bbox[0]) * (bbox[3] - bbox[1]),
                    }
        except Exception:
            result = None
        info_cache[path] = result
        return result

    log(f'Starting with {len(manifest)} unique SWFs')
    color_files = sorted(assets.rglob('*_colors.png'))
    reconstructed = 0
    for index, color_path in enumerate(color_files, 1):
        try:
            alpha_files = sorted(color_path.parent.glob('*_alphas.png'))
            if not alpha_files:
                continue
            with Image.open(color_path) as color_image:
                color_image = color_image.convert('RGBA')
                alpha_path = None
                for possible in alpha_files:
                    with Image.open(possible) as test:
                        if test.size == color_image.size:
                            alpha_path = possible
                            break
                if alpha_path is None:
                    continue
                with Image.open(alpha_path) as alpha_image:
                    merged = color_image.copy()
                    merged.putalpha(alpha_image.convert('L'))
                    asset_dir = color_path.parent.parents[1]
                    output_dir = recon / asset_dir.relative_to(assets)
                    output_dir.mkdir(parents=True, exist_ok=True)
                    output = output_dir / f'{color_path.stem.replace("_colors", "")}_rgba.png'
                    merged.save(output, compress_level=1)
                    recon_map[asset_dir].append(output)
                    reconstructed += 1
        except Exception as exc:
            fail(f'Alpha reconstruction: {color_path}', exc)
        if index % 100 == 0:
            log(f'Alpha pairs {index}/{len(color_files)}')
    log(f'Reconstructed {reconstructed} RGBA images')

    def candidates(asset_dir: Path) -> list[dict]:
        if asset_dir in candidate_cache:
            return candidate_cache[asset_dir]
        raw: list[tuple[int, Path, str]] = []
        for path in recon_map.get(asset_dir, []):
            raw.append((100, path, 'reconstructed'))
        sprite_root = asset_dir / 'composites' / 'sprite_previews'
        if sprite_root.exists():
            for path in sprite_root.rglob('*.png'):
                raw.append((90, path, 'sprite'))
        root_frame = asset_dir / 'composites' / 'root_frame_001.png'
        if root_frame.exists():
            raw.append((80, root_frame, 'root'))
        for folder, priority in [('shapes', 70), ('morphshapes', 65), ('buttons', 60), ('images', 55)]:
            base = asset_dir / 'fundamental' / folder
            if not base.exists():
                continue
            for path in base.rglob('*.png'):
                if path.name.endswith('_colors.png') or path.name.endswith('_alphas.png'):
                    continue
                raw.append((priority, path, folder))
        rows = []
        for priority, path, kind in raw:
            info = image_info(path)
            if info:
                row = dict(info)
                row['priority'] = priority
                row['kind'] = kind
                rows.append(row)
        candidate_cache[asset_dir] = rows
        return rows

    def best(asset_dir: Path | None, expected_w: int = 0, expected_h: int = 0) -> Path | None:
        if asset_dir is None or not asset_dir.exists():
            return None
        rows = candidates(asset_dir)
        if not rows:
            return None
        def score(row: dict) -> float:
            base = row['priority'] * 1_000_000_000.0 + row['area']
            if expected_w and expected_h:
                wr = min(row['width'], expected_w) / max(row['width'], expected_w)
                hr = min(row['height'], expected_h) / max(row['height'], expected_h)
                exact = 2.0 if (row['width'], row['height']) == (expected_w, expected_h) else 0.0
                base += (wr * hr + exact) * 10_000_000_000.0
            else:
                base += math.log2(max(2, row['area'])) * 100_000_000.0
            return base
        return max(rows, key=score)['path']

    def contact_sheet(files: list[Path], labels: list[str], output: Path, title: str, maximum: int = 120) -> bool:
        valid: list[tuple[Path, str]] = []
        for path, label in zip(files, labels):
            if image_info(path):
                valid.append((path, label))
            if len(valid) >= maximum:
                break
        if not valid:
            return False
        cols, cw, ch, lh, hh = 6, 220, 175, 44, 64
        rows = math.ceil(len(valid) / cols)
        sheet = Image.new('RGB', (cols * cw, hh + rows * (ch + lh)), 'white')
        draw = ImageDraw.Draw(sheet)
        draw.text((16, 18), title[:180], fill='black')
        for i, (path, label) in enumerate(valid):
            x, y = (i % cols) * cw, hh + (i // cols) * (ch + lh)
            try:
                with Image.open(path) as image:
                    image = image.convert('RGBA')
                    image.thumbnail((cw - 16, ch - 16), Image.Resampling.LANCZOS)
                    background = Image.new('RGBA', (cw, ch), (242, 242, 242, 255))
                    background.alpha_composite(image, ((cw - image.width) // 2, (ch - image.height) // 2))
                    sheet.paste(background.convert('RGB'), (x, y))
                draw.text((x + 6, y + ch + 4), label.replace('_', ' ')[:54], fill='black')
            except Exception:
                pass
        output.parent.mkdir(parents=True, exist_ok=True)
        sheet.save(output, quality=88, optimize=True)
        return True

    meadow_rows = []
    meadow_count = 0
    config_root = xmlroot / 'meadows'
    configs = sorted(config_root.rglob('config.xml')) if config_root.exists() else []
    meadow_output = logical / 'meadows_homes_gardens_and_adventures'
    for config_index, config in enumerate(configs, 1):
        try:
            root = ET.parse(config).getroot()
        except Exception as exc:
            fail(f'Meadow config parse: {config}', exc)
            continue
        layers = root.findall('./clientLayout/layers/layer')
        if not layers:
            continue
        name = (root.findtext('name') or config.parent.name).strip()
        zone_id = (root.findtext('zoneID') or config.parent.name).strip()
        parsed = []
        all_tags: set[str] = set()
        for layer_number, layer in enumerate(layers, 1):
            options = []
            for node in layer.findall('image'):
                filename = (node.text or '').strip()
                if not filename:
                    continue
                node_tags = tags(node)
                all_tags.update(node_tags)
                options.append({
                    'filename': filename,
                    'tags': node_tags,
                    'x': signed32(node.attrib.get('x')),
                    'y': signed32(node.attrib.get('y')),
                })
            parsed.append({
                'number': layer_number,
                'type': layer.attrib.get('type', ''),
                'width': signed32(layer.attrib.get('width')),
                'height': signed32(layer.attrib.get('height')),
                'options': options,
            })
        variants = [('default', None)] + [(f'event__{slug(tag)}', tag) for tag in sorted(all_tags)]
        zone_folder = meadow_output / f'zone_{slug(zone_id)}__{slug(name)}'
        zone_folder.mkdir(parents=True, exist_ok=True)
        (zone_folder / 'source_config_path.txt').write_text(config.relative_to(xmlroot).as_posix() + '\n', encoding='utf-8')
        for variant_folder, event_tag in variants:
            selected = []
            for layer in parsed:
                choice = None
                if event_tag:
                    choice = next((option for option in layer['options'] if event_tag in option['tags']), None)
                if choice is None:
                    choice = next((option for option in layer['options'] if not option['tags']), None)
                if choice is None:
                    continue
                source_swf = (config.parent / choice['filename']).relative_to(xmlroot).as_posix()
                asset_dir = source_to_asset.get(source_swf)
                chosen_image = best(asset_dir, layer['width'], layer['height'])
                selected.append({
                    'layer': layer['number'], 'layer_type': layer['type'],
                    'expected_width': layer['width'], 'expected_height': layer['height'],
                    'x': choice['x'], 'y': choice['y'], 'source_swf': source_swf,
                    'asset_directory': asset_dir.relative_to(lib).as_posix() if asset_dir else '',
                    'selected_image': chosen_image.relative_to(lib).as_posix() if chosen_image else '',
                    '_image': chosen_image, '_asset': asset_dir,
                })
            usable = [item for item in selected if item['_image']]
            if not usable:
                continue
            min_x = min([0] + [item['x'] for item in usable])
            min_y = min([0] + [item['y'] for item in usable])
            max_x = max(item['x'] + max(item['expected_width'], image_info(item['_image'])['width']) for item in usable)
            max_y = max(item['y'] + max(item['expected_height'], image_info(item['_image'])['height']) for item in usable)
            full_w, full_h = max(1, max_x - min_x), max(1, max_y - min_y)
            scale = min(1.0, 4096.0 / max(full_w, full_h))
            out_w, out_h = max(1, round(full_w * scale)), max(1, round(full_h * scale))
            canvas = Image.new('RGBA', (out_w, out_h), (0, 0, 0, 0))
            for item in usable:
                try:
                    with Image.open(item['_image']) as layer_image:
                        layer_image = layer_image.convert('RGBA')
                        if scale != 1.0:
                            layer_image = layer_image.resize((max(1, round(layer_image.width * scale)), max(1, round(layer_image.height * scale))), Image.Resampling.LANCZOS)
                        canvas.alpha_composite(layer_image, (round((item['x'] - min_x) * scale), round((item['y'] - min_y) * scale)))
                except Exception as exc:
                    fail(f'Meadow layer: {item["_image"]}', exc)
            variant_dir = zone_folder / variant_folder
            variant_dir.mkdir(parents=True, exist_ok=True)
            full_output = variant_dir / 'full_composite.png'
            canvas.save(full_output, compress_level=1)
            viewport = Image.new('RGBA', (550, 400), (0, 0, 0, 0))
            viewport_used = 0
            for item in selected:
                root_frame = item['_asset'] / 'composites/root_frame_001.png' if item['_asset'] else None
                if root_frame and image_info(root_frame):
                    with Image.open(root_frame) as layer_image:
                        viewport.alpha_composite(layer_image.convert('RGBA'), (0, 0))
                    viewport_used += 1
            viewport_path = variant_dir / 'viewport_550x400.png'
            if viewport_used:
                viewport.save(viewport_path, compress_level=1)
            clean = [{key: value for key, value in item.items() if not key.startswith('_')} for item in selected]
            composition = {
                'name': name, 'zone_id': zone_id, 'variant': event_tag or 'default',
                'source_config': config.relative_to(xmlroot).as_posix(),
                'original_canvas': [full_w, full_h], 'output_canvas': [out_w, out_h],
                'scale': scale, 'layers': clean,
            }
            (variant_dir / 'composition.json').write_text(json.dumps(composition, indent=2), encoding='utf-8')
            meadow_rows.append([zone_id, name, event_tag or 'default', full_output.relative_to(lib).as_posix(), viewport_path.relative_to(lib).as_posix() if viewport_used else '', full_w, full_h, out_w, out_h, scale, len(usable), len(selected) - len(usable)])
            meadow_count += 1
        if config_index % 20 == 0:
            log(f'Meadow configs {config_index}/{len(configs)}; variants {meadow_count}')
    with (meta / 'meadow_compositions.csv').open('w', newline='', encoding='utf-8') as handle:
        writer = csv.writer(handle)
        writer.writerow(['zone_id','name','variant','full_composite','viewport','original_width','original_height','output_width','output_height','scale','usable_layers','missing_layers'])
        writer.writerows(meadow_rows)
    log(f'Generated {meadow_count} meadow/home/garden/adventure variants')

    panel_root = xmlroot / 'xml' / 'panels'
    minigame_output = logical / 'minigames'
    minigame_rows = []
    seen = set()
    def title_for(root: ET.Element, fallback: str) -> str:
        for query in ('./copy/title','./copy/title_screen','./copy/game_title','./title','.//gameTitle','.//title_screen'):
            node = root.find(query)
            text = (node.text or '').strip() if node is not None else ''
            if text and len(text) <= 120 and '<' not in text:
                return text
        for node in root.iter():
            text = (node.text or '').strip()
            if 'title' in node.tag.lower() and text and len(text) <= 120 and '<' not in text:
                return text
        return fallback
    if panel_root.exists():
        for xml_path in sorted(panel_root.rglob('*.xml')):
            try:
                root = ET.parse(xml_path).getroot()
            except Exception:
                continue
            title = title_for(root, xml_path.stem)
            for node in root.findall('.//swf'):
                if (node.attrib.get('type') or '').lower() != 'content':
                    continue
                text = (node.text or '').strip()
                if not text:
                    continue
                basename = Path(text.replace('{panelContent}/', '')).name
                source_swf = f'swf/panel_content/{basename}'
                key = (title, source_swf)
                if key in seen:
                    continue
                seen.add(key)
                asset_dir = source_to_asset.get(source_swf)
                if not asset_dir or not asset_dir.exists():
                    continue
                folder = minigame_output / f'{slug(title)}__{slug(Path(basename).stem)}'
                folder.mkdir(parents=True, exist_ok=True)
                primary = best(asset_dir)
                primary_out = ''
                if primary:
                    target = folder / 'primary_visual.png'
                    with Image.open(primary) as image:
                        image.convert('RGBA').save(target, compress_level=1)
                    primary_out = target.relative_to(lib).as_posix()
                rows = sorted(candidates(asset_dir), key=lambda item: (item['priority'], item['area']), reverse=True)
                sheet = folder / 'asset_contact_sheet.jpg'
                contact_sheet([item['path'] for item in rows], [item['path'].parent.name for item in rows], sheet, f'{title} - {basename}')
                (folder / 'README.txt').write_text(f'Title: {title}\nPanel XML: {xml_path.relative_to(xmlroot).as_posix()}\nContent SWF: {source_swf}\nExtracted folder: {asset_dir.relative_to(lib).as_posix()}\n', encoding='utf-8')
                minigame_rows.append([title, source_swf, xml_path.relative_to(xmlroot).as_posix(), asset_dir.relative_to(lib).as_posix(), primary_out, sheet.relative_to(lib).as_posix() if sheet.exists() else ''])
    with (meta / 'minigame_index.csv').open('w', newline='', encoding='utf-8') as handle:
        writer = csv.writer(handle)
        writer.writerow(['title','source_swf','panel_xml','asset_directory','primary_visual','contact_sheet'])
        writer.writerows(minigame_rows)
    log(f'Built {len(minigame_rows)} minigame/content folders')

    def category(source: str) -> str:
        low = source.lower()
        if low.startswith('meadows/'): return 'meadows_and_locations'
        if '/panel_content/' in low: return 'interface_panels_and_minigames'
        if 'animalfriend' in low or '/animalfriends/' in low: return 'animal_friends_and_pets'
        if '/shops/' in low: return 'shops'
        if '/items/' in low: return 'items_furniture_and_gardens'
        if '/badges/' in low or 'badge' in Path(low).name: return 'badges'
        if '/backgrounds/' in low or 'background' in Path(low).name: return 'backgrounds'
        if any(token in low for token in ('avatar','outfit','hair','wing','wardrobe','clothing','dress','shirt','shoe','accessor')): return 'fairies_clothing_hair_and_wings'
        if '/hotspots/' in low: return 'hotspots_and_interactions'
        if '/giveaways/' in low: return 'giveaways_and_rewards'
        if '/derbycarts/' in low: return 'derby_carts'
        if '/site/' in low or '/promos/' in low: return 'website_and_promotional'
        return 'other_flash_assets'

    visual_rows = []
    category_items: dict[str, list[tuple[Path, str]]] = defaultdict(list)
    asset_entries = sorted(asset_to_source.items(), key=lambda item: item[1])
    for index, (asset_dir, source) in enumerate(asset_entries, 1):
        chosen = best(asset_dir)
        cat = category(source)
        visual_rows.append([source, cat, asset_dir.relative_to(lib).as_posix(), chosen.relative_to(lib).as_posix() if chosen else '', len(recon_map.get(asset_dir, [])), len(list((asset_dir / 'composites/sprite_previews').rglob('*.png'))) if (asset_dir / 'composites/sprite_previews').exists() else 0, len(list((asset_dir / 'fundamental').rglob('*.png'))) if (asset_dir / 'fundamental').exists() else 0])
        if chosen:
            category_items[cat].append((chosen, source))
        if index % 300 == 0:
            log(f'Cataloged {index}/{len(asset_entries)} SWFs')
    with (meta / 'swf_visual_index.csv').open('w', newline='', encoding='utf-8') as handle:
        writer = csv.writer(handle)
        writer.writerow(['source_swf','category','asset_directory','best_preview','reconstructed_count','sprite_preview_count','fundamental_png_count'])
        writer.writerows(visual_rows)
    contact_root = logical / 'category_contact_sheets'
    category_counts = {}
    for cat, pairs in sorted(category_items.items()):
        category_counts[cat] = len(pairs)
        for start in range(0, len(pairs), 120):
            page = pairs[start:start + 120]
            contact_sheet([item[0] for item in page], [Path(item[1]).stem for item in page], contact_root / cat / f'page_{start // 120 + 1:03d}.jpg', f'{cat.replace("_", " ").title()} - page {start // 120 + 1}')
        (contact_root / cat).mkdir(parents=True, exist_ok=True)
        with (contact_root / cat / 'index.csv').open('w', newline='', encoding='utf-8') as handle:
            writer = csv.writer(handle)
            writer.writerow(['source_swf','preview'])
            for preview, source in pairs:
                writer.writerow([source, preview.relative_to(lib).as_posix()])
    with (meta / 'category_summary.csv').open('w', newline='', encoding='utf-8') as handle:
        writer = csv.writer(handle)
        writer.writerow(['category','assets_with_preview'])
        writer.writerows(sorted(category_counts.items()))

    image_rows = []
    extensions = Counter()
    total_bytes = 0
    total_files = 0
    for path in lib.rglob('*'):
        if not path.is_file():
            continue
        total_files += 1
        size = path.stat().st_size
        total_bytes += size
        extension = path.suffix.lower() or '[none]'
        extensions[extension] += 1
        if extension in IMAGE_EXTS:
            width = height = mode = ''
            if extension != '.svg':
                try:
                    with Image.open(path) as image:
                        width, height, mode = image.width, image.height, image.mode
                except Exception:
                    pass
            image_rows.append([path.relative_to(lib).as_posix(), extension, size, width, height, mode])
    with (meta / 'image_index.csv').open('w', newline='', encoding='utf-8') as handle:
        writer = csv.writer(handle)
        writer.writerow(['path','extension','bytes','width','height','mode'])
        writer.writerows(image_rows)

    browser_records = []
    for source, cat, asset_dir, preview, *_ in visual_rows:
        if preview:
            browser_records.append({'source': source, 'category': cat, 'preview': '../' + preview, 'folder': '../' + asset_dir})
    browser_html = '''<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Pixie Hollow Image Library</title><style>body{font-family:system-ui;margin:0;background:#f4efff;color:#241c38}header{position:sticky;top:0;background:#2b1f46;color:#fff;padding:16px;z-index:2}input,select,button{font:inherit;padding:10px;margin:4px;border:0;border-radius:8px}main{padding:16px;display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:14px}article{background:#fff;padding:10px;border-radius:12px;box-shadow:0 2px 10px #0002}img{width:100%;height:180px;object-fit:contain;background:#eee;border-radius:8px}.small{font-size:12px;overflow-wrap:anywhere}</style><header><h1>Pixie Hollow Complete Image Library</h1><input id="q" placeholder="Search"><select id="c"><option value="">All categories</option></select><button id="more">Show more</button><span id="count"></span></header><main id="grid"></main><script>const data=__DATA__;const cats=[...new Set(data.map(x=>x.category))].sort();const q=document.getElementById('q'),c=document.getElementById('c'),g=document.getElementById('grid'),count=document.getElementById('count');for(const x of cats){const o=document.createElement('option');o.value=x;o.textContent=x.replaceAll('_',' ');c.appendChild(o)}let limit=120;function render(){const s=q.value.toLowerCase(),cat=c.value,rows=data.filter(x=>(!cat||x.category===cat)&&(!s||(x.source+' '+x.category).toLowerCase().includes(s)));g.innerHTML='';for(const x of rows.slice(0,limit)){const a=document.createElement('article');a.innerHTML=`<a href="${encodeURI(x.folder)}"><img loading="lazy" src="${encodeURI(x.preview)}"></a><div class="small">${x.category.replaceAll('_',' ')}</div><div class="small">${x.source}</div>`;g.appendChild(a)}count.textContent=`${Math.min(limit,rows.length)} of ${rows.length}`}q.oninput=()=>{limit=120;render()};c.onchange=()=>{limit=120;render()};document.getElementById('more').onclick=()=>{limit+=120;render()};render();</script>'''.replace('__DATA__', json.dumps(browser_records, ensure_ascii=True))
    (browse / 'index.html').write_text(browser_html, encoding='utf-8')
    (browse / 'README.txt').write_text('Extract the ZIP, then open index.html. The browser is fully offline.\n', encoding='utf-8')

    category_lines = '\n'.join(f'- `{name}`: {count:,} assets with previews' for name, count in sorted(category_counts.items()))
    readme = f'''# Pixie Hollow Complete Image Library

Generated from the public `PixieHollowRE/web` repository.

## Start here

1. Extract this ZIP completely.
2. Open `05_browse/index.html` for the offline searchable visual browser.
3. Browse `04_logical_composites/` for complete meadow/event combinations, minigame folders, and category contact sheets.
4. Browse `02_swf_assets/` for every fundamental bitmap, vector shape, button, sprite preview, and root-frame render extracted from each SWF.

## Folders

- `00_metadata/`: complete SWF hash map, original XML/JSON configuration, image indexes, minigame mappings, meadow composition records, and extraction/build logs.
- `01_loose_images/`: every normal image file that was already loose in the repository, preserving its original path.
- `02_swf_assets/`: all {len(manifest):,} unique hash-deduplicated SWFs extracted into fundamental and composite images. The source repository contained 3,737 SWF paths, including identical archived copies.
- `03_reconstructed_images/`: {reconstructed:,} full-resolution transparent RGBA images rebuilt from separate Flash color and alpha planes.
- `04_logical_composites/`: {meadow_count:,} meadow/home/garden/adventure variants, {len(minigame_rows):,} friendly minigame/content folders, and paginated category contact sheets.
- `05_browse/`: offline searchable HTML browser.

## Inside each SWF folder

- `fundamental/images/`: embedded raster data.
- `fundamental/shapes/`: Flash vector shapes rendered to transparent PNG.
- `fundamental/morphshapes/`: rendered morph endpoints.
- `fundamental/buttons/`: rendered button states.
- `fundamental/symbolClass/symbols.csv`: symbol names where available.
- `composites/root_frame_001.png`: first root timeline frame.
- `composites/sprite_previews/`: first-frame renders of individual sprites for important visual categories.
- `context/`: source path, SHA-256, and duplicate repository paths.

## Meadow composites

Each variant folder contains `full_composite.png`, a 550x400 viewport where available, and `composition.json` identifying every selected source SWF and image. Native size is retained when practical. Extremely large homes/worlds are proportionally limited to a 4,096-pixel longest side; all original full-resolution layers remain available elsewhere in the library.

## Categories

{category_lines}

## Statistics

- Unique SWFs: {len(manifest):,}
- Reconstructed RGBA images: {reconstructed:,}
- Meadow/home/garden/adventure variants: {meadow_count:,}
- Minigame/content mappings: {len(minigame_rows):,}
- Indexed ordinary images: {len(image_rows):,}
- Total files: {total_files:,}
- Uncompressed bytes: {total_bytes:,}
- File types: {', '.join(f'{key}={value:,}' for key, value in extensions.most_common(15))}

## Limitations

Some live screens depend on ActionScript, imported SWFs, player inventory, selected dye colors, fairy rigging, server state, or later animation frames. This archive preserves the underlying pieces and creates combinations that can be reconstructed reliably, but not every possible live state is pixel-identical.

## Rights

The source material is from the public `PixieHollowRE/web` archive. This conversion does not change ownership of Disney/Pixie Hollow or third-party artwork. Review applicable rights before redistribution or commercial use.
'''
    (lib / 'README.md').write_text(readme, encoding='utf-8')
    summary = {
        'unique_swfs': len(manifest), 'reconstructed_rgba_images': reconstructed,
        'meadow_variants': meadow_count, 'minigame_mappings': len(minigame_rows),
        'indexed_images': len(image_rows), 'total_files': total_files,
        'uncompressed_bytes': total_bytes, 'categories': category_counts,
    }
    (meta / 'build_summary.json').write_text(json.dumps(summary, indent=2), encoding='utf-8')
    log('Final assembly complete: ' + json.dumps(summary, sort_keys=True))


if __name__ == '__main__':
    main()
