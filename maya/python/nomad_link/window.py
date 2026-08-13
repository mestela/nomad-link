# SPDX-License-Identifier: MIT
"""A small window: connect, pull a scene, see what is happening.

Named window.py rather than ui.py because nomad_link.ui() is the function
that opens it, and a submodule sharing a name with a function in __init__
shadows it in both directions.
"""
import maya.cmds as cmds

# not `from . import client`: the package defines a client() function, which
# shadows the module of the same name
from .client import DEFAULT_PORT, client

WINDOW = "nomadLinkWindow"


def show():
    if cmds.window(WINDOW, exists=True):
        cmds.deleteUI(WINDOW)
    cmds.window(WINDOW, title="Nomad Link", widthHeight=(320, 210), sizeable=True)
    cmds.columnLayout(adjustableColumn=True, rowSpacing=6, columnOffset=("both", 8))
    cmds.text(label="")
    host = cmds.textFieldGrp("nomadLinkHost", label="Host", text="",
                             annotation="Leave empty to discover Nomad on the network",
                             columnWidth2=(50, 240))
    cmds.button(label="Connect", command=lambda *_: _connect(host))
    cmds.button(label="Disconnect", command=lambda *_: _do(lambda: client().disconnect()))
    cmds.separator(height=8, style="in")
    cmds.button(label="Get Scene", command=lambda *_: _get_scene())
    cmds.button(label="Clear", command=lambda *_: _clear())
    cmds.separator(height=8, style="in")
    cmds.text("nomadLinkStatus", label="Disconnected", align="left")
    cmds.text("nomadLinkStats", label="", align="left")
    cmds.showWindow(WINDOW)
    from . import scene
    scene.on_status(_refresh)  # the pump drives the label, so progress is live
    _refresh()
    return WINDOW


def _connect(field):
    host = cmds.textFieldGrp(field, query=True, text=True).strip()
    client().connect(host, DEFAULT_PORT)
    _refresh()


def _get_scene():
    link = client()
    link.clear_scene()
    link.request("request_scene")
    _refresh()


def _clear():
    from . import scene
    client().clear()
    scene.clear()
    _refresh()


def _do(action):
    action()
    _refresh()


def _refresh():
    """Called from the pump: one label edit, nothing heavier."""
    if not cmds.window(WINDOW, exists=True):
        return
    link = client()
    text = "%s - %s" % (link.status, link.message)
    if link.receiving:
        text = "Receiving: %d objects..." % link.object_count
    elif link.connected and link.object_count:
        text = "Connected (%d objects)" % link.object_count
    cmds.text("nomadLinkStatus", edit=True, label=text)

    stats = link.stats
    quiet = link.quiet_for() if stats["messages"] else 0.0
    cmds.text("nomadLinkStats", edit=True, label=(
        "%d meshes, %.1f MB%s" % (
            len(link.meshes), stats["bytes"] / 1048576.0,
            ", nothing for %ds" % quiet if quiet > 5 else "")))
