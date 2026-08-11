# SPDX-License-Identifier: MIT
"""Nomad Sculpt <-> Houdini link (see PROTOCOL.md in stephomi/nomad-link).

Everything the HDAs call lives here:

    import nomad_link
    nomad_link.connect()                 # discover and connect
    nomad_link.client().meshes           # decoded meshes, keyed by mesh_id
"""
from . import convert, transport  # noqa: F401  (importable without Houdini)
from .client import DEFAULT_PORT, PROTOCOL, client

try:
    from .nodes import (
        answer_request,
        connect_button,
        cook_import,
        cook_in,
        cook_out,
        disconnect_button,
        get_scene,
        get_selection,
        mesh_menu,
        refresh_inputs,
        send_button,
        send_geometry,
        status_text,
        store_mesh_id,
    )
except ImportError:  # no hou: the codecs and the client still work
    pass

__all__ = [
    "DEFAULT_PORT", "PROTOCOL", "client", "connect", "disconnect",
    "connect_button", "disconnect_button", "get_scene", "get_selection",
    "send_button", "send_geometry", "cook_in", "cook_out", "cook_import", "mesh_menu",
    "refresh_inputs", "status_text", "store_mesh_id", "answer_request",
    "watch", "report",
]


def connect(host="", port=DEFAULT_PORT):
    """Connect to Nomad; empty host discovers it (UDP broadcast + Bonjour)."""
    return client().connect(host, port)


def disconnect():
    client().disconnect()


def watch(enable=True):
    """Log every message Nomad sends, so `report()` can show what actually arrived."""
    client().verbose = enable
    return "logging every message" if enable else "logging errors only"


def report(lines=40):
    """Print what the link is doing. Paste this when something is not syncing."""
    link = client()
    print("status      : %s - %s" % (link.status, link.message))
    print("nomad       : %s at %s:%s" % (link.nomad_version, link.host, link.port))
    print("nomad can   : %s" % ", ".join(sorted(link.peer_capabilities)))
    config = {key: value for key, value in link.session_config.items() if key != "type"}
    print("session     : %s" % config)
    print("cached      : %d meshes, %d materials, %d lights, %d cameras, %d textures"
          % (len(link.meshes), len(link.materials), len(link.lights),
             len(link.cameras), len(link.textures)))
    for name, store in (("lights", link.lights), ("cameras", link.cameras)):
        for link_id, entry in store.items():
            print("  %-8s %-24s %s" % (name[:-1], entry.get("name", "?"), link_id))
    print("recent      :")
    for line in link.log[-lines:]:
        print("  " + line)
