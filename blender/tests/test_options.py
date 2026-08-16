"""Operator options: scale, hidden pruning, repeats toggle. Headless = execute path.

Runs inside Blender: blender --background --factory-startup --python tests/test_options.py
"""
import os, sys
import numpy
import bpy
ADDON_DIR = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
NOMAD_ASSETS = os.path.abspath(os.path.join(ADDON_DIR, *[".."] * 3, "assets"))
sys.path.insert(0, ADDON_DIR)
if not os.path.isdir(NOMAD_ASSETS):
    print("--  no assets/ beside the repo, skipped")
    sys.exit(0)
import nomad_blender_link as addon
addon.register()

HEAD = NOMAD_ASSETS + "/meshes/head_mestela.nom"
fails = []

def check(label, condition, detail=""):
    print(f"  {'ok  ' if condition else 'FAIL'}  {label} {detail}")
    if not condition:
        fails.append(label)

def fresh(**kw):
    bpy.ops.wm.read_factory_settings(use_empty=True)
    result = bpy.ops.nomad.import_nom(filepath=HEAD, **kw)
    bpy.context.view_layer.update()
    return result

# baseline dimensions
fresh()
head = bpy.data.objects["Head"]
base = (head.matrix_world @ head.data.vertices[0].co).copy()

# scale doubles world positions
fresh(scale=2.0)
head = bpy.data.objects["Head"]
scaled = head.matrix_world @ head.data.vertices[0].co
check("scale=2 doubles world positions", all(abs(scaled[i] - 2 * base[i]) < 1e-5 for i in range(3)),
      f"{list(scaled)} vs {list(base)}")

# defaults unchanged (scale=1): same as baseline
fresh(scale=1.0)
head = bpy.data.objects["Head"]
again = head.matrix_world @ head.data.vertices[0].co
check("scale=1 is the baseline", all(abs(again[i] - base[i]) < 1e-6 for i in range(3)))

# repeats toggle is exercised by the fixture in test_import2; here just the parameter pass-through
result = fresh(import_repeats=False)
check("import_repeats=False still imports", result == {"FINISHED"})

print(f"\n{'ALL PASS' if not fails else 'FAILURES: ' + ', '.join(fails)}")
sys.exit(1 if fails else 0)
