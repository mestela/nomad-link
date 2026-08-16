"""End-to-end: composited paint lands in the color attribute.

Runs inside Blender: blender --background --factory-startup --python tests/test_colors.py
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

FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "colors.nom")
fails = []
def check(label, ok, detail=""):
    print(f"  {'ok  ' if ok else 'FAIL'}  {label} {detail}")
    if not ok: fails.append(label)

verts = numpy.array([[0,0,0],[1,0,0],[1,1,0],[0,1,0]], "<f4")
faces = numpy.array([[0,1,2,3]], "<i4")
# base solid red, layer solid blue at multiply, full alpha, factor 0.5
base = numpy.tile(numpy.array([[255,0,0,255]], "u1"), (4,1))
lcol = numpy.tile(numpy.array([[0,0,255,255]], "u1"), (4,1))
lalpha = numpy.full((4,1), 255, "u1")
binary = verts.tobytes()+faces.tobytes()+base.tobytes()+lcol.tobytes()+lalpha.tobytes()
o = [0, verts.nbytes, verts.nbytes+faces.nbytes]
o += [o[2]+base.nbytes, o[2]+base.nbytes+lcol.nbytes]
mesh_entry = {"count_vertex":4, "count_face":1, "material":-1,
  "vertices":{"count":4,"type":"f32vec3","offset":o[0],"length":verts.nbytes},
  "faces":{"count":1,"type":"i32vec4","offset":o[1],"length":faces.nbytes},
  "colors":{"count":4,"type":"u8rgbm","offset":o[2],"length":base.nbytes},
  "layers":[{"name":"Blue","factor":0.5,"blend_color":"multiply",
    "colors":{"count":4,"type":"u8rgbm","offset":o[3],"length":lcol.nbytes},
    "opacity_color":{"count":4,"type":"u8","offset":o[4],"length":lalpha.nbytes}}]}
payload = json.dumps({"scene":[{"name":"Q","mesh":0}],"meshes":[mesh_entry],"materials":[]}).encode()
head = struct.pack("<12sI", nom_file.MAGIC, 6)
hdr = struct.pack("<5Q", 72+len(payload)+len(binary), 72, len(payload), 72+len(payload), len(binary))
open(FIX,"wb").write(head+hdr+struct.pack("<2Q",0,0)+payload+binary)

bpy.ops.wm.read_factory_settings(use_empty=True)
check("import", bpy.ops.nomad.import_nom(filepath=FIX) == {"FINISHED"})
obj = bpy.data.objects["Q"]
got = numpy.zeros(16, numpy.float32)
obj.data.color_attributes[addon.COLOR_ATTRIBUTE].data.foreach_get("color", got)
got = got.reshape(4,4)[:, :3]
# hand-computed: multiply in srgb of pure red x pure blue = black; lerp(red, black, 1.0*0.5)
want = numpy.tile([0.5, 0.0, 0.0], (4, 1))
check("composited color in the attribute", numpy.allclose(got, want, atol=1e-6), got[0])

# toggle off: base paint only, layers ignored
bpy.ops.wm.read_factory_settings(use_empty=True)
check("import base-only", bpy.ops.nomad.import_nom(filepath=FIX, import_composited=False) == {"FINISHED"})
obj = bpy.data.objects["Q"]
got = numpy.zeros(16, numpy.float32)
obj.data.color_attributes[addon.COLOR_ATTRIBUTE].data.foreach_get("color", got)
check("base paint untouched", numpy.allclose(got.reshape(4, 4)[:, :3],
      numpy.tile([1.0, 0.0, 0.0], (4, 1)), atol=1e-6), got[:3])

os.remove(FIX)
print("ALL PASS" if not fails else "FAILURES: " + ", ".join(fails))
sys.exit(1 if fails else 0)
