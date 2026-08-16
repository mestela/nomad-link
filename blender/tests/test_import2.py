"""Blender-side checks: instances, skew separation, repeats hint, discard bit, u8 masks.

Runs inside Blender: blender --background --factory-startup --python tests/test_import2.py
"""
import json, os, struct, sys
import numpy
import bpy
from mathutils import Matrix, Vector

ADDON_DIR = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
NOMAD_ASSETS = os.path.abspath(os.path.join(ADDON_DIR, *[".."] * 3, "assets"))
ADDON = ADDON_DIR
sys.path.insert(0, ADDON)
sys.path.insert(0, os.path.join(ADDON, "nomad_blender_link"))
import nomad_blender_link as addon
import nom_file
addon.register()

FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixture.nom")
fails = []


def check(label, condition, detail=""):
    print(f"  {'ok  ' if condition else 'FAIL'}  {label} {detail}")
    if not condition:
        fails.append(label)


def write_nom(js, binary):
    payload = json.dumps(js).encode()
    head = struct.pack("<12sI", nom_file.MAGIC, 6)
    offsets = struct.pack("<5Q", 72 + len(payload) + len(binary), 72, len(payload),
                          72 + len(payload), len(binary))
    with open(FIX, "wb") as handle:
        handle.write(head + offsets + struct.pack("<2Q", 0, 0) + payload + binary)


# one quad + one tri; face 1 carries a group and the DISCARD bit; masks stored legacy u8
positions = numpy.array([[0, 0, 0], [1, 0, 0], [1, 1, 0], [0, 1, 0], [2, 0, 0]], "<f4")
faces = numpy.array([[0, 1, 2, 3], [1, 4, 2, -1]], "<i4")
meta = numpy.array([2, 3 | 0x8000], "<u2")
masks = numpy.array([255, 128, 0, 255, 255], "u1")
binary = positions.tobytes() + faces.tobytes() + meta.tobytes() + masks.tobytes()
off_faces = positions.nbytes
off_meta = off_faces + faces.nbytes
off_masks = off_meta + meta.nbytes

mesh_entry = {
    "count_vertex": 5, "count_face": 2, "material": -1,
    "vertices": {"count": 5, "type": "f32vec3", "offset": 0, "length": positions.nbytes},
    "faces": {"count": 2, "type": "i32vec4", "offset": off_faces, "length": faces.nbytes},
    "faces_group": {"count": 2, "type": "u16", "offset": off_meta, "length": meta.nbytes},
    "masks": {"count": 5, "type": "u8", "offset": off_masks, "length": masks.nbytes},
}
identity = [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1]
# column-major shear: column 1 = (1,1,0,0) leans Y onto X; plus a translation
skewed = [1, 0, 0, 0, 1, 1, 0, 0, 0, 0, 1, 0, 3, 0, 0, 1]
repeat = [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 5, 0, 0, 1]

moved = [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 7, 0, 1]
scene = [
    {"name": "Group", "matrix": moved,
     "children": [{"name": "Skewed", "mesh": 0, "matrix": skewed, "repeats": [repeat]}]},
    {"name": "Twin", "mesh": 0, "matrix": identity,
     "children": [{"name": "Child", "mesh": 0, "matrix": identity}]},
]
write_nom({"scene": scene, "meshes": [mesh_entry], "materials": []}, binary)

bpy.ops.wm.read_factory_settings(use_empty=True)
result = bpy.ops.nomad.import_nom(filepath=FIX)
check("import finished", result == {"FINISHED"})
bpy.context.view_layer.update()

objs = {}
for obj in bpy.data.objects:
    objs.setdefault(obj.name.split(".")[0], []).append(obj)

# --- instances: one datablock, four users (Skewed, its repeat, Twin, Child)
meshes = {obj.data for obj in bpy.data.objects if obj.type == "MESH"}
mesh_objects = [obj for obj in bpy.data.objects if obj.type == "MESH"]
check("4 mesh objects", len(mesh_objects) == 4, len(mesh_objects))
check("one shared datablock", len(meshes) == 1, len(meshes))

