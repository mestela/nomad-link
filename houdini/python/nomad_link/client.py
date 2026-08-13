# SPDX-License-Identifier: MIT
"""One long-lived Nomad Link connection per Houdini session.

The socket lives in transport.Connection's own thread; everything else runs on
Houdini's main thread from an event loop callback, so SOP cooks only ever touch
the decoded mesh cache -- never the network.
"""
import json
import os
import time
import uuid

from . import convert, transport

PROTOCOL = 1
DEFAULT_PORT = 48312
CLIENT_NAME = "Houdini"
PING_INTERVAL = 10.0

# honest hello: we receive geometry, object state and the scene objects that the
# LOP side authors on a stage, and we send mesh_full. We do not advertise
# "camera" -- that means sending our working view, which we do not do.
CAPABILITIES = [
    "selection_transfer",
    "scene_transfer",
    "scene_edits",
    "object_state",
    "session_config",
    "mesh_delta_receive",
    "mesh_instance",
    "hierarchy",      # parent_id / child_index / group (0.11.37)
    "scene_batch",
    "skew",           # USD holds a skewed matrix directly, so no synthetic groups
    "ngon",
    "material",
    "light",
    "camera_object",
    "texture",
    "display_config",
]

_client = None


def client():
    global _client
    if _client is None:
        _client = Client()
    return _client


def _token_path():
    try:
        import hou
        base = hou.homeHoudiniDirectory()
    except Exception:
        base = os.path.expanduser("~")
    return os.path.join(base, "nomad_link_tokens.json")


def texture_cache():
    """Where texture blobs land, so materials can point USD at real files."""
    try:
        import hou
        base = hou.text.expandString("$HOUDINI_TEMP_DIR")
    except Exception:
        import tempfile
        base = tempfile.gettempdir()
    path = os.path.join(base, "nomad_link_textures")
    if not os.path.isdir(path):
        try:
            os.makedirs(path)
        except OSError:
            pass
    return path


def _load_tokens():
    try:
        with open(_token_path()) as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return {}


def _save_token(host, token):
    tokens = _load_tokens()
    tokens[host] = token
    try:
        with open(_token_path(), "w") as handle:
            json.dump(tokens, handle, indent=1)
    except OSError:
        pass


