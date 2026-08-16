"""Runs inside Blender: blender --background --factory-startup --python tests/test_import.py"""
import os, sys, time
import bpy

ADDON_DIR = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
NOMAD_ASSETS = os.path.abspath(os.path.join(ADDON_DIR, *[".."] * 3, "assets"))
ADDON = ADDON_DIR
ASSETS = NOMAD_ASSETS
sys.path.insert(0, ADDON)

if not os.path.isdir(NOMAD_ASSETS):
    print("--  no assets/ beside the repo, skipped")
    sys.exit(0)
import nomad_blender_link as addon
addon.register()
print("lz4 backend:", addon.nom_file.lz4_backend()[0])

fails = []


def check(label, condition, detail=""):
    print(f"  {'ok  ' if condition else 'FAIL'}  {label} {detail}")
    if not condition:
        fails.append(label)


def fresh():
    bpy.ops.wm.read_factory_settings(use_empty=True)


# --- single file, hierarchy + transforms
fresh()
path = f"{ASSETS}/meshes/head_mestela.nom"
t0 = time.perf_counter()
result = bpy.ops.nomad.import_nom(filepath=path)
dt = time.perf_counter() - t0
print(f"\nhead_mestela.nom -> {result} in {dt*1000:.0f}ms")
objects = {o.name: o for o in bpy.data.objects}
check("3 objects", len(objects) == 3, sorted(objects))
head = objects.get("Head")
check("Head is a mesh", head is not None and head.type == "MESH")
check("Head vertex count", head and len(head.data.vertices) == 19050, f"{len(head.data.vertices)}")
check("Head face count", head and len(head.data.polygons) == 19052, f"{len(head.data.polygons)}")
tris = sum(1 for p in head.data.polygons if len(p.vertices) == 3)
check("8 triangles kept", tris == 8, f"{tris}")
check("uv layer", bool(head.data.uv_layers))
check("color attribute", addon.COLOR_ATTRIBUTE in head.data.color_attributes)
check("material", len(head.data.materials) == 1)
eye, mirror = objects.get("Eye"), objects.get("Mirror")
check("Mirror is an empty", mirror is not None and mirror.type == "EMPTY")
check("Eye parented to Mirror", eye is not None and eye.parent is mirror)

# Z-up: the head should be taller than it is deep, and sit near the origin
lo = [min(v.co[i] for v in head.data.vertices) for i in range(3)]
hi = [max(v.co[i] for v in head.data.vertices) for i in range(3)]
size = [hi[i] - lo[i] for i in range(3)]
check("Z is the tall axis", size[2] > size[0] and size[2] > size[1], [round(s, 2) for s in size])
world = [head.matrix_world @ v.co for v in head.data.vertices[:1]]
check("finite world transform", all(abs(c) < 1e6 for c in world[0]), [round(c, 3) for c in world[0]])

# --- eye placement: the child must not collapse onto the parent origin
eye_world = eye.matrix_world.translation
check("Eye is offset from origin", eye_world.length > 1e-4, [round(c, 3) for c in eye_world])

# --- multires file picks the saved level
fresh()
bpy.ops.nomad.import_nom(filepath=f"{ASSETS}/projects/Skulls/Realistic.nom")
skull = next((o for o in bpy.data.objects if o.type == "MESH"), None)
check("skull imported", skull is not None and len(skull.data.vertices) == 46584,
      skull and len(skull.data.vertices))

# --- multi-file drop through directory+files
fresh()
bpy.ops.nomad.import_nom(
    directory=f"{ASSETS}/projects/Heads/",
    files=[{"name": "Planar.nom"}, {"name": "Stylized.nom"}])
check("two files dropped", len([o for o in bpy.data.objects if o.type == "MESH"]) >= 2,
      len(bpy.data.objects))
check("selection is the import", all(o.select_get() for o in bpy.data.objects))

# --- every shipped asset round-trips
fresh()
paths = []
for root, _dirs, names in os.walk(ASSETS):
    paths += [os.path.join(root, n) for n in names if n.endswith(".nom")]
bad = []
for path in sorted(paths):
    bpy.ops.wm.read_factory_settings(use_empty=True)
    try:
        if bpy.ops.nomad.import_nom(filepath=path) != {"FINISHED"}:
            bad.append((os.path.basename(path), "not FINISHED"))
        elif not any(o.type == "MESH" for o in bpy.data.objects):
            bad.append((os.path.basename(path), "no mesh"))
        else:
            for obj in bpy.data.objects:
                if obj.type == "MESH":
                    obj.data.validate(verbose=False)
    except Exception as error:
        bad.append((os.path.basename(path), repr(error)[:80]))
check(f"all {len(paths)} shipped .nom files import", not bad, bad[:5])

# --- file handler + menu registration
check("file handler registered", hasattr(bpy.types, "NOMAD_FH_import_nom"))
check("import menu entry", addon.menu_import_nom in bpy.types.TOPBAR_MT_file_import._dyn_ui_initialize())

# --- unregister leaves nothing behind
addon.unregister()
check("menu removed", addon.menu_import_nom not in bpy.types.TOPBAR_MT_file_import._dyn_ui_initialize())

print(f"\n{'ALL PASS' if not fails else 'FAILURES: ' + ', '.join(fails)}")
sys.exit(1 if fails else 0)