# --- skew: unskew empty parent, exact world matrix preserved
empties = objs.get("nomad_unskew", [])
check("one unskew empty", len(empties) == 1, [o.name for o in bpy.data.objects])
skew_obj = objs["Skewed"][0]
check("skewed mesh under the empty", skew_obj.parent in empties)
check("unskew empty under the group", empties[0].parent is objs["Group"][0])
target = addon.TO_BLENDER @ addon.matrix_from_columns(moved) @ addon.matrix_from_columns(skewed) @ addon.TO_NOMAD
close = all(abs(skew_obj.matrix_world[i][j] - target[i][j]) < 1e-5 for i in range(4) for j in range(4))
check("skewed world exact (skew survives)", close,
      f"\n{skew_obj.matrix_world}\nvs\n{target}")
clean = Matrix.LocRotScale(*target.decompose())
check("the empty is skew-free", all(abs(empties[0].matrix_world[i][j] - clean[i][j]) < 1e-5
                                    for i in range(4) for j in range(4)))
check("unskewed objects got no empty", objs["Twin"][0].parent is None)

# --- repeats: a second object at the hinted transform, sharing the datablock
skew_group = [o for o in objs["Skewed"] if o.type == "MESH"]
check("repeat instantiated", len(skew_group) == 2, len(skew_group))
if len(skew_group) == 2:
    base = next(o for o in skew_group if o.name == "Skewed")
    copy = next(o for o in skew_group if o is not base)
    check("repeat shares the datablock", copy.data is base.data)
    want = addon.TO_BLENDER @ addon.matrix_from_columns(repeat) @ addon.TO_NOMAD
    check("repeat at the hinted transform",
          all(abs(copy.matrix_world[i][j] - want[i][j]) < 1e-5 for i in range(4) for j in range(4)),
          f"\n{copy.matrix_world}")

# --- discard bit: group ids stripped, face 1 hidden
data = objs["Twin"][0].data
groups = numpy.zeros(2, numpy.int32)
data.attributes[addon.FACE_GROUP_ATTRIBUTE].data.foreach_get("value", groups)
check("groups masked to 15 bits", list(groups) == [2, 3], list(groups))
hidden = numpy.zeros(2, bool)
data.attributes[addon.HIDE_FACE_ATTRIBUTE].data.foreach_get("value", hidden)
check("discard bit became hidden face", list(hidden) == [False, True], list(hidden))

# --- u8 masks: widened then inverted (Nomad 1=unmasked)
mask = numpy.zeros(5, numpy.float32)
data.attributes[addon.MASK_ATTRIBUTE].data.foreach_get("value", mask)
check("u8 masks widen and invert", numpy.allclose(mask, [0, 1 - 128 / 255, 1, 0, 0], atol=1e-3),
      list(numpy.round(mask, 3)))

# --- parenting under an instanced node still exact
child = objs["Child"][0]
check("child parented to Twin", child.parent is objs["Twin"][0])

# --- real corpus still passes end to end
if not os.path.isdir(NOMAD_ASSETS):
    os.remove(FIX)
    print("--  corpus regression skipped (no assets/)")
    print("ALL PASS" if not fails else "FAILURES: " + ", ".join(fails))
    sys.exit(1 if fails else 0)
bpy.ops.wm.read_factory_settings(use_empty=True)
result = bpy.ops.nomad.import_nom(filepath=NOMAD_ASSETS + "/meshes/head_mestela.nom")
check("corpus regression: head imports", result == {"FINISHED"} and
      len([o for o in bpy.data.objects if o.type == "MESH"]) == 2)
check("corpus regression: no unskew empties on clean files",
      not [o for o in bpy.data.objects if o.name.startswith("nomad_unskew")])

os.remove(FIX)
print(f"\n{'ALL PASS' if not fails else 'FAILURES: ' + ', '.join(fails)}")
sys.exit(1 if fails else 0)
