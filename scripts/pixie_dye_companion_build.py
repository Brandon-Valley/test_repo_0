#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import html
import json
import math
import re
import shutil
import time
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from pathlib import Path

from PIL import Image, ImageDraw

Image.MAX_IMAGE_PIXELS = None

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
IDENTITY_MATRIX = [1.0, 0.0, 0.0, 1.0, 0.0, 0.0]
FORCED_TWO_COLOR_ITEM_IDS = {7019}


def slug(value: object) -> str:
    result = re.sub(r"[^A-Za-z0-9._-]+", "_", str(value or "")).strip("_")
    return result or "unnamed"


def normalize_name(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False), encoding="utf-8")


def parse_colors(path: Path, sort_path: Path) -> tuple[list[dict], list[str]]:
    root = ET.parse(path).getroot()
    colors = []
    for element in root.findall(".//color"):
        rgb = [int(value) for value in (element.text or "").split()[:3]]
        if len(rgb) != 3:
            continue
        tags = [value.strip() for value in element.attrib.get("color_tags", "").split(",") if value.strip()]
        colors.append(
            {
                "color_id": int(element.attrib["color_id"]),
                "name": element.attrib.get("color_name", ""),
                "description": element.attrib.get("color_desc", ""),
                "tags": tags,
                "primary_tag": tags[0] if tags else "other",
                "rgb": rgb,
                "hex": "#%02X%02X%02X" % tuple(rgb),
            }
        )
    spectrum = [
        value.strip()
        for value in (ET.parse(sort_path).getroot().findtext("colorTagSpectrum") or "").split(",")
        if value.strip()
    ]
    rank = {tag: index for index, tag in enumerate(spectrum)}
    colors.sort(key=lambda row: (rank.get(row["primary_tag"], 999), row["color_id"]))
    for order, row in enumerate(colors, 1):
        row["game_sort_order"] = order
        row["dye_bottle_item_id_formula"] = f"TYPE_DYEBOTTLE_ID * IDS_PER_TYPE + {row['color_id']}"
    return colors, spectrum


def flags_for_attribute(raw_value: str | None, count: int = 2) -> list[bool]:
    # This exactly mirrors Item.isSlotDyeableFromXML.  A missing or empty
    # attribute becomes [""] and therefore defaults to true.  If fewer flags
    # are supplied than requested, the last flag is repeated.
    values = (raw_value or "").split(",")
    current = True
    output = []
    for index in range(count):
        if index < len(values):
            current = values[index] != "0"
        output.append(current)
    return output


def parse_catalog(path: Path, catalog_name: str) -> dict[int, dict]:
    root = ET.parse(path).getroot()
    copy = root.find("copy")
    items: dict[int, dict] = {}
    descriptions: dict[int, str] = {}
    if copy is None:
        return items
    for element in copy:
        match = re.fullmatch(r"item(\d+)", element.tag)
        if match:
            item_id = int(match.group(1))
            items[item_id] = {
                "item_id": item_id,
                "name": (element.text or "").strip(),
                "catalog": catalog_name,
                "attributes": dict(element.attrib),
                "dyeable_attribute": element.attrib.get("dyeable"),
                "xml_slot_editable": flags_for_attribute(element.attrib.get("dyeable")),
            }
        match = re.fullmatch(r"desc(\d+)", element.tag)
        if match:
            descriptions[int(match.group(1))] = (element.text or "").strip()
    for item_id, row in items.items():
        row["description"] = descriptions.get(item_id, "")
    return items


def parse_all_item_catalogs(source: Path) -> dict[int, dict]:
    catalogs: dict[int, dict] = {}
    for filename, name in [
        ("homeAssets.xml", "home"),
        ("gardenAssets.xml", "garden"),
        ("avatarAssets.xml", "avatar"),
        ("items.xml", "items"),
    ]:
        path = source / "xml" / filename
        if path.exists():
            catalogs.update(parse_catalog(path, name))
    return catalogs


def load_graphs(graph_root: Path) -> tuple[dict[str, dict], dict[tuple[str, int], str]]:
    graphs: dict[str, dict] = {}
    classes: dict[tuple[str, int], str] = {}
    for path in graph_root.rglob("*.json"):
        try:
            graph = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        source_swf = graph.get("source_swf")
        if not source_swf or "nodes" not in graph:
            continue
        graphs[source_swf] = graph
        for symbol in graph.get("exported_symbols", []):
            classes[(source_swf, int(symbol["tag_id"]))] = symbol.get("class_name", "")
    return graphs, classes


def image_dimensions(path: Path) -> tuple[int, int] | None:
    try:
        with Image.open(path) as image:
            return image.size
    except Exception:
        return None


def source_reference_index(image_records: list[dict]) -> tuple[dict[tuple[str, int], list[dict]], dict[tuple[str, str], list[dict]]]:
    by_tag: dict[tuple[str, int], list[dict]] = defaultdict(list)
    by_class: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for record in image_records:
        for reference in record.get("source_references", []):
            source_swf = reference.get("source_swf") or ""
            tag_id = reference.get("tag_id")
            class_name = reference.get("symbol_class") or record.get("symbol_class") or ""
            if source_swf and tag_id is not None:
                by_tag[(source_swf, int(tag_id))].append(record)
            if source_swf and class_name:
                by_class[(source_swf, class_name)].append(record)
    return by_tag, by_class


def choose_image(records: list[dict]) -> dict | None:
    if not records:
        return None
    return max(
        records,
        key=lambda row: (
            int(row.get("nontransparent_area") or 0),
            int(row.get("width") or 0) * int(row.get("height") or 0),
        ),
    )


