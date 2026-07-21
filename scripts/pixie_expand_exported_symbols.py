#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path

from pixie_dye_companion_build import (
    FORCED_TWO_COLOR_ITEM_IDS,
    browser_html,
    choose_image,
    flags_for_attribute,
    generate_masks,
    load_graphs,
    parse_all_item_catalogs,
    source_reference_index,
    write_json,
)


def mapping_key_for_symbol(source_swf: str, tag_id: int) -> str:
    return f"symbol:{source_swf}#{tag_id}"


def build_mapping(
    key: str,
    source_swf: str,
    symbol: dict,
    root_record: dict,
    catalogs: dict[int, dict],
    by_tag: dict[tuple[str, int], list[dict]],
) -> dict:
    item_id = root_record.get("item_id")
    item_id = int(item_id) if item_id is not None else None
    catalog = catalogs.get(item_id, {}) if item_id is not None else {}
    xml_slot_editable = catalog.get("xml_slot_editable", flags_for_attribute(None))
    mapping = {
        "mapping_key": key,
        "item_id": item_id,
        "name": catalog.get("name") or root_record.get("item_name") or symbol.get("class_name") or "",
        "catalog": catalog.get("catalog") or ("exported_symbol_without_catalog_id" if item_id is None else "home_library"),
        "catalog_match_status": "catalog_id_proven" if item_id is not None else "no_authoritative_catalog_id",
        "dyeable_attribute": catalog.get("dyeable_attribute"),
        "xml_slot_editable": xml_slot_editable,
        "root_image": root_record["canonical_path"],
        "root_image_record": root_record,
        "source_swf": source_swf,
        "root_tag_id": int(symbol["tag_id"]),
        "symbol_class": symbol.get("class_name", ""),
        "components": [],
        "dye_targets": {"1": [], "2": []},
    }
    seen = set()
    for descendant in symbol.get("descendants", []):
        child_tag = int(descendant["tag_id"])
        component_images = [
            row["canonical_path"]
            for row in by_tag.get((source_swf, child_tag), [])
        ]
        unique = (
            child_tag,
            descendant.get("parent_tag_id"),
            descendant.get("depth"),
            descendant.get("instance_name"),
            json.dumps(descendant.get("matrix_from_root")),
        )
        if unique in seen:
            continue
        seen.add(unique)
        component = {
            "tag_id": child_tag,
            "parent_tag_id": descendant.get("parent_tag_id"),
            "depth": descendant.get("depth"),
            "instance_name": descendant.get("instance_name"),
            "matrix_from_root": descendant.get("matrix_from_root"),
            "image_paths": sorted(set(component_images)),
            "path": descendant.get("path", []),
        }
        mapping["components"].append(component)
        if descendant.get("instance_name") in {"color1", "color2"}:
            slot = descendant["instance_name"][-1]
            mapping["dye_targets"][slot].append(
                {
                    "tag_id": child_tag,
                    "parent_tag_id": descendant.get("parent_tag_id"),
                    "depth": descendant.get("depth"),
                    "matrix_from_root": descendant.get("matrix_from_root"),
                    "image_paths": sorted(set(component_images)),
                }
            )
    artwork_slots = sorted(
        int(slot)
        for slot, values in mapping["dye_targets"].items()
        if values
    )
    if item_id in FORCED_TWO_COLOR_ITEM_IDS:
        artwork_slots = sorted(set(artwork_slots) | {1, 2})
    mapping["dye_slot_numbers"] = artwork_slots
    mapping["dye_slot_count"] = len(artwork_slots)
    mapping["dye_slots"] = [
        {
            "slot_number": slot,
            "xml_flag_index": slot - 1,
            "instance_name": f"color{slot}",
            "editable": (
                True
                if item_id in FORCED_TWO_COLOR_ITEM_IDS
                else bool(xml_slot_editable[slot - 1])
            ),
            "hard_coded_force_two_color_exception": item_id in FORCED_TWO_COLOR_ITEM_IDS,
            "editability_basis": (
                "catalog dyeable attribute"
                if item_id is not None
                else "default true because no authoritative catalog record is linked"
            ),
        }
        for slot in artwork_slots
    ]
    return mapping


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True)
    parser.add_argument("--library", required=True)
    parser.add_argument("--graphs", required=True)
    parser.add_argument("--output", required=True)
    arguments = parser.parse_args()

    source = Path(arguments.source).resolve()
    library = Path(arguments.library).resolve()
    graph_root = Path(arguments.graphs).resolve()
    output = Path(arguments.output).resolve()
    config_root = output / "10_dye_and_composition" / "config"

    image_records = json.loads(
        (library / "00_metadata" / "image_index.json").read_text(encoding="utf-8")
    )
    size_limits = json.loads(
        (library / "00_metadata" / "item_size_limits.json").read_text(encoding="utf-8")
    )
    colors = json.loads((config_root / "dyes.json").read_text(encoding="utf-8"))
    existing_items = json.loads(
        (config_root / "item_component_map.json").read_text(encoding="utf-8")
    )
    catalogs = parse_all_item_catalogs(source)
    graphs, _ = load_graphs(graph_root)
    by_tag, by_class = source_reference_index(image_records)

    item_key_by_root: dict[tuple[str, int], str] = {}
    for item_key, mapping in existing_items.items():
        source_swf = mapping.get("source_swf")
        tag_id = mapping.get("root_tag_id")
        if source_swf and tag_id is not None:
            item_key_by_root[(source_swf, int(tag_id))] = item_key
            mapping["mapping_key"] = item_key

    all_mappings: dict[str, dict] = dict(existing_items)
    symbol_mappings: dict[str, dict] = {}
    exported_key_by_root: dict[tuple[str, int], str] = {}

    for source_swf, graph in sorted(graphs.items()):
        for symbol in graph.get("exported_symbols", []):
            tag_id = int(symbol["tag_id"])
            root_record = choose_image(by_tag.get((source_swf, tag_id), []))
            if root_record is None and symbol.get("class_name"):
                root_record = choose_image(
                    by_class.get((source_swf, symbol["class_name"]), [])
                )
            if root_record is None:
                continue
            key = item_key_by_root.get((source_swf, tag_id))
            if key is None:
                key = mapping_key_for_symbol(source_swf, tag_id)
                mapping = build_mapping(
                    key,
                    source_swf,
                    symbol,
                    root_record,
                    catalogs,
                    by_tag,
                )
                symbol_mappings[key] = mapping
                all_mappings[key] = mapping
            exported_key_by_root[(source_swf, tag_id)] = key

    mask_report = generate_masks(
        output,
        library,
        all_mappings,
        graphs,
        by_tag,
    )

    image_parent_rows = []
    unmatched_rows = []
    mapping_key_by_path: dict[str, str] = {}
    for record in image_records:
        relationships = []
        exported_as = []
        exported_mapping_keys = set()
        component_of_mapping_keys = set()
        component_of_item_ids = set()
        direct_dye_slots = set()
        for reference in record.get("source_references", []):
            source_swf = reference.get("source_swf") or ""
            tag_id = reference.get("tag_id")
            if not source_swf or tag_id is None:
                continue
            tag_id = int(tag_id)
            graph = graphs.get(source_swf)
            if not graph:
                continue
            exported_key = exported_key_by_root.get((source_swf, tag_id))
            if exported_key:
                exported_mapping_keys.add(exported_key)
                exported_as.append(
                    {
                        "source_swf": source_swf,
                        "tag_id": tag_id,
                        "class_name": all_mappings[exported_key].get("symbol_class", ""),
                        "mapping_key": exported_key,
                    }
                )
            for parent in graph.get("child_to_parents", {}).get(str(tag_id), []):
                parent_key = exported_key_by_root.get(
                    (source_swf, int(parent["parent_tag_id"]))
                )
                relation = {
                    "source_swf": source_swf,
                    "tag_id": tag_id,
                    **parent,
                    "parent_mapping_key": parent_key,
                }
                relationships.append(relation)
                if parent_key:
                    component_of_mapping_keys.add(parent_key)
                    parent_item_id = all_mappings[parent_key].get("item_id")
                    if parent_item_id is not None:
                        component_of_item_ids.add(int(parent_item_id))
                if parent.get("instance_name") in {"color1", "color2"}:
                    direct_dye_slots.add(int(parent["instance_name"][-1]))

        role = "catalog_matched_export_or_render" if record.get("item_id") is not None else "unresolved_internal_tag"
        if record.get("item_id") is None:
            if direct_dye_slots:
                role = "named_dye_component"
            elif component_of_mapping_keys:
                role = "nested_component_of_exported_object"
            elif exported_mapping_keys:
                role = "complete_exported_symbol_without_catalog_match"
            elif "root_frame" in Path(record["canonical_path"]).stem:
                role = "root_timeline_preview"
            elif record.get("category") == "scene_layers":
                role = "scene_layer_or_scene_component"

        preferred_key = None
        if record.get("item_id") is not None and str(record["item_id"]) in all_mappings:
            preferred_key = str(record["item_id"])
        elif exported_mapping_keys:
            preferred_key = sorted(exported_mapping_keys)[0]
        if preferred_key:
            mapping_key_by_path[record["canonical_path"]] = preferred_key

        image_row = {
            "canonical_path": record["canonical_path"],
            "item_id": record.get("item_id"),
            "item_name": record.get("item_name", ""),
            "category": record.get("category"),
            "role": role,
            "preferred_mapping_key": preferred_key,
            "exported_as": exported_as,
            "direct_parent_relationships": relationships,
            "component_of_mapping_keys": sorted(component_of_mapping_keys),
            "component_of_item_ids": sorted(component_of_item_ids),
            "direct_dye_slot_numbers": sorted(direct_dye_slots),
            "source_references": record.get("source_references", []),
        }
        image_parent_rows.append(image_row)
        if record.get("item_id") is None:
            unmatched_rows.append(image_row)

    parent_by_path = {row["canonical_path"]: row for row in image_parent_rows}
    browser_rows = []
    for record in image_records:
        if not record.get("width"):
            continue
        item_id = record.get("item_id")
        mapping_key = mapping_key_by_path.get(record["canonical_path"])
        mapping = all_mappings.get(mapping_key, {}) if mapping_key else {}
        slots = mapping.get("dye_slots", [])
        limits = size_limits.get(str(item_id), {}) if item_id is not None else {}
        parent = parent_by_path.get(record["canonical_path"], {})
        browser_rows.append(
            {
                "path": record["canonical_path"],
                "category": record.get("category"),
                "item_id": item_id,
                "name": record.get("item_name") or record.get("symbol_class") or Path(record["canonical_path"]).stem,
                "width": record.get("width"),
                "height": record.get("height"),
                "sources": len(record.get("source_references", [])),
                "minimum_percent": limits.get("minimum_percent"),
                "maximum_percent": limits.get("maximum_percent"),
                "default_percent": limits.get("default_percent"),
                "mapping_key": mapping_key,
                "dye_slot_count": len(slots),
                "editable_dye_slot_count": sum(1 for slot in slots if slot.get("editable")),
                "dye_slots": slots,
                "has_exact_dye_masks": bool(mapping.get("dye_slot_masks")),
                "mapping_role": parent.get("role"),
                "component_of_mapping_keys": parent.get("component_of_mapping_keys", []),
                "component_of_item_ids": parent.get("component_of_item_ids", []),
            }
        )

    write_json(config_root / "item_component_map.json", existing_items)
    write_json(config_root / "symbol_component_map.json", symbol_mappings)
    write_json(config_root / "object_component_map.json", all_mappings)
    write_json(config_root / "image_parent_map.json", image_parent_rows)
    write_json(config_root / "unmatched_resolution.json", unmatched_rows)
    write_json(config_root / "browser_rows.json", browser_rows)
    write_json(config_root / "additional_mask_report.json", mask_report)

    updated_browser = browser_html(browser_rows, colors, all_mappings)
    updated_browser = updated_browser.replace(
        "itemComponents[String(row.item_id)]||null",
        "itemComponents[row.mapping_key]||null",
    )
    (output / "09_browse_library.html").write_text(updated_browser, encoding="utf-8")
    (output / "10_dye_and_composition" / "09_browse_library_dye_lab.html").write_text(updated_browser, encoding="utf-8")

    summary_path = output / "10_dye_and_composition" / "BUILD_SUMMARY.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    summary.update(
        {
            "catalog_item_component_mappings": len(existing_items),
            "additional_exported_symbol_mappings": len(symbol_mappings),
            "all_visible_exported_object_mappings": len(all_mappings),
            "all_library_images_classified": len(image_parent_rows),
            "additional_mask_report": mask_report,
        }
    )
    write_json(summary_path, summary)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
