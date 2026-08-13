# SPDX-License-Identifier: MIT
"""Exercise the Maya scene building against a fake maya module.

    python3 tests/test_scene.py     (needs numpy)

This checks the array bookkeeping -- winding, uv flip, hierarchy, transforms,
live point updates -- not Maya's own behaviour. The real thing still needs a
smoke test in Maya.
"""
import os
import sys

import numpy

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "python"))

import fake_maya  # noqa: E402

SCENE = fake_maya.install()

from nomad_link import convert, scene  # noqa: E402
from nomad_link.client import client  # noqa: E402


def check(condition, message):
    if not condition:
        raise AssertionError(message)
    print("ok  " + message)


def mesh(mesh_id, name, **extra):
    points = numpy.array([[0, 0, 0], [1, 0, 0], [1, 1, 0], [0, 1, 0], [2, 0, 0]], "f4")
    texcoords = numpy.array([[0, 0], [1, 0], [1, 1], [0, 1], [0, 0], [1, 0], [1, 1]], "f4")
    header, binary = convert.encode_mesh(
        mesh_id=mesh_id, geometry_id=mesh_id + "-geo", name=name,
        positions=points, sizes=numpy.array([4, 3], "i4"),
        corners=numpy.array([0, 1, 2, 3, 1, 4, 2], "i4"), texcoords=texcoords,
        point_attribs={"color": numpy.tile([0.25, 0.5, 0.75], (5, 1)),
                       "alpha": numpy.linspace(0, 1, 5)},
        ngon=True)
    header.update(extra)
    return convert.decode_mesh(header, binary)


link = client()
link.meshes.clear()
del link.order[:]

# ---- one mesh
link._store(mesh("m1", "Head"))
scene.rebuild()
built = [name for name in SCENE["meshes"]]
check(len(built) == 1, "one mesh created: %s" % built)
data = SCENE["meshes"][built[0]]
check(len(data["points"]) == 5, "five points")
check(list(data["counts"]) == [4, 3], "a quad and a triangle, not triangulated")
check(list(data["connects"]) == [0, 1, 2, 3, 1, 4, 2],
      "winding is NOT flipped: Maya is counter-clockwise like glTF")
check(abs(data["points"][4].x - 2.0) < 1e-6, "positions transfer")
check(len(data["uvs"]) == 7, "per-corner uvs")
check(abs(data["uvs"][2][1] - 0.0) < 1e-6,
      "v flipped to Maya's bottom-left origin: %s" % (data["uvs"][2],))
check(len(data["colours"]) == 5 and abs(data["colours"][0][2] - 0.75) < 0.01,
      "vertex colours with alpha")
check(built[0].endswith("Head"), "the object is named after Nomad's: %s" % built[0])

# ---- a sculpt stroke patches the mesh instead of rebuilding it
SCENE["updates"][:] = []
moved = mesh("m1", "Head")
moved["positions"][0] = [9.0, 9.0, 9.0]
link.meshes["m1"] = moved
scene.rebuild(link.revision + 1)
check(len(SCENE["updates"]) == 1, "the existing mesh was patched, not recreated")
check(len(SCENE["deleted"]) == 0, "nothing was deleted to do it")
check(abs(SCENE["meshes"][built[0]]["points"][0].x - 9.0) < 1e-6, "the point moved")

# ---- hierarchy and transforms
link.meshes.clear()
del link.order[:]
SCENE["nodes"].clear()
SCENE["meshes"].clear()
scene._built.clear()
parent_world = list(convert.IDENTITY)
parent_world[13] = 10.0
child_local = list(convert.IDENTITY)
child_local[13] = 3.0
link._store(mesh("p", "Body", world_matrix=parent_world))
link._store(mesh("c", "Hand", parent_id="p", world_matrix=convert.multiply(parent_world, child_local),
                 local_matrix=child_local, world_matrix_parent=parent_world))
scene.rebuild(link.revision + 1)
check(SCENE["nodes"]["|Hand"]["parent"] == "|Body",
      "the child is parented under its Nomad parent: %s" % SCENE["nodes"]["|Hand"])
local = SCENE["transforms"]["|Hand"]
check(abs(local[13] - 3.0) < 1e-6,
      "the child carries the local transform, not the world: %.3f" % local[13])
world = SCENE["transforms"]["|Body"]
check(abs(world[13] - 10.0) < 1e-6, "the parent carries its world transform")

# a frame that is not the parent's world must be derived instead
skewed = list(convert.IDENTITY)
skewed[0], skewed[5], skewed[10] = 0.478, 0.311, 0.888
link._store(mesh("s", "Skewed", parent_id="p", world_matrix=skewed,
                 local_matrix=list(convert.IDENTITY), world_matrix_parent=skewed))
scene.rebuild(link.revision + 1)
derived = SCENE["transforms"]["|Skewed"]
check(abs(derived[0] - 0.478) < 1e-4,
      "the scale survives when world_matrix_parent is not the parent: %.3f" % derived[0])

# ---- visibility and removal
link.meshes["c"]["visible"] = False
scene.rebuild(link.revision + 1)
check(SCENE["attributes"]["|Hand.visibility"] is False, "hidden objects are hidden")
link.meshes.pop("c")
link.order.remove("c")
scene.rebuild(link.revision + 1)
check("|Hand" in SCENE["deleted"], "an object gone from Nomad is removed from Maya")

print("\nall good")

# ---- the entry points import cleanly (a name in __init__ shadows a submodule
# of the same name, which is how nomad_link.ui() broke first time out)
import nomad_link  # noqa: E402
import importlib  # noqa: E402

window = importlib.import_module("nomad_link.window")
check(hasattr(window, "show"), "the window module imports")
check(window.DEFAULT_PORT == 48312, "and reached the real client module, not the function")
check(callable(nomad_link.ui), "nomad_link.ui() survives the window module being imported")
for name in ("connect", "disconnect", "get_scene", "get_selection", "clear", "report", "ui"):
    check(callable(getattr(nomad_link, name)), "nomad_link.%s exists" % name)