class Client:
    def __init__(self):
        self.connection = transport.Connection(CLIENT_NAME, CAPABILITIES)
        self.host = ""
        self.port = DEFAULT_PORT
        self.message = "Disconnected"
        self.nomad_version = ""
        self.peer_capabilities = set()
        self.session_config = {}
        self.meshes = {}          # mesh_id -> decoded mesh dict (convert.decode_mesh)
        self.order = []           # arrival order, for the node menus
        self.materials = {}       # mesh_id -> material block (PROTOCOL.md section 10)
        self.lights = {}          # link_id -> light header
        self.cameras = {}         # link_id -> camera_object header
        self.groups = {}          # link_id -> transform-only node (0.11.37)
        self.textures = {}        # texture_id -> {"name": ..., "path": ...} on disk
        self.display = {}         # display_config settings
        self.working_camera = {}  # newest `camera` message: Nomad's own viewport
        self.revision = 0         # bumped whenever the cache changes
        self.log = []
        self.verbose = False      # nomad_link.watch(): log every message that arrives
        self._pending_acks = {}   # request_id -> node path waiting for its mesh_id
        self._requested = set()   # mesh_ids we already asked a mesh_full for
        self._requested_textures = set()
        self._pending_states = {}  # object_state that arrived before its object
        self._callback = None
        self._last_ping = 0.0

    # ------------------------------------------------------------- lifecycle

    @property
    def status(self):
        return self.connection.status

    @property
    def connected(self):
        return self.connection.status == "Connected"

    def peer_has(self, capability):
        return capability in self.peer_capabilities

    def connect(self, host="", port=DEFAULT_PORT):
        self.disconnect()
        if not host:
            self.message = "Searching for Nomad..."
            found = transport.discover(port, timeout=2.0)
            if not found:
                self.message = "No Nomad answered the discovery broadcast"
                return False
            host, port = found
        self.host, self.port = host, int(port)
        self.message = "Connecting to %s:%d..." % (self.host, self.port)
        self.connection.connect(
            self.host, self.port, _load_tokens().get(self.host, ""),
            transport.VERSION, PROTOCOL,
        )
        self._install_pump()
        return True

    def disconnect(self):
        self.connection.disconnect()
        self._remove_pump()
        self.peer_capabilities = set()
        self.nomad_version = ""
        self.message = "Disconnected"

    def send(self, header, binary=b""):
        return self.connection.send(header, binary)

    def clear_scene(self):
        """Forget every object. Textures are immutable per id, so they survive."""
        self.meshes.clear()
        self.materials.clear()
        self.lights.clear()
        self.cameras.clear()
        self.groups.clear()
        del self.order[:]
        self._requested.clear()
        self._pending_states.clear()
        self._touch()

    def clear(self):
        """Forget everything cached, including textures and display settings.

        The cache lives in the session rather than on a node, so deleting and
        recreating a node does not reset it: loading a different project in
        Nomad otherwise leaves the previous scene behind.
        """
        self.clear_scene()
        self.textures.clear()
        self._requested_textures.clear()
        self.display.clear()
        self.working_camera = {}
        self._pending_acks.clear()
        self.message = "Cache cleared"
        self._touch()

    def set_session(self, **flags):
        """Change Nomad's live-sync channels (PROTOCOL.md section 5).

        Nomad owns these settings; a stale base_revision is answered with the
        current config instead of applying the change, so we echo what we have.
        """
        # echo the whole config with our overrides on top: a partial message risks
        # being read as "every flag I left out is off"
        header = {key: value for key, value in self.session_config.items()
                  if key.startswith("sync_") or key in ("live_sync", "sync_mode")}
        header.update(flags)
        header["type"] = "set_session_config"
        header["base_revision"] = int(self.session_config.get("revision", 0))
        self.note("-> set_session_config %s" % sorted(
            key for key, value in header.items() if key.startswith("sync_") and value))
        return self.send(header)

    def request(self, kind, link_id=""):
        header = {"type": kind, "request_id": uuid.uuid4().hex}
        if link_id:
            header["link_id"] = link_id
        return self.send(header)

    # ------------------------------------------------------------- main loop

    def _install_pump(self):
        try:
            import hou
            if not hou.isUIAvailable() or self._callback is not None:
                return
        except ImportError:
            return
        self._callback = self.pump
        hou.ui.addEventLoopCallback(self._callback)

    def _remove_pump(self):
        if self._callback is None:
            return
        try:
            import hou
            hou.ui.removeEventLoopCallback(self._callback)
        except Exception:
            pass
        self._callback = None

    def describe(self, header):
        kind = header.get("type", "?")
        who = header.get("name") or header.get("link_id") or header.get("mesh_id") or ""
        live = " live" if header.get("live_sync") else ""
        return "<- %-16s %s%s" % (kind, str(who)[:24], live)

    def pump(self):
        """Drain the socket queue. Main thread only (event loop or hython loop)."""
        before = self.revision
        for header, binary in self.connection.poll():
            if self.verbose:
                self.note(self.describe(header))
            try:
                self._handle(header, binary)
            except Exception as exc:  # never let one bad packet kill the callback
                self.note("error handling %s: %s" % (header.get("type"), exc))
        if self.connection.status == "Error" and self.message != self.connection.error:
            self.message = self.connection.error or "Connection lost"
        if self.connected and time.time() - self._last_ping > PING_INTERVAL:
            self._last_ping = time.time()
            self.send({"type": "ping"})
        if self.revision != before:
            self._dirty_nodes()

    def note(self, text):
        self.log.append(text)
        del self.log[:-200]

    def _touch(self):
        self.revision += 1

    def _dirty_nodes(self):
        nodes = self._nodes()
        if nodes is not None:
            nodes.refresh_inputs(self.revision)

    # -------------------------------------------------------------- messages

    def _handle(self, header, binary):
        kind = header.get("type")
        if kind == "hello":
            self.nomad_version = header.get("nomad_version", "?")
            self.peer_capabilities = set(header.get("capabilities", []))
            # name the host: connecting to the iPad when you meant the demo (or the
            # reverse) otherwise looks identical from the node
            self.message = "Connected to Nomad %s at %s" % (self.nomad_version, self.host)
            token = header.get("pair_token")
            if token:
                _save_token(self.host, token)
        elif kind == "pairing_pending":
            self.message = "Waiting for approval in Nomad's Link menu"
        elif kind == "error":
            self.message = "Nomad: %s" % header.get("message", "error")
            self.note(self.message)
            self._requested.clear()
        elif kind == "mesh_full":
            mesh = convert.decode_mesh(header, binary)
            previous = self.meshes.get(mesh["mesh_id"])
            if previous is not None:  # absent means leave as-is, not reset
                for key in ("visible", "locked", "parent_id", "child_index"):
                    if key not in header and key in previous:
                        mesh[key] = previous[key]
            self._store(mesh)
            if "material" in header:  # mesh_full carries the same block as `material`
                self._store_material(header["mesh_id"], header["material"])
        elif kind == "mesh_instance":
            self._instance(header)
        elif kind == "mesh_delta":
            mesh = self.meshes.get(header.get("mesh_id"))
            if mesh is None or not convert.apply_delta(mesh, header, binary):
                self._recover(header.get("mesh_id", ""))
            else:
                self._touch()
        elif kind == "mesh_attributes":
            self._recover(header.get("mesh_id", ""))  # cheaper to just refetch
        elif kind == "object_state":
            self._object_state(header)
        elif kind == "object_delete":
            self._delete(header.get("link_id"))
        elif kind == "group":
            link_id = header.get("link_id", "")
            entry = self.groups.setdefault(link_id, {"link_id": link_id, "type": "group"})
            entry.update(header)
            self._touch()
        elif kind == "scene_batch":
            # applied in array order as one step: a re-parent has to land before
            # the delete that would otherwise orphan it
            for message in header.get("messages", ()):
                try:
                    self._handle(message, b"")
                except Exception as exc:  # one bad entry must not drop the rest
                    self.note("error in scene_batch %s: %s" % (message.get("type"), exc))
        elif kind == "material":
            self._store_material(header.get("mesh_id", ""), header.get("material", {}))
        elif kind in ("light", "camera_object"):
            store = self.lights if kind == "light" else self.cameras
            link_id = header.get("link_id", "")
            entry = store.setdefault(link_id, {"link_id": link_id, "type": kind})
            entry.update(header)  # only edited fields are sent; absent means unchanged
            self._touch()
        elif kind == "camera":
            self.working_camera = header  # newest wins, older pending ones are stale
        elif kind == "display_config":
            self.display.update(header.get("display", {}))
            self._touch()
        elif kind == "texture":
            self._store_texture(header, binary)
        elif kind == "mesh_ack":
            path = self._pending_acks.pop(header.get("request_id", ""), None)
            if path and self._nodes():
                self._nodes().store_mesh_id(path, header.get("mesh_id", ""))
        elif kind == "session_config":
            self.session_config = header
        elif kind in ("request_mesh", "request_selection", "request_scene"):
            if self._nodes():
                self._nodes().answer_request(header)

    @staticmethod
    def _nodes():
        """The Houdini-facing module, or None when running headless."""
        try:
            from . import nodes
        except ImportError:
            return None
        return nodes

    def _object_state(self, header):
        """Rename/move/hide, for whichever kind of object the id belongs to."""
        link_id = header.get("link_id")
        entry = (self.meshes.get(link_id) or self.lights.get(link_id)
                 or self.cameras.get(link_id) or self.groups.get(link_id))
        if entry is None:
            # a transfer can announce state before the geometry it describes;
            # hold it rather than dropping the only word we get on visibility
            self._pending_states[link_id] = header
            return
        entry["name"] = header.get("name", entry.get("name", ""))
        entry["visible"] = bool(header.get("visible", entry.get("visible", True)))
        if "locked" in header:
            entry["locked"] = bool(header["locked"])
        # absent parent_id leaves parenting alone, so a peer that does not model
        # hierarchy never flattens a tree (PROTOCOL.md section 3)
        for key in ("parent_id", "child_index", "local_matrix", "world_matrix_parent"):
            if key in header:
                entry[key] = header[key]
        if "world_matrix" in header:
            entry["world_matrix"] = list(header["world_matrix"])
        self._touch()

    def _store_material(self, mesh_id, material):
        if not mesh_id:
            return
        # only edited fields travel, so merge rather than replace
        entry = self.materials.setdefault(mesh_id, {})
        textures = entry.pop("textures", {})
        entry.update(material)
        incoming = material.get("textures")
        if incoming is not None:
            # a present channel is authoritative, an absent one keeps what we have
            textures.update(incoming)
        entry["textures"] = textures
        for channel in textures.values():
            texture_id = channel.get("texture_id")
            if texture_id and texture_id not in self.textures:
                self.request_texture(texture_id)
        self._touch()

    def request_texture(self, texture_id):
        if texture_id in self._requested_textures:
            return
        self._requested_textures.add(texture_id)
        self.send({"type": "request_texture", "texture_id": texture_id})

    def _store_texture(self, header, binary):
        """Blobs are immutable per id; cache them on disk so USD can reference them."""
        texture_id = header.get("texture_id", "")
        if not texture_id or not binary:
            return
        name = os.path.basename(str(header.get("name", "")))  # untrusted: basename only
        extension = os.path.splitext(name)[1].lower() or ".png"
        if extension not in (".png", ".jpg", ".jpeg", ".exr", ".tif", ".tiff", ".tga", ".webp"):
            extension = ".png"
        path = os.path.join(texture_cache(), texture_id + extension)
        if not os.path.exists(path):
            try:
                with open(path, "wb") as handle:
                    handle.write(binary)
            except OSError as exc:
                self.note("could not cache texture %s: %s" % (name or texture_id, exc))
                return
        self.textures[texture_id] = {"name": name, "path": path}
        self._requested_textures.discard(texture_id)
        self._touch()

    def _delete(self, link_id):
        """object_delete takes the node and its children (0.11.37)."""
        if not link_id:
            return
        doomed = [link_id]
        stores = (self.meshes, self.lights, self.cameras, self.groups)
        while True:
            children = [
                other for store in stores for other, entry in store.items()
                if entry.get("parent_id") in doomed and other not in doomed
            ]
            if not children:
                break
            doomed.extend(children)
        removed = False
        for victim in doomed:
            for store in stores + (self.materials,):
                removed = store.pop(victim, None) is not None or removed
        if removed:
            self.order = [i for i in self.order if i in self.meshes]
            self._touch()

    def _store(self, mesh):
        mesh_id = mesh["mesh_id"]
        if mesh_id not in self.meshes:
            self.order.append(mesh_id)
        self.meshes[mesh_id] = mesh
        pending = self._pending_states.pop(mesh_id, None)
        if pending is not None:
            self._object_state(pending)
        self._requested.discard(mesh_id)
        self._touch()

    def _instance(self, header):
        """Share an already-known geometry under a second mesh_id."""
        source = next(
            (m for m in self.meshes.values() if m["geometry_id"] == header.get("geometry_id")),
            None,
        )
        if source is None:
            self._recover(header.get("mesh_id", ""))
            return
        mesh = dict(source)
        mesh["mesh_id"] = header.get("mesh_id", "")
        # materials are keyed by mesh_id and Nomad sends one for the original, so
        # remember where this instance's geometry came from
        mesh["material_source"] = source.get("material_source") or source["mesh_id"]
        mesh["name"] = header.get("name", source["name"])
        mesh["visible"] = bool(header.get("visible", True))
        mesh["world_matrix"] = list(header.get("world_matrix", convert.IDENTITY))
        self._store(mesh)

    def _recover(self, mesh_id):
        if mesh_id and mesh_id not in self._requested:
            self._requested.add(mesh_id)
            self.request("request_mesh", mesh_id)

    # ----------------------------------------------------------- outgoing geo

    def send_mesh(self, header, binary, node_path=""):
        if node_path:
            if len(self._pending_acks) > 64:
                self._pending_acks.clear()  # acks that never came must not pile up
            self._pending_acks[header.get("request_id", "")] = node_path
        return self.send(header, binary)