def matrix_inverse_affine(forward: list[float]) -> tuple[float, float, float, float, float, float] | None:
    a, b, c, d, tx, ty = forward
    determinant = a * d - b * c
    if abs(determinant) < 1e-12:
        return None
    ia = d / determinant
    ic = -c / determinant
    ib = -b / determinant
    id_value = a / determinant
    itx = -(ia * tx + ic * ty)
    ity = -(ib * tx + id_value * ty)
    return ia, ic, itx, ib, id_value, ity


def render_target_mask(
    library: Path,
    root_record: dict,
    child_record: dict,
    root_bounds: list[float],
    child_bounds: list[float],
    matrix: list[float],
) -> Image.Image | None:
    root_path = library / root_record["canonical_path"]
    child_path = library / child_record["canonical_path"]
    if not root_path.exists() or not child_path.exists():
        return None
    try:
        with Image.open(root_path) as root_image:
            root_width, root_height = root_image.size
        with Image.open(child_path) as child_image:
            child_alpha = child_image.convert("RGBA").getchannel("A")
            child_width, child_height = child_alpha.size
    except Exception:
        return None

    root_xmin, root_xmax, root_ymin, root_ymax = root_bounds
    child_xmin, child_xmax, child_ymin, child_ymax = child_bounds
    root_units_width = root_xmax - root_xmin
    root_units_height = root_ymax - root_ymin
    child_units_width = child_xmax - child_xmin
    child_units_height = child_ymax - child_ymin
    if min(root_units_width, root_units_height, child_units_width, child_units_height) <= 0:
        return None

    a, b, c, d, tx, ty = matrix
    child_scale_x = child_units_width / child_width
    child_scale_y = child_units_height / child_height
    root_scale_x = root_width / root_units_width
    root_scale_y = root_height / root_units_height

    forward = [
        root_scale_x * a * child_scale_x,
        root_scale_y * b * child_scale_x,
        root_scale_x * c * child_scale_y,
        root_scale_y * d * child_scale_y,
        root_scale_x * (a * child_xmin + c * child_ymin + tx - root_xmin),
        root_scale_y * (b * child_xmin + d * child_ymin + ty - root_ymin),
    ]
    inverse = matrix_inverse_affine(forward)
    if inverse is None:
        return None
    return child_alpha.transform(
        (root_width, root_height),
        Image.Transform.AFFINE,
        inverse,
        resample=Image.Resampling.BILINEAR,
        fillcolor=0,
    )


def generate_masks(
    output: Path,
    library: Path,
    item_components: dict[str, dict],
    graphs: dict[str, dict],
    by_tag: dict[tuple[str, int], list[dict]],
) -> dict[str, dict]:
    mask_root = output / "10_dye_and_composition" / "assets" / "dye_slot_masks"
    mask_root.mkdir(parents=True, exist_ok=True)
    report = {"attempted": 0, "created": 0, "failed": 0, "items_with_exact_masks": 0}

    for item_id, mapping in item_components.items():
        root_record = mapping.get("root_image_record")
        source_swf = mapping.get("source_swf")
        root_tag_id = mapping.get("root_tag_id")
        graph = graphs.get(source_swf or "")
        if not root_record or not graph or root_tag_id is None:
            continue
        root_node = graph.get("nodes", {}).get(str(root_tag_id), {})
        root_bounds = root_node.get("bounds_twips")
        if not root_bounds:
            continue
        masks_for_item = {}
        for slot in (1, 2):
            targets = mapping.get("dye_targets", {}).get(str(slot), [])
            if not targets:
                continue
            report["attempted"] += 1
            merged = None
            target_details = []
            for target in targets:
                child_tag_id = int(target["tag_id"])
                child_record = choose_image(by_tag.get((source_swf, child_tag_id), []))
                child_node = graph.get("nodes", {}).get(str(child_tag_id), {})
                child_bounds = child_node.get("bounds_twips")
                if not child_record or not child_bounds:
                    target_details.append({"tag_id": child_tag_id, "status": "missing_render_or_bounds"})
                    continue
                rendered = render_target_mask(
                    library,
                    root_record,
                    child_record,
                    root_bounds,
                    child_bounds,
                    target["matrix_from_root"],
                )
                if rendered is None or rendered.getbbox() is None:
                    target_details.append({"tag_id": child_tag_id, "status": "render_failed"})
                    continue
                merged = rendered if merged is None else Image.max(merged, rendered)
                target_details.append(
                    {
                        "tag_id": child_tag_id,
                        "status": "rendered",
                        "component_image": child_record["canonical_path"],
                    }
                )
            if merged is None or merged.getbbox() is None:
                report["failed"] += 1
                mapping.setdefault("mask_failures", {})[str(slot)] = target_details
                continue
            filename = f"item_{item_id}__root_{root_tag_id}__slot_{slot}.png"
            destination = mask_root / filename
            merged.save(destination, optimize=True)
            relative = f"10_dye_and_composition/assets/dye_slot_masks/{filename}"
            masks_for_item[str(slot)] = relative
            mapping.setdefault("mask_details", {})[str(slot)] = target_details
            report["created"] += 1
        if masks_for_item:
            mapping["dye_slot_masks"] = masks_for_item
            mapping["preview_accuracy"] = "exact_named_display_object_masks"
            report["items_with_exact_masks"] += 1
        elif mapping.get("dye_slot_count", 0):
            mapping["preview_accuracy"] = "whole_image_color_multiply_fallback"
    return report


