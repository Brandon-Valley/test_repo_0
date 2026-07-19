import bpy
import glob
import json
import os

lines = []

def log(text):
    text = str(text)
    print(text)
    lines.append(text)

for obj in list(bpy.data.objects):
    bpy.data.objects.remove(obj, do_unlink=True)

candidates = []
for pattern in ("model/**/*.gltf", "model/**/*.glb", "model/**/*.fbx", "model/**/*.obj"):
    candidates.extend(glob.glob(pattern, recursive=True))

log("FILES")
for path in sorted(glob.glob("model/**/*", recursive=True)):
    if os.path.isfile(path):
        log(path)

if not candidates:
    raise RuntimeError("No supported model file found")

source = sorted(candidates, key=lambda p: (0 if p.lower().endswith((".gltf", ".glb")) else 1, len(p)))[0]
log("SOURCE=" + source)

ext = os.path.splitext(source)[1].lower()
if ext in (".gltf", ".glb"):
    bpy.ops.import_scene.gltf(filepath=os.path.abspath(source))
elif ext == ".fbx":
    bpy.ops.import_scene.fbx(filepath=os.path.abspath(source))
elif ext == ".obj":
    bpy.ops.wm.obj_import(filepath=os.path.abspath(source))

log("OBJECTS")
for obj in bpy.data.objects:
    log(json.dumps({
        "name": obj.name,
        "type": obj.type,
        "parent": obj.parent.name if obj.parent else None,
        "dimensions": [round(v, 6) for v in obj.dimensions],
        "location": [round(v, 6) for v in obj.location],
        "rotation_mode": obj.rotation_mode,
    }, sort_keys=True))

log("ARMATURES")
for obj in bpy.data.objects:
    if obj.type != "ARMATURE":
        continue
    log("ARMATURE=" + obj.name)
    for bone in obj.data.bones:
        log(json.dumps({
            "bone": bone.name,
            "parent": bone.parent.name if bone.parent else None,
            "head": [round(v, 6) for v in bone.head_local],
            "tail": [round(v, 6) for v in bone.tail_local],
            "matrix": [[round(value, 6) for value in row] for row in bone.matrix_local],
        }, sort_keys=True))

log("ACTIONS")
for action in bpy.data.actions:
    log(json.dumps({
        "name": action.name,
        "frame_range": [round(v, 4) for v in action.frame_range],
        "fcurves": len(action.fcurves),
        "groups": sorted([group.name for group in action.groups]),
    }, sort_keys=True))
    for fc in action.fcurves:
        log(json.dumps({
            "action": action.name,
            "data_path": fc.data_path,
            "array_index": fc.array_index,
            "keys": len(fc.keyframe_points),
            "first": [round(v, 4) for v in fc.keyframe_points[0].co] if fc.keyframe_points else None,
            "last": [round(v, 4) for v in fc.keyframe_points[-1].co] if fc.keyframe_points else None,
        }, sort_keys=True))

log("SCENE")
log("fps=" + str(bpy.context.scene.render.fps))
log("frame_start=" + str(bpy.context.scene.frame_start))
log("frame_end=" + str(bpy.context.scene.frame_end))

with open("report.txt", "w", encoding="utf-8") as handle:
    handle.write("\n".join(lines) + "\n")
