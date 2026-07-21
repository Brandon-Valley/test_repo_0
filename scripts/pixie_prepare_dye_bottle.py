#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

from PIL import Image

from pixie_swf_display_graph import build_graph


def one(paths: list[Path], description: str) -> Path:
    if not paths:
        raise FileNotFoundError(description)
    preferred = [path for path in paths if "pixiehollow" not in path.as_posix().lower()]
    return sorted(preferred or paths)[0]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence", required=True)
    parser.add_argument("--output", required=True)
    arguments = parser.parse_args()

    evidence = Path(arguments.evidence).resolve()
    output = Path(arguments.output).resolve()
    output.mkdir(parents=True, exist_ok=True)

    dump = one(
        list(evidence.rglob("swf/meadow/gather_common/swf_dump.txt")),
        "gather_common swf_dump.txt",
    )
    symbols = one(
        list(evidence.rglob("swf/meadow/gather_common/export/symbolClass/symbols.csv")),
        "gather_common symbols.csv",
    )
    graph = build_graph(dump, symbols, "swf/meadow/gather_common.swf")
    (output / "bottle_graph.json").write_text(
        json.dumps(graph, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    symbol = next(
        row
        for row in graph["exported_symbols"]
        if row.get("class_name") == "DyeBottle"
    )
    target = next(
        row
        for row in symbol.get("dye_targets", [])
        if row.get("instance_name") == "color1"
    )
    root_tag = int(symbol["tag_id"])
    child_tag = int(target["tag_id"])

    root_png = one(
        list(evidence.rglob(f"DefineSprite_{root_tag}_DyeBottle/1.png")),
        "complete DyeBottle sprite PNG",
    )
    child_png = one(
        list(evidence.rglob(f"DefineSprite_{child_tag}*/1.png")),
        "DyeBottle color1 child PNG",
    )
    root_node = graph["nodes"][str(root_tag)]
    child_node = graph["nodes"][str(child_tag)]
    root_bounds = root_node.get("bounds_twips")
    child_bounds = child_node.get("bounds_twips")
    if not root_bounds or not child_bounds:
        raise RuntimeError(
            f"Missing SWF bounds: root={root_bounds!r} child={child_bounds!r}"
        )

    with Image.open(root_png) as root_image:
        root_rgba = root_image.convert("RGBA")
        root_size = root_rgba.size
    with Image.open(child_png) as child_image:
        child_alpha = child_image.convert("RGBA").getchannel("A")

    a, b, c, d, tx, ty = target["matrix_from_root"]
    corners = [
        (a * x + c * y + tx, b * x + d * y + ty)
        for x in (child_bounds[0], child_bounds[1])
        for y in (child_bounds[2], child_bounds[3])
    ]
    root_width_twips = root_bounds[1] - root_bounds[0]
    root_height_twips = root_bounds[3] - root_bounds[2]
    if root_width_twips <= 0 or root_height_twips <= 0:
        raise RuntimeError(f"Invalid root bounds: {root_bounds!r}")

    left = round(
        (min(x for x, _ in corners) - root_bounds[0])
        * root_size[0]
        / root_width_twips
    )
    right = round(
        (max(x for x, _ in corners) - root_bounds[0])
        * root_size[0]
        / root_width_twips
    )
    top = round(
        (min(y for _, y in corners) - root_bounds[2])
        * root_size[1]
        / root_height_twips
    )
    bottom = round(
        (max(y for _, y in corners) - root_bounds[2])
        * root_size[1]
        / root_height_twips
    )
    if right <= left or bottom <= top:
        raise RuntimeError(
            f"Invalid transformed color1 rectangle: {(left, top, right, bottom)!r}"
        )

    resized_alpha = child_alpha.resize(
        (right - left, bottom - top), Image.Resampling.BILINEAR
    )
    mask = Image.new("L", root_size, 0)
    mask.paste(resized_alpha, (left, top))
    if mask.getbbox() is None:
        raise RuntimeError("Prepared DyeBottle color1 mask is empty")

    shutil.copy2(root_png, output / "dye_bottle_base.png")
    mask.save(output / "dye_bottle_color1_mask.png", optimize=True)
    mapping = {
        "source_swf": "swf/meadow/gather_common.swf",
        "root_symbol": "DyeBottle",
        "root_tag_id": root_tag,
        "color1_child_tag_id": child_tag,
        "matrix_from_root": target["matrix_from_root"],
        "root_bounds_twips": root_bounds,
        "child_bounds_twips": child_bounds,
        "root_image_size": root_size,
        "child_image_size": child_alpha.size,
        "mask_rectangle": [left, top, right, bottom],
        "mask_bbox": mask.getbbox(),
        "root_png": root_png.as_posix(),
        "child_png": child_png.as_posix(),
    }
    (output / "dye_bottle_mapping.json").write_text(
        json.dumps(mapping, indent=2), encoding="utf-8"
    )
    print(json.dumps(mapping, indent=2))


if __name__ == "__main__":
    main()