def multiply_tint(image: Image.Image, mask: Image.Image, rgb: list[int]) -> Image.Image:
    base = image.convert("RGBA")
    pixels = base.load()
    alpha = mask.convert("L").resize(base.size, Image.Resampling.BILINEAR).load()
    red, green, blue = rgb
    for y in range(base.height):
        for x in range(base.width):
            amount = alpha[x, y] / 255.0
            if amount <= 0:
                continue
            r, g, b, a = pixels[x, y]
            pixels[x, y] = (
                round(r * (1 - amount) + r * red / 255.0 * amount),
                round(g * (1 - amount) + g * green / 255.0 * amount),
                round(b * (1 - amount) + b * blue / 255.0 * amount),
                a,
            )
    return base


def draw_fallback_bottle(rgb: list[int], size: tuple[int, int] = (54, 72)) -> Image.Image:
    image = Image.new("RGBA", size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    width, height = size
    outline = (48, 39, 52, 255)
    glass = (238, 247, 245, 215)
    liquid = tuple(rgb) + (245,)
    draw.rounded_rectangle((18, 2, 36, 14), radius=3, fill=(92, 69, 50, 255), outline=outline, width=2)
    draw.polygon([(14, 13), (40, 13), (47, 27), (47, 62), (41, 69), (13, 69), (7, 62), (7, 27)], fill=glass, outline=outline)
    draw.polygon([(9, 35), (45, 35), (45, 61), (39, 67), (15, 67), (9, 61)], fill=liquid)
    draw.ellipse((14, 40, 22, 50), fill=(255, 255, 255, 100))
    return image


def generate_bottles(output: Path, colors: list[dict], exact_bottle_root: Path | None) -> dict:
    bottle_root = output / "10_dye_and_composition" / "assets" / "dye_bottles"
    bottle_root.mkdir(parents=True, exist_ok=True)
    exact_base = None
    exact_mask = None
    method = "generated_vector_fallback"
    if exact_bottle_root and exact_bottle_root.exists():
        pngs = [path for path in exact_bottle_root.rglob("*.png") if "DyeBottle" in path.as_posix()]
        if pngs:
            candidate = max(pngs, key=lambda path: path.stat().st_size)
            try:
                exact_base = Image.open(candidate).convert("RGBA")
                exact_mask = exact_base.getchannel("A")
                method = "exact_shared_DyeBottle_symbol_whole_symbol_multiplier"
                shutil.copy2(candidate, bottle_root.parent / "dye_bottle_base.png")
            except Exception:
                exact_base = None
    for color in colors:
        if exact_base is not None and exact_mask is not None:
            bottle = multiply_tint(exact_base.copy(), exact_mask, color["rgb"])
        else:
            bottle = draw_fallback_bottle(color["rgb"])
        bottle.save(bottle_root / f"{color['color_id']:03d}__{slug(color['name'])}.png", optimize=True)
        color["bottle_image"] = f"10_dye_and_composition/assets/dye_bottles/{color['color_id']:03d}__{slug(color['name'])}.png"
    return {"method": method, "count": len(colors)}


def browser_html(rows: list[dict], colors: list[dict], item_components: dict[str, dict]) -> str:
    rows_json = json.dumps(rows, separators=(",", ":"), ensure_ascii=False)
    colors_json = json.dumps(colors, separators=(",", ":"), ensure_ascii=False)
    item_json = json.dumps(item_components, separators=(",", ":"), ensure_ascii=False)
    return f'''<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Pixie Hollow Houses and Gardens Dye Lab</title>
<style>
:root{{--bg:#102216;--panel:#1c3a25;--header:#17331f;--line:#51775b;--text:#f5fff6;--muted:#b8d5be;--gold:#f4cf69}}
*{{box-sizing:border-box}}body{{margin:0;font-family:system-ui;background:var(--bg);color:var(--text)}}
header{{position:sticky;top:0;background:var(--header);padding:14px;z-index:5;box-shadow:0 3px 15px #0008}}
.controls{{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-top:9px}}input,select,button{{padding:10px;border-radius:8px;border:1px solid var(--line);background:#0d1d12;color:white}}button{{cursor:pointer}}#grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:10px;padding:12px}}article{{background:var(--panel);padding:10px;border-radius:10px;min-width:0;cursor:pointer;transition:.16s transform,.16s box-shadow}}article:hover{{transform:translateY(-2px);box-shadow:0 8px 22px #0008}}article img{{width:100%;height:180px;object-fit:contain;background:#e9eee9;border-radius:6px}}small{{display:block;overflow-wrap:anywhere;color:var(--muted)}}h3{{font-size:15px;margin:8px 0 4px}}.badge{{display:inline-block;padding:2px 7px;border-radius:99px;background:#0d1d12;margin:3px 4px 2px 0;font-size:12px}}dialog{{width:min(1120px,96vw);height:min(880px,94vh);border:1px solid var(--line);border-radius:14px;background:#102216;color:white;padding:0;box-shadow:0 20px 90px #000}}dialog::backdrop{{background:#000b}}.modalHead{{display:flex;gap:10px;align-items:center;padding:12px 15px;background:var(--header);position:sticky;top:0;z-index:2}}.modalHead h2{{margin:0;flex:1;font-size:18px}}.modalBody{{display:grid;grid-template-columns:minmax(320px,1.1fr) minmax(320px,.9fr);gap:16px;padding:16px;height:calc(100% - 60px);overflow:auto}}.preview{{background:#e9eee9;border-radius:12px;min-height:420px;display:grid;place-items:center;position:relative;overflow:hidden}}canvas{{max-width:100%;max-height:650px;image-rendering:auto}}.meta{{font-size:13px;color:var(--muted);overflow-wrap:anywhere;margin-top:8px}}.slots{{display:flex;gap:10px;flex-wrap:wrap;margin:10px 0}}.slot{{width:125px;min-height:76px;border:2px dashed var(--line);border-radius:11px;padding:8px;background:#0d1d12}}.slot.locked{{opacity:.55}}.slot.active{{border-color:var(--gold)}}.slot strong,.slot span{{display:block}}.bottles{{display:grid;grid-template-columns:repeat(auto-fill,minmax(52px,1fr));gap:5px;max-height:430px;overflow:auto;padding:6px;background:#0d1d12;border-radius:10px}}.bottle{{height:72px;padding:2px;border:1px solid transparent;background:transparent;position:relative}}.bottle:hover{{border-color:var(--gold)}}.bottle img{{width:100%;height:100%;object-fit:contain}}.warning{{background:#503b16;border:1px solid #b8882e;padding:9px;border-radius:8px;margin:8px 0}}@media(max-width:760px){{.modalBody{{grid-template-columns:1fr}}dialog{{width:100vw;height:100vh;max-width:none;max-height:none;border-radius:0}}}}
</style></head><body>
<header><strong>Pixie Hollow Houses and Gardens - Dye Lab</strong><div class="controls">
<input id="q" placeholder="Search ID, name, path"><select id="cat"><option value="">All categories</option></select>
<input id="chars" placeholder="Characters to filter, e.g. __"><button id="charMode" type="button" data-mode="with" hidden>Exclude all with</button>
<select id="dyeSlots"><option value="">All dye slot counts</option><option value="0">0 dye slots</option><option value="1">1 dye slot</option><option value="2">2 dye slots</option></select>
<span id="count"></span></div></header><div id="grid"></div>
<dialog id="viewer"><div class="modalHead"><h2 id="modalTitle"></h2><button id="closeModal">Close</button></div><div class="modalBody">
<div><div class="preview"><canvas id="previewCanvas"></canvas></div><div class="meta" id="modalMeta"></div><label>Preview size <input id="sizeSlider" type="range" min="60" max="150" value="100"><span id="sizeValue">100%</span></label></div>
<div><h3>Dye slots</h3><div id="previewWarning"></div><div class="slots" id="slots"></div><h3>Dye bottles in game order</h3><div class="bottles" id="bottles"></div></div>
</div></dialog>
<script>
const rows={rows_json},dyes={colors_json},itemComponents={item_json};
const grid=document.querySelector('#grid'),q=document.querySelector('#q'),cat=document.querySelector('#cat'),chars=document.querySelector('#chars'),charMode=document.querySelector('#charMode'),dyeSlots=document.querySelector('#dyeSlots'),count=document.querySelector('#count');
[...new Set(rows.map(x=>x.category))].sort().forEach(x=>cat.add(new Option(x.replaceAll('_',' '),x)));
function esc(v){{return String(v??'').replace(/[&<>"']/g,c=>({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[c]))}}
function render(){{const term=q.value.toLowerCase(),c=cat.value,needle=chars.value,mode=charMode.dataset.mode,slot=dyeSlots.value;charMode.hidden=!needle;const filtered=rows.filter(x=>{{if(c&&x.category!==c)return false;if(term&&!JSON.stringify(x).toLowerCase().includes(term))return false;if(slot!==''&&Number(x.dye_slot_count||0)!==Number(slot))return false;if(needle){{const has=String(x.name||'').includes(needle);if(mode==='with'&&has)return false;if(mode==='without'&&!has)return false}}return true}});count.textContent=' '+filtered.length+' images';grid.innerHTML=filtered.slice(0,1000).map((x,i)=>`<article data-index="${{rows.indexOf(x)}}"><img loading="lazy" src="${{encodeURI(x.path)}}"><h3>${{esc(x.item_id??'')}} ${{esc(x.name)}}</h3><span class="badge">${{x.dye_slot_count||0}} dye slot${{x.dye_slot_count===1?'':'s'}}</span><span class="badge">size ${{x.minimum_percent??'N/A'}}-${{x.maximum_percent??'N/A'}}%</span><small>${{esc(x.category)}} | ${{x.width}}x${{x.height}} | ${{x.sources}} source refs</small><small>${{esc(x.path)}}</small></article>`).join('');grid.querySelectorAll('article').forEach(card=>card.onclick=()=>openViewer(rows[Number(card.dataset.index)]))}}
q.oninput=render;cat.onchange=render;chars.oninput=render;dyeSlots.onchange=render;charMode.onclick=()=>{{charMode.dataset.mode=charMode.dataset.mode==='with'?'without':'with';charMode.textContent=charMode.dataset.mode==='with'?'Exclude all with':'Exclude all without';render()}};
const viewer=document.querySelector('#viewer'),canvas=document.querySelector('#previewCanvas'),ctx=canvas.getContext('2d',{{willReadFrequently:true}}),slots=document.querySelector('#slots'),bottles=document.querySelector('#bottles'),warning=document.querySelector('#previewWarning'),sizeSlider=document.querySelector('#sizeSlider'),sizeValue=document.querySelector('#sizeValue');let current=null,baseImage=null,basePixels=null,maskImages={{}},selected={{}};
function loadImage(src){{return new Promise((resolve,reject)=>{{const im=new Image();im.onload=()=>resolve(im);im.onerror=reject;im.src=encodeURI(src)}})}}
async function openViewer(row){{current=row;selected={{}};maskImages={{}};document.querySelector('#modalTitle').textContent=`${{row.item_id??''}} ${{row.name}}`;document.querySelector('#modalMeta').innerHTML=`<b>Category:</b> ${{esc(row.category)}}<br><b>Image:</b> ${{esc(row.path)}}<br><b>Dimensions:</b> ${{row.width}} x ${{row.height}}<br><b>Size range:</b> ${{row.minimum_percent??'N/A'}}% to ${{row.maximum_percent??'N/A'}}%<br><b>Mapping:</b> ${{esc(row.mapping_role||'unresolved')}}`;sizeSlider.min=row.minimum_percent??60;sizeSlider.max=row.maximum_percent??150;sizeSlider.value=100;sizeSlider.disabled=row.minimum_percent==null;sizeValue.textContent='100%';viewer.showModal();baseImage=await loadImage(row.path);canvas.width=baseImage.naturalWidth;canvas.height=baseImage.naturalHeight;ctx.clearRect(0,0,canvas.width,canvas.height);ctx.drawImage(baseImage,0,0);basePixels=ctx.getImageData(0,0,canvas.width,canvas.height);const map=itemComponents[String(row.item_id)]||null;const slotCount=row.dye_slot_count||0;slots.innerHTML='';for(let s=1;s<=slotCount;s++){{const editable=row.dye_slots?.find(v=>v.slot_number===s)?.editable!==false;const div=document.createElement('div');div.className='slot'+(editable?'':' locked');div.dataset.slot=s;div.innerHTML=`<strong>Slot ${{s}}</strong><span>${{editable?'Drop dye here':'Locked / original color'}}</span>`;if(editable){{div.ondragover=e=>e.preventDefault();div.ondrop=e=>{{e.preventDefault();applyDye(s,Number(e.dataTransfer.getData('text/dye-id')))}};div.onclick=()=>div.classList.toggle('active')}}slots.append(div)}}warning.innerHTML=slotCount&&!row.has_exact_dye_masks?'<div class="warning">This item has valid game dye slots, but an exact extracted pixel mask was not recoverable. The browser uses a whole-object RGB-multiply fallback for preview only; the JSON mapping still identifies the real color1/color2 display objects.</div>':'';if(map?.dye_slot_masks){{for(const [slot,path] of Object.entries(map.dye_slot_masks)){{try{{maskImages[slot]=await loadImage(path)}}catch(e){{}}}}}}paint();}}
function applyDye(slot,id){{const dye=dyes.find(x=>x.color_id===id);if(!dye)return;selected[slot]=dye;const box=slots.querySelector(`[data-slot="${{slot}}"] span`);if(box)box.textContent=`${{dye.name}} (${{dye.hex}})`;paint()}}
function paint(){{if(!basePixels)return;ctx.putImageData(basePixels,0,0);let data=ctx.getImageData(0,0,canvas.width,canvas.height);const base=basePixels.data,out=data.data;for(const [slot,dye] of Object.entries(selected)){{let maskData=null;if(maskImages[slot]){{const temp=document.createElement('canvas');temp.width=canvas.width;temp.height=canvas.height;temp.getContext('2d').drawImage(maskImages[slot],0,0,canvas.width,canvas.height);maskData=temp.getContext('2d').getImageData(0,0,canvas.width,canvas.height).data}}for(let i=0;i<out.length;i+=4){{const amount=maskData?maskData[i+3]/255:(base[i+3]?1:0);if(!amount)continue;out[i]=Math.round(out[i]*(1-amount)+out[i]*dye.rgb[0]/255*amount);out[i+1]=Math.round(out[i+1]*(1-amount)+out[i+1]*dye.rgb[1]/255*amount);out[i+2]=Math.round(out[i+2]*(1-amount)+out[i+2]*dye.rgb[2]/255*amount)}}}}ctx.putImageData(data,0,0);const scale=Number(sizeSlider.value)/100;canvas.style.width=`${{Math.max(40,baseImage.naturalWidth*scale)}}px`;canvas.style.height='auto';sizeValue.textContent=sizeSlider.value+'%';}}
sizeSlider.oninput=paint;document.querySelector('#closeModal').onclick=()=>viewer.close();viewer.addEventListener('click',e=>{{if(e.target===viewer)viewer.close()}});
bottles.innerHTML=dyes.map(d=>`<button class="bottle" draggable="true" data-id="${{d.color_id}}" title="${{esc(d.name)}} | ${{d.hex}} | RGB ${{d.rgb.join(', ')}}"><img loading="lazy" src="${{encodeURI(d.bottle_image)}}" alt="${{esc(d.name)}}"></button>`).join('');bottles.querySelectorAll('.bottle').forEach(b=>{{b.ondragstart=e=>e.dataTransfer.setData('text/dye-id',b.dataset.id);b.onclick=()=>{{const active=slots.querySelector('.slot.active:not(.locked)')||slots.querySelector('.slot:not(.locked)');if(active)applyDye(Number(active.dataset.slot),Number(b.dataset.id))}}}});render();
</script></body></html>'''


def create_docs(output: Path, summary: dict) -> None:
    root = output / "10_dye_and_composition"
    readme = f'''# Pixie Hollow Dye and Composition Companion

This companion was generated from PixieHollowRE/web commit `{summary['web_commit']}` and is designed to sit inside the root of the existing Houses and Gardens library.

Copy the `10_dye_and_composition` folder and the updated `09_browse_library.html` into the root of the library.  The browser uses the existing relative image paths.

## Main files

- `config/item_component_map.json`: each matched real item and every known descendant Flash tag/image.
- `config/image_parent_map.json`: parent and child relationships for every indexed image reference.
- `config/swf_display_object_graph.jsonl`: complete per-SWF display-list graphs.
- `config/unmatched_resolution.json`: why each formerly unmatched image was classified that way.
- `config/item_dye_slots.json`: ordered `color1` and `color2` slots, editability, masks, and source evidence.
- `config/dyes.json`: every dye ID, name, description, tags, RGB, hex, game sort order, and bottle image.
- `assets/dye_bottles`: rendered bottle image for every dye.
- `assets/dye_slot_masks`: exact masks recovered from named Flash display objects.

See `DYE_SYSTEM.md`, `COMPOSITION_AND_DOUBLE_UNDERSCORES.md`, and `UNMATCHED_EXPLAINED.md` for details.
'''
    composition = '''# Composition and the double underscores

The double underscores in the image-library filenames are separators introduced by the extraction script.  They are not a Pixie Hollow file-format rule.

A matched file normally follows `ITEM_ID__Friendly_Item_Name__HASH.png`.  An unmatched file normally follows `FlashSymbolOrTag__HASH.png`.  Therefore a friendly matched item often appears not to contain `__` in the browser's displayed name, while a raw fallback name does.

The real composition structure comes from the SWF display list.  A complete item is normally an exported `DefineSprite`.  That sprite places child shapes, bitmaps, and sprites at specific depths and transformation matrices.  Children may themselves contain more children.  The complete mapping is stored in `config/swf_display_object_graph.jsonl` and connected to library images by source SWF and Flash character/tag ID.

Named child instances `color1` and `color2` are special: the game recursively searches for those exact names and applies dye ColorTransforms to them.
'''
    dye = '''# Pixie Hollow dye system

## Catalog

`colorAssets.xml` defines every color by numeric ID, display name, description, category tags, and an RGB triplet.  The generated `config/dyes.json` also supplies hex and game display order.

## Exact tint operation

The client converts RGB to a Flash ColorTransform with `redMultiplier = R / 255`, `greenMultiplier = G / 255`, and `blueMultiplier = B / 255`.  Alpha remains unchanged and the offsets remain zero.  It recursively traverses the display tree and applies the transform only to instances named `color1` or `color2`.

## Slot numbering

The dye UI has two channels maximum.  User-facing slots 1 and 2 correspond to XML flag indexes 0 and 1 and display-object names `color1` and `color2`.

The XML `dyeable` list controls whether each existing channel can be changed.  A missing attribute defaults to true.  `1,0` means the first slot is editable and the second is locked.  A single `0` locks all later slots because the code repeats the final supplied flag.  A nominally editable slot is disabled when the item has no valid color in that channel.  Item 7019 is a hard-coded two-color exception in the dye panel.

## Bottles

Dye bottles are virtual item descriptions created for all color IDs.  The bottle item's color ID is stored in its first color field.  The visual is one shared cached symbol named `DyeBottle`, not one separately drawn source image per dye.  The generated bottle directory contains one rendered convenience PNG per color.

## Game order

The game groups bottles using this tag spectrum: pink, red, orange, brown, yellow, green, blue, purple, gray, silver, white, black, other.  Within each tag group it sorts numerically by color ID.
'''
    unmatched = '''# What unmatched means

`unmatched` never meant that an image was unused, invalid, missing from the game, or not a real item.  It meant only that the first extractor could not confidently attach that particular exported PNG to a catalog item ID.

Common reasons include:

- the PNG is a nested shape or sprite inside a complete item;
- the SWF exposes only a numeric character/tag ID;
- the internal class name is abbreviated or differs from the player-facing catalog name;
- the file is a root-frame preview, animation frame, effect, scene layer, mask, or interface element;
- several images belong to one exported item;
- the source relationship is implicit in the SWF display list rather than SymbolClass metadata.

`config/unmatched_resolution.json` now distinguishes nested components, dye components, exported but catalog-unmatched roots, root previews, and unresolved internal tags.  Some complete objects can remain catalog-unmatched if the repository contains no authoritative catalog-to-class link; they are still represented in the display graph.
'''
    (root / "README.md").write_text(readme, encoding="utf-8")
    (root / "COMPOSITION_AND_DOUBLE_UNDERSCORES.md").write_text(composition, encoding="utf-8")
    (root / "DYE_SYSTEM.md").write_text(dye, encoding="utf-8")
    (root / "UNMATCHED_EXPLAINED.md").write_text(unmatched, encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True)
    parser.add_argument("--library", required=True)
    parser.add_argument("--graphs", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--web-commit", required=True)
    parser.add_argument("--server-commit", default="")
    parser.add_argument("--evidence", default="")
    parser.add_argument("--exact-bottle-assets", default="")
    arguments = parser.parse_args()

    source = Path(arguments.source).resolve()
    library = Path(arguments.library).resolve()
    graph_root = Path(arguments.graphs).resolve()
    output = Path(arguments.output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    companion = output / "10_dye_and_composition"
    config_root = companion / "config"
    config_root.mkdir(parents=True, exist_ok=True)

    metadata = library / "00_metadata"
    image_records = json.loads((metadata / "image_index.json").read_text(encoding="utf-8"))
    size_limits = json.loads((metadata / "item_size_limits.json").read_text(encoding="utf-8"))
    colors, spectrum = parse_colors(source / "xml/colorAssets.xml", source / "xml/sortData.xml")
    catalogs = parse_all_item_catalogs(source)
    graphs, graph_classes = load_graphs(graph_root)
    by_tag, by_class = source_reference_index(image_records)

    root_by_item: dict[int, list[dict]] = defaultdict(list)
    item_ids_by_source_tag: dict[tuple[str, int], set[int]] = defaultdict(set)
    for record in image_records:
        if record.get("item_id") is not None:
            root_by_item[int(record["item_id"])].append(record)
            for reference in record.get("source_references", []):
                if reference.get("source_swf") and reference.get("tag_id") is not None:
                    item_ids_by_source_tag[(reference["source_swf"], int(reference["tag_id"]))].add(int(record["item_id"]))

    item_components: dict[str, dict] = {}
    for item_id, records in sorted(root_by_item.items()):
        root_record = choose_image(records)
        if not root_record:
            continue
        chosen_reference = None
        chosen_symbol = None
        for reference in root_record.get("source_references", []):
            source_swf = reference.get("source_swf") or ""
            tag_id = reference.get("tag_id")
            if tag_id is None:
                continue
            graph = graphs.get(source_swf)
            if not graph:
                continue
            symbol = next((row for row in graph.get("exported_symbols", []) if int(row["tag_id"]) == int(tag_id)), None)
            if symbol:
                chosen_reference = reference
                chosen_symbol = symbol
                break
        catalog = catalogs.get(item_id, {})
        mapping = {
            "item_id": item_id,
            "name": catalog.get("name") or root_record.get("item_name") or "",
            "catalog": catalog.get("catalog") or "home_library",
            "dyeable_attribute": catalog.get("dyeable_attribute"),
            "xml_slot_editable": catalog.get("xml_slot_editable", flags_for_attribute(None)),
            "root_image": root_record["canonical_path"],
            "root_image_record": root_record,
            "source_swf": chosen_reference.get("source_swf") if chosen_reference else "",
            "root_tag_id": int(chosen_reference["tag_id"]) if chosen_reference and chosen_reference.get("tag_id") is not None else None,
            "symbol_class": chosen_reference.get("symbol_class") if chosen_reference else root_record.get("symbol_class", ""),
            "components": [],
            "dye_targets": {"1": [], "2": []},
        }
        if chosen_symbol:
            seen_components = set()
            for descendant in chosen_symbol.get("descendants", []):
                child_tag = int(descendant["tag_id"])
                component_images = [row["canonical_path"] for row in by_tag.get((mapping["source_swf"], child_tag), [])]
                key = (child_tag, descendant.get("parent_tag_id"), descendant.get("depth"), descendant.get("instance_name"), json.dumps(descendant.get("matrix_from_root")))
                if key in seen_components:
                    continue
                seen_components.add(key)
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
        artwork_slots = sorted(int(slot) for slot, values in mapping["dye_targets"].items() if values)
        if item_id in FORCED_TWO_COLOR_ITEM_IDS:
            artwork_slots = sorted(set(artwork_slots) | {1, 2})
        mapping["dye_slot_numbers"] = artwork_slots
        mapping["dye_slot_count"] = len(artwork_slots)
        mapping["dye_slots"] = [
            {
                "slot_number": slot,
                "xml_flag_index": slot - 1,
                "instance_name": f"color{slot}",
                "editable": True if item_id in FORCED_TWO_COLOR_ITEM_IDS else bool(mapping["xml_slot_editable"][slot - 1]),
                "hard_coded_force_two_color_exception": item_id in FORCED_TWO_COLOR_ITEM_IDS,
            }
            for slot in artwork_slots
        ]
        item_components[str(item_id)] = mapping

    image_parent_rows = []
    unmatched_rows = []
    for record in image_records:
        relationships = []
        exported_as = []
        component_of_items = set()
        dye_slots = set()
        for reference in record.get("source_references", []):
            source_swf = reference.get("source_swf") or ""
            tag_id = reference.get("tag_id")
            if not source_swf or tag_id is None:
                continue
            tag_id = int(tag_id)
            graph = graphs.get(source_swf)
            if not graph:
                continue
            class_name = graph_classes.get((source_swf, tag_id))
            if class_name:
                exported_as.append({"source_swf": source_swf, "tag_id": tag_id, "class_name": class_name})
            for parent in graph.get("child_to_parents", {}).get(str(tag_id), []):
                relation = {"source_swf": source_swf, "tag_id": tag_id, **parent}
                relationships.append(relation)
                if parent.get("instance_name") in {"color1", "color2"}:
                    dye_slots.add(int(parent["instance_name"][-1]))
                component_of_items.update(item_ids_by_source_tag.get((source_swf, int(parent["parent_tag_id"])), set()))
        role = "catalog_matched_export_or_render" if record.get("item_id") is not None else "unresolved_internal_tag"
        if record.get("item_id") is None:
            if dye_slots:
                role = "named_dye_component"
            elif component_of_items:
                role = "nested_component_of_catalog_item"
            elif exported_as:
                role = "complete_exported_symbol_without_catalog_match"
            elif "root_frame" in Path(record["canonical_path"]).stem:
                role = "root_timeline_preview"
            elif record.get("category") == "scene_layers":
                role = "scene_layer_or_scene_component"
        image_row = {
            "canonical_path": record["canonical_path"],
            "item_id": record.get("item_id"),
            "item_name": record.get("item_name", ""),
            "category": record.get("category"),
            "role": role,
            "exported_as": exported_as,
            "direct_parent_relationships": relationships,
            "component_of_item_ids": sorted(component_of_items),
            "dye_slot_numbers": sorted(dye_slots),
            "source_references": record.get("source_references", []),
        }
        image_parent_rows.append(image_row)
        if record.get("item_id") is None:
            unmatched_rows.append(image_row)

    mask_report = generate_masks(output, library, item_components, graphs, by_tag)
    bottle_report = generate_bottles(
        output,
        colors,
        Path(arguments.exact_bottle_assets).resolve() if arguments.exact_bottle_assets else None,
    )

    item_slots = {}
    for item_id, catalog in sorted(catalogs.items()):
        mapping = item_components.get(str(item_id), {})
        artwork_slots = mapping.get("dye_slot_numbers", [])
        if item_id in FORCED_TWO_COLOR_ITEM_IDS:
            artwork_slots = sorted(set(artwork_slots) | {1, 2})
        slot_rows = []
        for slot in artwork_slots:
            slot_rows.append(
                {
                    "slot_number": slot,
                    "xml_flag_index": slot - 1,
                    "instance_name": f"color{slot}",
                    "editable": True if item_id in FORCED_TWO_COLOR_ITEM_IDS else catalog["xml_slot_editable"][slot - 1],
                    "mask": mapping.get("dye_slot_masks", {}).get(str(slot)),
                }
            )
        item_slots[str(item_id)] = {
            "item_id": item_id,
            "name": catalog.get("name", ""),
            "catalog": catalog.get("catalog"),
            "dyeable_attribute": catalog.get("dyeable_attribute"),
            "missing_attribute_defaults_to_true": catalog.get("dyeable_attribute") is None,
            "artwork_slot_numbers": artwork_slots,
            "slot_count": len(artwork_slots),
            "slots": slot_rows,
            "force_two_color_exception": item_id in FORCED_TWO_COLOR_ITEM_IDS,
        }

    browser_rows = []
    parent_by_path = {row["canonical_path"]: row for row in image_parent_rows}
    for record in image_records:
        if not record.get("width"):
            continue
        item_id = record.get("item_id")
        mapping = item_components.get(str(item_id), {}) if item_id is not None else {}
        slots_for_item = mapping.get("dye_slots", [])
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
                "dye_slot_count": len(slots_for_item),
                "editable_dye_slot_count": sum(1 for slot in slots_for_item if slot.get("editable")),
                "dye_slots": slots_for_item,
                "has_exact_dye_masks": bool(mapping.get("dye_slot_masks")),
                "mapping_role": parent.get("role"),
                "component_of_item_ids": parent.get("component_of_item_ids", []),
            }
        )

    write_json(config_root / "dyes.json", colors)
    write_json(config_root / "color_sort_spectrum.json", spectrum)
    write_json(config_root / "item_dye_slots.json", item_slots)
    write_json(config_root / "item_component_map.json", item_components)
    write_json(config_root / "image_parent_map.json", image_parent_rows)
    write_json(config_root / "unmatched_resolution.json", unmatched_rows)
    with (config_root / "swf_display_object_graph.jsonl").open("w", encoding="utf-8") as handle:
        for source_swf, graph in sorted(graphs.items()):
            handle.write(json.dumps(graph, ensure_ascii=False, separators=(",", ":")) + "\n")

    with (config_root / "dyes.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, ["game_sort_order", "color_id", "name", "hex", "rgb", "primary_tag", "tags", "description", "bottle_image"])
        writer.writeheader()
        for color in colors:
            writer.writerow({**color, "rgb": ",".join(map(str, color["rgb"])), "tags": ",".join(color["tags"])})
    with (config_root / "item_dye_slots.csv").open("w", newline="", encoding="utf-8") as handle:
        fields = ["item_id", "name", "catalog", "dyeable_attribute", "slot_number", "instance_name", "editable", "mask", "force_two_color_exception"]
        writer = csv.DictWriter(handle, fields)
        writer.writeheader()
        for item in item_slots.values():
            if not item["slots"]:
                writer.writerow({"item_id": item["item_id"], "name": item["name"], "catalog": item["catalog"], "dyeable_attribute": item["dyeable_attribute"], "slot_number": 0, "instance_name": "", "editable": False, "mask": "", "force_two_color_exception": item["force_two_color_exception"]})
            for slot in item["slots"]:
                writer.writerow({"item_id": item["item_id"], "name": item["name"], "catalog": item["catalog"], "dyeable_attribute": item["dyeable_attribute"], **slot, "force_two_color_exception": item["force_two_color_exception"]})

    updated_browser = browser_html(browser_rows, colors, item_components)
    (output / "09_browse_library.html").write_text(updated_browser, encoding="utf-8")
    (companion / "09_browse_library_dye_lab.html").write_text(updated_browser, encoding="utf-8")

    evidence_target = companion / "source_evidence"
    if arguments.evidence and Path(arguments.evidence).exists():
        shutil.copytree(arguments.evidence, evidence_target, dirs_exist_ok=True)
    for source_file in [
        source / "xml/colorAssets.xml",
        source / "xml/sortData.xml",
        source / "xml/homeAssets.xml",
        source / "xml/gardenAssets.xml",
        source / "xml/avatarAssets.xml",
        source / "xml/items.xml",
        source / "xml/cacheableMedia.xml",
        source / "xml/panels/dyePanel.xml",
        source / "xml/tutorialAssets.xml",
    ]:
        if source_file.exists():
            destination = evidence_target / "xml" / source_file.name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_file, destination)

    summary = {
        "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "web_commit": arguments.web_commit,
        "game_server_commit": arguments.server_commit,
        "library_images": len(image_records),
        "swf_graphs": len(graphs),
        "catalog_items_all_types": len(catalogs),
        "mapped_library_items": len(item_components),
        "dyes": len(colors),
        "unmatched_images_classified": len(unmatched_rows),
        "mask_report": mask_report,
        "bottle_report": bottle_report,
        "double_underscore_is_extractor_separator": True,
        "maximum_dye_channels_in_client": 2,
    }
    write_json(companion / "BUILD_SUMMARY.json", summary)
    create_docs(output, summary)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
