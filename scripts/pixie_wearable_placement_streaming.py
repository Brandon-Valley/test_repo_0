#!/usr/bin/env python3
from __future__ import annotations
import argparse, hashlib, json, sys
from collections import defaultdict
from pathlib import Path

import pixie_wearable_placement_extractor as base


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument('--xml-dir', required=True)
    ap.add_argument('--source-root', required=True)
    ap.add_argument('--output', required=True)
    args = ap.parse_args()

    xml_dir = Path(args.xml_dir)
    source_root = Path(args.source_root)
    symbols = []
    class_index = defaultdict(list)
    poses = {}
    outfits = defaultdict(lambda: {'symbols': [], 'categories': set()})
    doc_count = 0

    for path in sorted(xml_dir.rglob('*.xml')):
        reltxt = path.with_suffix('.source.txt')
        source = reltxt.read_text(errors='ignore').strip() if reltxt.exists() else path.stem + '.swf'
        try:
            doc = base.SwfDoc(path, source)
        except Exception as exc:
            print('parse failure', path, exc, file=sys.stderr)
            continue
        doc_count += 1

        sha = None
        swf_path = source_root / doc.source_swf
        if swf_path.exists():
            h = hashlib.sha256()
            with swf_path.open('rb') as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b''):
                    h.update(block)
            sha = h.hexdigest()

        for cid, name in doc.symbols:
            category, confidence, evidence = base.classify(name)
            bounds = doc.bounds(cid)
            display_list = doc.display_list(cid)
            entry = {
                'symbol_id': f'{doc.source_swf}::{name}',
                'class_name': name,
                'character_id': cid,
                'source_swf': doc.source_swf,
                'source_swf_sha256': sha,
                'category': category,
                'classification_confidence': confidence,
                'classification_evidence': evidence,
                'heuristic_outfit_group': base.group_key(name),
                'local_bounds': base.rect_json(bounds),
                'display_list': display_list,
                'named_layers': [x for x in display_list if x.get('name')],
                'origin_px_in_trimmed_image': (
                    {'x': base.r(-bounds[0] / 20), 'y': base.r(-bounds[1] / 20)}
                    if bounds else None
                ),
            }
            symbols.append(entry)
            class_index[name.lower()].append({
                'symbol_id': entry['symbol_id'],
                'class_name': name,
                'source_swf': doc.source_swf,
                'character_id': cid,
                'category': category,
                'local_bounds': entry['local_bounds'],
            })

            slots = doc.slot_paths(cid)
            if slots and (
                name in base.BODY_WORDS
                or 'pose' in name.lower()
                or 'animation' in name.lower()
                or name == 'VectorModelContainer'
            ):
                poses[entry['symbol_id']] = {
                    'pose_name': name,
                    'source_swf': doc.source_swf,
                    'character_id': cid,
                    'slots': slots,
                }

            if category != 'unclassified':
                key = f"{doc.source_swf}::{entry['heuristic_outfit_group']}"
                outfits[key]['symbols'].append(entry['symbol_id'])
                outfits[key]['categories'].add(category)

        del doc

    items = base.parse_items(source_root)
    for item in items:
        for piece in item['pieces']:
            piece['placement_resolution'] = base.resolve_piece(item, piece, class_index)

    outfit_groups = [
        {
            'group_id': key,
            'source_swf': key.split('::')[0],
            'heuristic_key': key.split('::', 1)[1],
            'symbols': value['symbols'],
            'categories': sorted(value['categories']),
            'confidence': 'heuristic_name_grouping',
        }
        for key, value in outfits.items()
        if len(value['symbols']) > 1
    ]

    meta = {
        'schema_version': '1.0.0',
        'generated_by': 'Pixie Hollow wearable placement extractor',
        'coordinate_system': {
            'source_units': 'SWF twips',
            'twips_per_pixel': 20,
            'matrix_order': 'x=a*x+c*y+tx; y=b*x+d*y+ty',
            'trimmed_png_usage': 'Place the image so its local origin is at origin_px_in_trimmed_image, then apply the pose slot affine matrix.',
        },
        'important_model_note': 'Items do not generally store a standalone x/y. Catalog records select one or more piece slots and a class/frame. Placement is the composition of the wearable symbol local authoring geometry with the selected avatar pose slot matrix.',
        'catalog_limitations': 'Catalog item IDs are included wherever item XML exists in the archive. Every extractable wearable class symbol is included even when no item-ID catalog record survives.',
        'resolution_notes': [
            'Legacy clothing frames map directly to numbered classes such as frame 11 -> Chest11.',
            'Legacy accessories reserve frame 1 for none, so frame 12 -> Necklace11 and frame 9 -> Bracelet8.',
            'Shoe artwork may use a ShoeLeft class for both left and right slots; pose transforms are preserved separately.',
            'Later monthly bundles use descriptive class names and appear in wearable_symbols/outfit_groups even without an item ID.',
        ],
    }

    data = {
        '_meta': meta,
        'statistics': {
            'swf_xml_documents': doc_count,
            'wearable_and_avatar_symbols': len(symbols),
            'pose_definitions': len(poses),
            'catalog_item_designs': len(items),
            'heuristic_outfit_groups': len(outfit_groups),
            'catalog_piece_records': sum(len(x['pieces']) for x in items),
            'resolved_catalog_piece_records': sum(
                p['placement_resolution']['resolved']
                for x in items for p in x['pieces']
            ),
        },
        'canonical_piece_slots': sorted(base.SLOT_WORDS),
        'poses': poses,
        'catalog_items': items,
        'wearable_symbols': symbols,
        'outfit_groups': sorted(outfit_groups, key=lambda x: x['group_id']),
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(data, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    print(json.dumps(data['statistics'], indent=2))
    print('output bytes', output.stat().st_size)


if __name__ == '__main__':
    main()
