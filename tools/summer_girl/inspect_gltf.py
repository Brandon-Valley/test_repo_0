import glob
import json
import os
import struct


def load_glb(path):
    with open(path, "rb") as handle:
        magic, version, total_length = struct.unpack("<4sII", handle.read(12))
        if magic != b"glTF":
            raise RuntimeError("Invalid GLB magic")
        json_length, json_type = struct.unpack("<II", handle.read(8))
        if json_type != 0x4E4F534A:
            raise RuntimeError("First GLB chunk is not JSON")
        return json.loads(handle.read(json_length).decode("utf-8").rstrip(" \t\r\n\x00"))


candidates = sorted(glob.glob("model/**/*.gltf", recursive=True))
if candidates:
    source = candidates[0]
    with open(source, "r", encoding="utf-8") as handle:
        data = json.load(handle)
else:
    candidates = sorted(glob.glob("model/**/*.glb", recursive=True))
    if not candidates:
        raise RuntimeError("No glTF or GLB found")
    source = candidates[0]
    data = load_glb(source)

nodes = data.get("nodes", [])
meshes = data.get("meshes", [])
skins = data.get("skins", [])
animations = data.get("animations", [])
accessors = data.get("accessors", [])

lines = []
lines.append("SOURCE=" + source)
lines.append("ASSET=" + json.dumps(data.get("asset", {}), sort_keys=True))
lines.append("COUNTS=" + json.dumps({
    "nodes": len(nodes),
    "meshes": len(meshes),
    "skins": len(skins),
    "animations": len(animations),
    "accessors": len(accessors),
}, sort_keys=True))

lines.append("NODES")
for index, node in enumerate(nodes):
    entry = {
        "index": index,
        "name": node.get("name"),
        "children": node.get("children", []),
        "mesh": node.get("mesh"),
        "skin": node.get("skin"),
        "translation": node.get("translation"),
        "rotation": node.get("rotation"),
        "scale": node.get("scale"),
        "matrix": node.get("matrix"),
    }
    lines.append(json.dumps(entry, sort_keys=True))

lines.append("SKINS")
for index, skin in enumerate(skins):
    joint_entries = []
    for joint_index in skin.get("joints", []):
        joint_name = nodes[joint_index].get("name") if 0 <= joint_index < len(nodes) else None
        joint_entries.append({"index": joint_index, "name": joint_name})
    lines.append(json.dumps({
        "index": index,
        "name": skin.get("name"),
        "skeleton": skin.get("skeleton"),
        "joints": joint_entries,
        "inverseBindMatrices": skin.get("inverseBindMatrices"),
    }, sort_keys=True))

lines.append("ANIMATIONS")
for animation_index, animation in enumerate(animations):
    lines.append(json.dumps({
        "index": animation_index,
        "name": animation.get("name"),
        "channels": len(animation.get("channels", [])),
        "samplers": len(animation.get("samplers", [])),
    }, sort_keys=True))
    for channel_index, channel in enumerate(animation.get("channels", [])):
        target = channel.get("target", {})
        node_index = target.get("node")
        node_name = nodes[node_index].get("name") if isinstance(node_index, int) and 0 <= node_index < len(nodes) else None
        sampler_index = channel.get("sampler")
        sampler = animation.get("samplers", [])[sampler_index] if isinstance(sampler_index, int) and 0 <= sampler_index < len(animation.get("samplers", [])) else {}
        input_accessor = sampler.get("input")
        output_accessor = sampler.get("output")
        input_info = accessors[input_accessor] if isinstance(input_accessor, int) and 0 <= input_accessor < len(accessors) else {}
        output_info = accessors[output_accessor] if isinstance(output_accessor, int) and 0 <= output_accessor < len(accessors) else {}
        lines.append(json.dumps({
            "animation": animation_index,
            "channel": channel_index,
            "node": node_index,
            "node_name": node_name,
            "path": target.get("path"),
            "interpolation": sampler.get("interpolation", "LINEAR"),
            "input_accessor": input_accessor,
            "input_min": input_info.get("min"),
            "input_max": input_info.get("max"),
            "input_count": input_info.get("count"),
            "output_accessor": output_accessor,
            "output_count": output_info.get("count"),
            "output_type": output_info.get("type"),
        }, sort_keys=True))

lines.append("FILES")
for path in sorted(glob.glob("model/**/*", recursive=True)):
    if os.path.isfile(path):
        lines.append(f"{os.path.getsize(path)}\t{path}")

report = "\n".join(lines) + "\n"
print(report)
with open("gltf_report.txt", "w", encoding="utf-8") as handle:
    handle.write(report)
