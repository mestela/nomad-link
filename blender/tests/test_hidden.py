"""Runs inside Blender: blender --background --factory-startup --python tests/test_hidden.py"""
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

FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hidden.nom")
positions = numpy.array([[0, 0, 0], [1, 0, 0], [1, 1, 0]], "<f4")
faces = numpy.array([[0, 1, 2, -1]], "<i4")
binary = positions.tobytes() + faces.tobytes()
mesh_entry = {"count_vertex": 3, "count_face": 1, "material": -1,
              "vertices": {"count": 3, "type": "f32vec3", "offset": 0, "length": positions.nbytes},
              "faces": {"count": 1, "type": "i32vec4", "offset": positions.nbytes, "length": faces.nbytes}}
scene = [{"name": "Shown", "mesh": 0},
         {"name": "Ghost", "mesh": 0, "visible": False,
          "children": [{"name": "GhostChild", "mesh": 0}]}]
payload = json.dumps({"scene": scene, "meshes": [mesh_entry], "materials": []}).encode()
head = struct.pack("<12sI", nom_file.MAGIC, 6)
offsets = struct.pack("<5Q", 72 + len(payload) + len(binary), 72, len(payload), 72 + len(payload), len(binary))
open(FIX, "wb").write(head + offsets + struct.pack("<2Q", 0, 0) + payload + binary)

bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.nomad.import_nom(filepath=FIX)
names = sorted(o.name for o in bpy.data.objects)
ok1 = names == ["Ghost", "GhostChild", "Shown"] and bpy.data.objects["Ghost"].hide_get()
print("  ok    hidden imported+hidden by default" if ok1 else f"  FAIL default {names}")

ok2 = bpy.data.objects["GhostChild"].hide_get()
print("  ok    visibility cascades: child of hidden group is hidden" if ok2 else "  FAIL cascade")
os.remove(FIX)
sys.exit(0 if ok1 and ok2 else 1)
