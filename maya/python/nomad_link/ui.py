# SPDX-License-Identifier: MIT
"""A small window: connect, pull a scene, see what is happening."""
import maya.cmds as cmds

from . import client as _client
from .client import client

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
    cmds.showWindow(WINDOW)
    _refresh()
    return WINDOW


def _connect(field):
    host = cmds.textFieldGrp(field, query=True, text=True).strip()
    client().connect(host, _client.DEFAULT_PORT)
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
    """Status is polled by the window rather than pushed, to keep the pump cheap."""
    if not cmds.window(WINDOW, exists=True):
        return
    link = client()
    text = "%s - %s" % (link.status, link.message)
    if link.receiving:
        text = "Receiving: %d objects..." % link.object_count
    elif link.connected and link.object_count:
        text = "%s (%d objects)" % (link.message, link.object_count)
    cmds.text("nomadLinkStatus", edit=True, label=text)
    cmds.scriptJob(runOnce=True, idleEvent=lambda: None)
