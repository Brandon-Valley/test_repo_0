#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import re
from collections import defaultdict
from pathlib import Path


class BitReader:
    def __init__(self, data: bytes, bit_position: int = 0) -> None:
        self.data = data
        self.bit_position = bit_position

    def read(self, count: int) -> int:
        value = 0
        for _ in range(count):
            byte = self.data[self.bit_position // 8]
            shift = 7 - self.bit_position % 8
            value = value << 1 | ((byte >> shift) & 1)
            self.bit_position += 1
        return value

    def read_signed(self, count: int) -> int:
        value = self.read(count)
        if count and value & (1 << (count - 1)):
            value -= 1 << count
        return value

    def align(self) -> None:
        self.bit_position = (self.bit_position + 7) // 8 * 8

    @property
    def byte_position(self) -> int:
        return self.bit_position // 8


def parse_rect(data: bytes, offset: int = 0) -> tuple[list[float], int]:
    reader = BitReader(data, offset * 8)
    bits = reader.read(5)
    values = [reader.read_signed(bits) for _ in range(4)]
    reader.align()
    return [float(value) for value in values], reader.byte_position


def parse_matrix(data: bytes, offset: int = 0) -> tuple[list[float], int]:
    reader = BitReader(data, offset * 8)
    a = d = 1.0
    b = c = 0.0
    if reader.read(1):
        bits = reader.read(5)
        a = reader.read_signed(bits) / 65536.0
        d = reader.read_signed(bits) / 65536.0
    if reader.read(1):
        bits = reader.read(5)
        b = reader.read_signed(bits) / 65536.0
        c = reader.read_signed(bits) / 65536.0
    bits = reader.read(5)
    tx = float(reader.read_signed(bits) if bits else 0)
    ty = float(reader.read_signed(bits) if bits else 0)
    reader.align()
    return [a, b, c, d, tx, ty], reader.byte_position


def parse_place_object_2(raw: bytes) -> dict:
    flags = raw[0]
    position = 1
    depth = int.from_bytes(raw[position:position + 2], "little")
    position += 2
    child_id = None
    if flags & 0x02:
        child_id = int.from_bytes(raw[position:position + 2], "little")
        position += 2
    matrix = [1.0, 0.0, 0.0, 1.0, 0.0, 0.0]
    if flags & 0x04:
        matrix, position = parse_matrix(raw, position)
    return {
        "flags": flags,
        "depth": depth,
        "child_tag_id": child_id,
        "matrix": matrix,
    }


def extract_raw_hex(line: str) -> str:
    match = re.search(r"len=\s*\d+\s+(.*)$", line)
    if not match:
        return ""
    prefix = match.group(1).split("...", 1)[0]
    return " ".join(re.findall(r"\b[0-9A-Fa-f]{2}\b", prefix))


def transform_bounds(bounds: list[float], matrix: list[float]) -> list[float]:
    xmin, xmax, ymin, ymax = bounds
    a, b, c, d, tx, ty = matrix
    points = [
        (a * x + c * y + tx, b * x + d * y + ty)
        for x in (xmin, xmax)
        for y in (ymin, ymax)
    ]
    return [
        min(point[0] for point in points),
        max(point[0] for point in points),
        min(point[1] for point in points),
        max(point[1] for point in points),
    ]


def union_bounds(bounds_list: list[list[float]]) -> list[float] | None:
    bounds_list = [bounds for bounds in bounds_list if bounds]
    if not bounds_list:
        return None
    return [
        min(bounds[0] for bounds in bounds_list),
        max(bounds[1] for bounds in bounds_list),
        min(bounds[2] for bounds in bounds_list),
        max(bounds[3] for bounds in bounds_list),
    ]


def compose_matrix(parent: list[float], child: list[float]) -> list[float]:
    a, b, c, d, tx, ty = parent
    e, f, g, h, ux, uy = child
    return [
        a * e + c * f,
        b * e + d * f,
        a * g + c * h,
        b * g + d * h,
        a * ux + c * uy + tx,
        b * ux + d * uy + ty,
    ]


def read_symbols(path: Path) -> dict[int, str]:
    symbols: dict[int, str] = {}
    if not path.exists():
        return symbols
    with path.open(encoding="utf-8", errors="ignore") as handle:
        for row in csv.reader(handle, delimiter=";"):
            if len(row) < 2:
                continue
            try:
                symbols[int(row[0])] = row[1].strip().strip('"')
            except ValueError:
                continue
    return symbols


def parse_dump(path: Path) -> dict:
    nodes: dict[int, dict] = {}
    root_placements: list[dict] = []
    current_sprite: int | None = None

    define_pattern = re.compile(
        r"^[0-9A-Fa-f]+:(\s+)\d+\.\s+(Define\w+)\s+\(chid:\s*(\d+)\)"
    )
    place_pattern = re.compile(
        r"^[0-9A-Fa-f]+:(\s+)\d+\.\s+PlaceObject2\s+\(chid:\s*(\d+),\s*dpt:\s*(\d+)"
    )
    show_frame_pattern = re.compile(r"^[0-9A-Fa-f]+:(\s+)\d+\.\s+ShowFrame")

    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        define_match = define_pattern.match(line)
        if define_match:
            indentation = len(define_match.group(1))
            is_nested = indentation >= 6
            if is_nested:
                continue
            tag_type = define_match.group(2)
            tag_id = int(define_match.group(3))
            raw_hex = extract_raw_hex(line)
            node = nodes.setdefault(
                tag_id,
                {
                    "tag_id": tag_id,
                    "tag_type": tag_type,
                    "bounds_twips": None,
                    "placements": [],
                },
            )
            node["tag_type"] = tag_type
            if tag_type.startswith("DefineShape") and raw_hex:
                try:
                    raw = bytes.fromhex(raw_hex)
                    node["bounds_twips"], _ = parse_rect(raw, 2)
                except Exception as error:
                    node["bounds_error"] = str(error)
            current_sprite = tag_id if tag_type == "DefineSprite" else None
            continue

        place_match = place_pattern.match(line)
        if place_match:
            indentation = len(place_match.group(1))
            is_nested = indentation >= 6
            child_id = int(place_match.group(2))
            depth = int(place_match.group(3))
            name_match = re.search(r'nm:\s*"([^"]+)"', line)
            instance_name = name_match.group(1) if name_match else None
            raw_hex = extract_raw_hex(line)
            placement = {
                "child_tag_id": child_id,
                "depth": depth,
                "instance_name": instance_name,
                "matrix": [1.0, 0.0, 0.0, 1.0, 0.0, 0.0],
            }
            if raw_hex:
                try:
                    parsed = parse_place_object_2(bytes.fromhex(raw_hex))
                    placement.update(parsed)
                except Exception as error:
                    placement["parse_error"] = str(error)
                    placement["raw_hex"] = raw_hex
            if is_nested and current_sprite is not None:
                nodes[current_sprite]["placements"].append(placement)
            elif not is_nested:
                root_placements.append(placement)
                current_sprite = None
            continue

        show_match = show_frame_pattern.match(line)
        if show_match and current_sprite is not None and len(show_match.group(1)) >= 6:
            # The exported browser images and item previews use frame 1.  Stop
            # collecting placements for later frames, but retain the first one.
            current_sprite = None

    graph = {"nodes": nodes, "root_placements": root_placements}
    compute_sprite_bounds(graph)
    return graph


def compute_sprite_bounds(graph: dict) -> None:
    nodes: dict[int, dict] = graph["nodes"]
    visiting: set[int] = set()

    def calculate(tag_id: int) -> list[float] | None:
        node = nodes.get(tag_id)
        if node is None:
            return None
        if node.get("bounds_computed"):
            return node.get("bounds_twips")
        if tag_id in visiting:
            return node.get("bounds_twips")
        visiting.add(tag_id)
        if node.get("tag_type") == "DefineSprite":
            transformed = []
            for placement in node.get("placements", []):
                child_bounds = calculate(placement["child_tag_id"])
                if child_bounds:
                    transformed.append(transform_bounds(child_bounds, placement["matrix"]))
            node["bounds_twips"] = union_bounds(transformed)
        node["bounds_computed"] = True
        visiting.remove(tag_id)
        return node.get("bounds_twips")

    for node_id in list(nodes):
        calculate(node_id)


def collect_descendants(graph: dict, root_tag_id: int) -> list[dict]:
    nodes = graph["nodes"]
    output: list[dict] = []

    def walk(tag_id: int, matrix: list[float], path: list[dict], active: set[int]) -> None:
        if tag_id in active:
            return
        node = nodes.get(tag_id)
        if not node:
            return
        for placement in node.get("placements", []):
            combined = compose_matrix(matrix, placement["matrix"])
            step = {
                "parent_tag_id": tag_id,
                "child_tag_id": placement["child_tag_id"],
                "depth": placement["depth"],
                "instance_name": placement.get("instance_name"),
                "matrix": placement["matrix"],
            }
            row = {
                "tag_id": placement["child_tag_id"],
                "parent_tag_id": tag_id,
                "depth": placement["depth"],
                "instance_name": placement.get("instance_name"),
                "matrix_from_root": combined,
                "path": path + [step],
            }
            output.append(row)
            walk(placement["child_tag_id"], combined, path + [step], active | {tag_id})

    walk(root_tag_id, [1.0, 0.0, 0.0, 1.0, 0.0, 0.0], [], set())
    return output


def build_graph(dump_path: Path, symbols_path: Path, source_swf: str) -> dict:
    parsed = parse_dump(dump_path)
    symbols = read_symbols(symbols_path)
    child_to_parents: dict[int, list[dict]] = defaultdict(list)
    for parent_id, node in parsed["nodes"].items():
        for placement in node.get("placements", []):
            child_to_parents[placement["child_tag_id"]].append(
                {
                    "parent_tag_id": parent_id,
                    "depth": placement["depth"],
                    "instance_name": placement.get("instance_name"),
                    "matrix": placement["matrix"],
                }
            )

    exported_symbols = []
    for tag_id, class_name in sorted(symbols.items()):
        descendants = collect_descendants(parsed, tag_id)
        dye_targets = [
            row
            for row in descendants
            if row.get("instance_name") in {"color1", "color2"}
        ]
        exported_symbols.append(
            {
                "tag_id": tag_id,
                "class_name": class_name,
                "bounds_twips": parsed["nodes"].get(tag_id, {}).get("bounds_twips"),
                "direct_placements": parsed["nodes"].get(tag_id, {}).get("placements", []),
                "descendants": descendants,
                "dye_targets": dye_targets,
                "dye_slot_numbers": sorted(
                    {
                        int(row["instance_name"][-1])
                        for row in dye_targets
                        if row.get("instance_name") in {"color1", "color2"}
                    }
                ),
            }
        )

    return {
        "schema_version": 1,
        "source_swf": source_swf,
        "nodes": {str(key): value for key, value in sorted(parsed["nodes"].items())},
        "root_placements": parsed["root_placements"],
        "exported_symbols": exported_symbols,
        "child_to_parents": {
            str(key): value for key, value in sorted(child_to_parents.items())
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dump", required=True)
    parser.add_argument("--symbols", required=True)
    parser.add_argument("--source-swf", required=True)
    parser.add_argument("--output", required=True)
    arguments = parser.parse_args()

    graph = build_graph(
        Path(arguments.dump),
        Path(arguments.symbols),
        arguments.source_swf,
    )
    output = Path(arguments.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(graph, indent=2, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    main()
