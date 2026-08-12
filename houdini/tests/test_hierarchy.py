# SPDX-License-Identifier: MIT
"""Protocol 0.11.37 hierarchy: parent_id, group, scene_batch, cascading delete.

    hython tests/test_hierarchy.py
"""
import importlib
import os
import sys

import numpy
from pxr import Usd, UsdGeom

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "python"))

from mock_nomad import MockNomad, wait as pump_until  # noqa: E402

from nomad_link import convert, usd  # noqa: E402

client_module = importlib.import_module("nomad_link.client")
PORT = 48397


def check(condition, message):
    if not condition:
        raise AssertionError(message)
    print("ok  " + message)


def wait(predicate, seconds=3.0):
    return pump_until(link, predicate, seconds)


def mesh(mesh_id, name, **extra):
    header, binary = convert.encode_mesh(
        mesh_id=mesh_id, geometry_id=mesh_id + "-geo", name=name,
        positions=numpy.zeros((3, 3), "f4"), sizes=numpy.array([3], "i4"),
        corners=numpy.array([0, 1, 2], "i4"), ngon=True)
    header.update(extra)
    return header, binary


nomad = MockNomad(PORT)
nomad.start()
client_module._token_path = lambda: os.path.join(HERE, ".test_tokens.json")
link = client_module.Client()
link.connect("127.0.0.1", PORT)
check(wait(lambda: link.connected), "connected")
for capability in ("hierarchy", "scene_batch", "skew"):
    check(capability in nomad.hello["capabilities"],
          "we advertise `%s`, without which Nomad withholds it" % capability)

# a group with two children, and a grandchild
nomad.send({"type": "group", "link_id": "g1", "name": "Character",
            "world_matrix": convert.IDENTITY, "parent_id": ""})
nomad.send(*mesh("m1", "Torso", parent_id="g1", child_index=1))
nomad.send(*mesh("m2", "Head", parent_id="g1", child_index=0))
nomad.send(*mesh("m3", "Hat", parent_id="m2"))
check(wait(lambda: len(link.meshes) == 3 and link.groups), "the group and its children arrive")

stage = Usd.Stage.CreateInMemory()
usd.author_scene(stage, link, material_style="preview")
paths = [p.GetPath().pathString for p in stage.Traverse()]
check("/nomad/Character" in paths, "the group is an Xform: %s" % paths)
check(stage.GetPrimAtPath("/nomad/Character").GetTypeName() == "Xform", "and it is transform-only")
check("/nomad/Character/Head/Hat" in paths, "the tree is rebuilt under it")
order = [p.GetName() for p in stage.GetPrimAtPath("/nomad/Character").GetChildren()]
check(order == ["Head", "Torso"], "child_index orders siblings: %s" % order)

# an unknown parent must not lose the object (section 9)
nomad.send(*mesh("m4", "Orphan", parent_id="not-here-yet"))
check(wait(lambda: "m4" in link.meshes), "a mesh with an unknown parent still arrives")
orphan_stage = Usd.Stage.CreateInMemory()
usd.author_scene(orphan_stage, link, material_style="preview")
check(bool(orphan_stage.GetPrimAtPath("/nomad/Orphan")), "and sits at the root meanwhile")
nomad.send({"type": "group", "link_id": "not-here-yet", "name": "Late",
            "world_matrix": convert.IDENTITY})
check(wait(lambda: "not-here-yet" in link.groups), "the parent turns up")
late_stage = Usd.Stage.CreateInMemory()
usd.author_scene(late_stage, link, material_style="preview")
check(bool(late_stage.GetPrimAtPath("/nomad/Late/Orphan")), "and the node re-parents under it")

# absent fields leave state alone, rather than resetting it
nomad.send({"type": "object_state", "link_id": "m1", "visible": False})
check(wait(lambda: link.meshes["m1"]["visible"] is False), "object_state hides a mesh")
nomad.send(*mesh("m1", "Torso", parent_id="g1"))  # no visible field this time
check(wait(lambda: link.meshes["m1"]["name"] == "Torso"), "a fresh mesh_full arrives")
check(link.meshes["m1"]["visible"] is False,
      "an absent visible leaves it hidden rather than resetting it")

# visible on mesh_full, which 0.11.37 adds
nomad.send(*mesh("m5", "Hidden", visible=False))
check(wait(lambda: "m5" in link.meshes), "a mesh_full with visible arrives")
check(link.meshes["m5"]["visible"] is False, "and mesh_full visibility is honoured")

# scene_batch applies in order as one step
nomad.send({"type": "scene_batch", "live_sync": True, "messages": [
    {"type": "object_state", "link_id": "m3", "parent_id": "g1"},
    {"type": "object_delete", "link_id": "m2"},
]})
check(wait(lambda: "m2" not in link.meshes), "the batch's delete applied")
check(link.meshes["m3"]["parent_id"] == "g1",
      "and the re-parent that preceded it survived, so the child was not orphaned")

# object_delete takes the children with it
nomad.send(*mesh("p1", "Parent"))
nomad.send(*mesh("c1", "Child", parent_id="p1"))
nomad.send(*mesh("c2", "Grandchild", parent_id="c1"))
check(wait(lambda: "c2" in link.meshes), "a three-deep branch arrives")
nomad.send({"type": "object_delete", "link_id": "p1"})
check(wait(lambda: "p1" not in link.meshes), "deleting the parent works")
check("c1" not in link.meshes and "c2" not in link.meshes,
      "and takes the whole branch with it (0.11.37)")

link.disconnect()
os.remove(client_module._token_path())
print("\nall good")
