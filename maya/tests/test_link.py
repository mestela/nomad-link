# SPDX-License-Identifier: MIT
"""The client against a mock Nomad, with no Maya in sight.

    python3 tests/test_link.py
"""
import importlib
import os
import sys

import numpy

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "python"))

from mock_nomad import MockNomad, wait as pump_until  # noqa: E402

from nomad_link import convert  # noqa: E402

client_module = importlib.import_module("nomad_link.client")
PORT = 48396


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

check(wait(lambda: link.connected), "connects and pairs")
check(nomad.hello["client_name"] == "Maya", "introduces itself as Maya")
check("hierarchy" in nomad.hello["capabilities"], "asks for hierarchy")
check("selection_transfer" not in nomad.hello["capabilities"],
      "does not claim to answer requests: this build only imports")
check("camera" not in nomad.hello["capabilities"], "does not promise a working view")

nomad.send(*mesh("m1", "Head", parent_id=""))
check(wait(lambda: "m1" in link.meshes), "a mesh arrives")
nomad.send({"type": "group", "link_id": "g", "name": "croc",
            "world_matrix": list(convert.IDENTITY)})
check(wait(lambda: "g" in link.groups), "a group arrives")
nomad.send({"type": "object_state", "link_id": "m1", "visible": False})
check(wait(lambda: link.meshes["m1"]["visible"] is False), "visibility applies")
nomad.send({"type": "object_delete", "link_id": "m1"})
check(wait(lambda: "m1" not in link.meshes), "deletion applies")

link.disconnect()
os.remove(client_module._token_path())
print("\nall good")
