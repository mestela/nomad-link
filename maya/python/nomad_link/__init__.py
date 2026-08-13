# SPDX-License-Identifier: MIT
"""Nomad Sculpt -> Maya, over Nomad Link (see PROTOCOL.md in stephomi/nomad-link).

From Maya's script editor:

    import nomad_link
    nomad_link.connect()          # empty = discover on the network
    nomad_link.get_scene()        # ask Nomad for everything
    nomad_link.report()           # what the link is doing

Import only for now: nothing is sent back to Nomad.
"""
from . import convert, transport  # noqa: F401  (importable without Maya)
from .client import DEFAULT_PORT, PROTOCOL, client

__all__ = ["DEFAULT_PORT", "PROTOCOL", "client", "connect", "disconnect",
           "get_scene", "get_selection", "clear", "report", "watch", "ui"]


def connect(host="", port=DEFAULT_PORT):
    """Connect to Nomad; empty host discovers it (UDP broadcast + Bonjour)."""
    return client().connect(host, port)


def disconnect():
    client().disconnect()


def get_scene():
    """Ask for the whole scene. Replaces what is cached, as a transfer should."""
    client().clear_scene()
    return client().request("request_scene")


def get_selection():
    return client().request("request_selection")


def clear():
    """Forget the cached scene and remove what was built."""
    client().clear()
    try:
        from . import scene
        scene.clear()
    except ImportError:
        pass
    return "cleared"


def watch(enable=True):
    """Log every message that arrives, for report() to show."""
    client().verbose = enable
    return "logging every message" if enable else "logging errors only"


def report(lines=30):
    """Print what the link is doing. Paste this when something is not syncing."""
    link = client()
    print("status      : %s - %s" % (link.status, link.message))
    print("nomad       : %s at %s:%s" % (link.nomad_version, link.host, link.port))
    peers = link.session_config.get("peers") or []
    # peers share Nomad's sender: a lingering one holds up everybody
    print("other peers : %s" % (", ".join(peers) if peers else "none"))
    print("cached      : %d meshes, %d lights, %d cameras, %d groups"
          % (len(link.meshes), len(link.lights), len(link.cameras), len(link.groups)))
    stats = link.stats
    span = (stats["last"] - stats["first"]) or 0.0
    print("traffic     : %d messages, %.1f MB in %.1fs"
          % (stats["messages"], stats["bytes"] / 1048576.0, span))
    print("pump        : %d calls, worst gap %.2fs" % (stats["pumps"], stats["worst_gap"]))
    for name in list(link.order)[:12]:
        mesh = link.meshes.get(name)
        if mesh is not None:
            print("  mesh     %-24s %6d pts  %s" % (
                mesh["name"][:24], len(mesh["positions"]),
                "visible" if mesh.get("visible", True) else "HIDDEN"))
    if len(link.order) > 12:
        print("  ... and %d more" % (len(link.order) - 12))
    for line in link.log[-lines:]:
        print("  " + line)


def ui():
    """Open the little control window."""
    # the module is window.py, not ui.py: a submodule and a function of the same
    # name fight -- the function shadows the module on import, and importing the
    # module then replaces the function
    from . import window
    return window.show()
