"""Sculpt layers as shape keys: back-out from composed positions, factors, mute, active.

Runs inside Blender: blender --background --factory-startup --python tests/test_layers.py
"""
import json, os, struct, sys
import numpy
import bpy

ADDON_DIR = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
NOMAD_ASSETS = os.path.abspath(os.path.join(ADDON_DIR, *[".."] * 3, "assets"))
sys.path.insert(0, ADDON_DIR)
sys.path.insert(0, os.path.join(ADDON_DIR, "nomad_blender_link"))
import nomad_blender_link as addon
import nom_file
addon.register()

FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "layers.nom")
fails = []


def check(label, condition, detail=""):
    print(f"  {'ok  ' if condition else 'FAIL'}  {label} {detail}")
    if not condition:
        fails.append(label)


# base quad; layer A lifts +1 nomad-Y at factor 0.5, layer B lifts +2 nomad-X but hidden,
# lattice layer must be skipped, paint layer has no offsets
base = numpy.array([[0, 0, 0], [1, 0, 0], [1, 1, 0], [0, 1, 0]], "<f4")
offsets_a = numpy.tile(numpy.array([[0, 1, 0]], "<f4"), (4, 1))
offsets_b = numpy.tile(numpy.array([[2, 0, 0]], "<f4"), (4, 1))
offsets_l = numpy.tile(numpy.array([[9, 9, 9]], "<f4"), (4, 1))
factor_a, factor_b = 0.5, 0.75
# the file stores COMPOSED positions: hidden B contributes nothing, lattice offsets do
# contribute in Nomad but ride their own layer -- keep it zero-contribution here for clarity
composed = base + factor_a * offsets_a
faces = numpy.array([[0, 1, 2, 3]], "<i4")

blobs = [composed.tobytes(), faces.tobytes(), offsets_a.tobytes(), offsets_b.tobytes(), offsets_l.tobytes()]
offs, binary = [], b""
for piece in blobs:
    offs.append(len(binary))
    binary += piece

layers = [
    {"name": "Lift", "factor": factor_a, "visible": True,
     "offsets": {"count": 4, "type": "f32vec3", "offset": offs[2], "length": offsets_a.nbytes}},
    {"name": "Hidden", "factor": factor_b, "visible": False,
     "offsets": {"count": 4, "type": "f32vec3", "offset": offs[3], "length": offsets_b.nbytes}},
    {"name": "Lattice", "factor": 1.0, "lattice_offset": True,
     "offsets": {"count": 4, "type": "f32vec3", "offset": offs[4], "length": offsets_l.nbytes}},
    {"name": "PaintOnly", "factor": 1.0},
]
mesh_entry = {"count_vertex": 4, "count_face": 1, "material": -1, "active_layer": 1,
              "vertices": {"count": 4, "type": "f32vec3", "offset": offs[0], "length": composed.nbytes},
              "faces": {"count": 1, "type": "i32vec4", "offset": offs[1], "length": faces.nbytes},
              "layers": layers}
scene = [{"name": "Layered", "mesh": 0}, {"name": "Twin", "mesh": 0}]
payload = json.dumps({"scene": scene, "meshes": [mesh_entry], "materials": []}).encode()
head = struct.pack("<12sI", nom_file.MAGIC, 6)
header = struct.pack("<5Q", 72 + len(payload) + len(binary), 72, len(payload), 72 + len(payload), len(binary))
open(FIX, "wb").write(head + header + struct.pack("<2Q", 0, 0) + payload + binary)

bpy.ops.wm.read_factory_settings(use_empty=True)
result = bpy.ops.nomad.import_nom(filepath=FIX)
check("import finished", result == {"FINISHED"})
bpy.context.view_layer.update()

obj = bpy.data.objects["Layered"]
keys = obj.data.shape_keys.key_blocks if obj.data.shape_keys else []
check("Basis + 3 user keys (lattice skipped)", len(keys) == 4,
      [k.name for k in keys])
if len(keys) == 4:
    to_b = addon.to_blender_vectors
    basis = numpy.array([list(d.co) for d in keys["Basis"].data])
    check("Basis is the backed-out base", numpy.allclose(basis, to_b(base), atol=1e-5))
    lift = keys["Lift"]
    check("Lift key = base + offsets", numpy.allclose(
        numpy.array([list(d.co) for d in lift.data]), to_b(base + offsets_a), atol=1e-5))
    check("Lift value = factor", abs(lift.value - factor_a) < 1e-6, lift.value)
    check("Lift not muted", not lift.mute)
    hidden = keys["Hidden"]
    check("Hidden muted at its factor", hidden.mute and abs(hidden.value - factor_b) < 1e-6)
    paint = keys["PaintOnly"]
    check("paint-only key equals base", numpy.allclose(
        numpy.array([list(d.co) for d in paint.data]), to_b(base), atol=1e-5))
    check("active = Hidden (file index 1 -> key 2)", obj.active_shape_key_index == 2,
          obj.active_shape_key_index)

    # the evaluated mesh must reproduce the composed positions Nomad displays
    eval_obj = obj.evaluated_get(bpy.context.evaluated_depsgraph_get())
    evaluated = numpy.array([list(v.co) for v in eval_obj.data.vertices])
    check("evaluated == Nomad's composed mesh", numpy.allclose(evaluated, to_b(composed), atol=1e-5))

twin = bpy.data.objects["Twin"]
check("instance shares keys", twin.data.shape_keys is obj.data.shape_keys
      or twin.data.shape_keys == obj.data.shape_keys)
check("instance active key mirrored", twin.active_shape_key_index == obj.active_shape_key_index)

os.remove(FIX)
print(f"\n{'ALL PASS' if not fails else 'FAILURES: ' + ', '.join(fails)}")
sys.exit(1 if fails else 0)
