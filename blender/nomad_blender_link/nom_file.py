# Nomad Blender Link
# Copyright (C) 2024-2026 Hexanomad
# SPDX-License-Identifier: GPL-3.0-or-later
"""Reader for Nomad Sculpt .nom files. Deliberately free of bpy so it runs (and is
testable) outside Blender: everything here turns a file into numpy arrays."""

import ctypes
import ctypes.util
import glob
import json
import os
import struct
import sys

import numpy

MAGIC = b"Nomad Sculpt"
MAGIC_LEGACY = 0x6D26A524
CHUNK = 1024 * 1024  # writer splits arrays into independently compressed blocks

# name -> (numpy dtype, components). The stored "type" string is checked against this.
TYPES = {
    "f32vec3": ("<f4", 3),
    "f32vec2": ("<f4", 2),
    "i32vec4": ("<i4", 4),
    "i32": ("<i4", 1),
    "u8rgbm": ("u1", 4),
    "u8vec2": ("u1", 2),
    "u16": ("<u2", 1),
    "u8": ("u1", 1),
}

# per-layer paint arrays, value + alpha pairs
LAYER_PAINT = {
    "colors": "u8rgbm",
    "opacity_color": "u8",
    "roughness": "u8",
    "opacity_roughness": "u8",
    "metalness": "u8",
    "opacity_metalness": "u8",
    "opacity": "u8",
    "opacity_opacity": "u8",
}

# geometry arrays we import, and the type each one is written with
GEOMETRY = {
    "vertices": "f32vec3",
    "faces": "i32vec4",
    "faces_uv": "i32vec4",
    "faces_group": "u16",
    "uvs": "f32vec2",
    "colors": "u8rgbm",
    "roughness": "u8",
    "metalness": "u8",
    "opacity": "u8",
    "masks": "u16",
}


# ---------------------------------------------------------------- lz4 decoding

def _lz4_python(src, size):
    """Reference LZ4 block decoder. Correct everywhere, ~25 MB/s -- the last resort."""
    out = bytearray(size)
    end = len(src)
    s = d = 0
    while s < end:
        token = src[s]
        s += 1
        length = token >> 4
        if length == 15:
            while True:
                extra = src[s]
                s += 1
                length += extra
                if extra != 255:
                    break
        if length:
            out[d:d + length] = src[s:s + length]
            s += length
            d += length
        if s >= end:
            break
        offset = src[s] | (src[s + 1] << 8)
        s += 2
        match = (token & 15) + 4
        if (token & 15) == 15:
            while True:
                extra = src[s]
                s += 1
                match += extra
                if extra != 255:
                    break
        start = d - offset
        if offset >= match:
            out[d:d + match] = out[start:start + match]
        else:
            # overlapping match: repeat the pattern instead of copying byte by byte
            pattern = bytes(out[start:d])
            out[d:d + match] = (pattern * (-(-match // offset)))[:match]
        d += match
    if d != size:
        raise ValueError("Truncated LZ4 block")
    return bytes(out)


# smallest valid block: one 3-literal sequence, no match
PROBE = (b"\x30abc", 3, b"abc")


def _lz4_native():
    """python-lz4 if the user (or a bundled wheel) has it."""
    import lz4.block

    def decode(src, size):
        return lz4.block.decompress(src, uncompressed_size=size)

    if decode(PROBE[0], PROBE[1]) != PROBE[2]:
        raise ValueError("lz4.block failed the probe")
    return decode


def _lz4_libraries():
    """Paths that may export a C LZ4_decompress_safe. Blender bundles LZ4 inside
    OpenVDB (via Blosc), which costs us nothing to borrow."""
    roots = []
    prefix = os.path.dirname(sys.exec_prefix)  # .../<version>/python -> .../<version>
    roots += [os.path.join(prefix, "lib"), os.path.join(os.path.dirname(prefix), "lib")]
    for root in roots:
        for name in ("libopenvdb*", "liblz4*", "libblosc*", "openvdb*"):
            for suffix in (".dylib", ".so", ".so.*", ".dll"):
                yield from sorted(glob.glob(os.path.join(root, name + suffix)))
    found = ctypes.util.find_library("lz4")
    if found:
        yield found
    yield None  # already-loaded symbols (flat namespace on macOS/Linux)


def _lz4_ctypes():
    for path in _lz4_libraries():
        try:
            library = ctypes.CDLL(path)
            native = library.LZ4_decompress_safe
        except (OSError, AttributeError):
            continue
        native.restype = ctypes.c_int
        native.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int, ctypes.c_int]

        def decode(src, size, _native=native):
            out = ctypes.create_string_buffer(size)
            if _native(src, out, len(src), size) != size:
                raise ValueError("Truncated LZ4 block")
            return out.raw

        try:
            if decode(PROBE[0], PROBE[1]) == PROBE[2]:
                return decode
        except (ValueError, OSError):
            continue
    raise OSError("no LZ4 library found")


def lz4_backend():
    """(name, decode) for the fastest LZ4 block decoder available. The native paths run
    ~100x the pure-python one, so it is worth probing for them."""
    global _backend
    if _backend is None:
        for name, factory in (("lz4.block", _lz4_native), ("ctypes", _lz4_ctypes)):
            try:
                _backend = (name, factory())
                break
            except Exception:
                continue
        else:
            _backend = ("python", _lz4_python)
    return _backend


_backend = None


# ------------------------------------------------------------------ file layout

class NomFile:
    """A parsed .nom: the scene description as plain json, plus lazy array access."""

    def __init__(self, path):
        with open(path, "rb") as handle:
            raw = handle.read()
        if len(raw) <= 72:
            raise ValueError("File too small")

        self.path = path
        legacy = struct.unpack_from("<I", raw, 0)[0] == MAGIC_LEGACY
        if legacy:
            self.version = struct.unpack_from("<I", raw, 4)[0]
            json_size = struct.unpack_from("<Q", raw, 16)[0]
            json_offset, bin_offset = 32, 32 + json_size
            bin_size = struct.unpack_from("<Q", raw, 24)[0]
            self.thumbnail = None
        elif raw[:12] == MAGIC:
            self.version = struct.unpack_from("<I", raw, 12)[0]
            json_offset, json_size, bin_offset, bin_size = struct.unpack_from("<4Q", raw, 24)
            thumb_offset, thumb_dim = struct.unpack_from("<2Q", raw, 56)
            self.thumbnail = ((thumb_dim, raw[thumb_offset:thumb_offset + thumb_dim * thumb_dim * 4])
                              if thumb_offset and thumb_dim else None)
        else:
            raise ValueError("Not a Nomad file")

        if self.version < 1:
            raise ValueError("Unsupported version")
        if json_offset + json_size > len(raw) or bin_offset + bin_size > len(raw):
            raise ValueError("Truncated file")

        self.json = json.loads(raw[json_offset:json_offset + json_size])
        self._bin = memoryview(raw)[bin_offset:bin_offset + bin_size]
        self._solo = self.version < 6  # pre-v6 arrays are a single lz4 block, not chunked

    def array(self, node, name, kind):
        """The named array off `node` as an (n, components) array, or None if absent.
        `kind` is the expected TYPES key -- a stored type that disagrees is an error
        rather than a silent misread."""
        entry = node.get(name) if isinstance(node, dict) else None
        if not isinstance(entry, dict):
            return None
        dtype, components = TYPES[kind]
        count = int(entry.get("count", 0))
        if not count:
            return None
        if entry.get("only_zeros"):
            return numpy.zeros((count, components), dtype)

        stored = entry.get("type")
        # legacy files store some u16 paint (masks) as bytes; BinJson upconverts the same way
        widen = stored == "u8" and kind == "u16"
        if widen:
            dtype = "u1"
        elif stored is not None and stored != kind:
            raise ValueError(f"{name}: expected {kind}, found {stored}")
        offset, length = int(entry["offset"]), int(entry["length"])
        if offset < 0 or length < 0 or offset + length > len(self._bin):
            raise ValueError(f"{name}: array outside the binary block")

        size = count * components * numpy.dtype(dtype).itemsize
        if entry.get("lz4"):
            data = self._decompress(offset, length, size)
        else:
            data = bytes(self._bin[offset:offset + size])
        values = numpy.frombuffer(data, dtype=dtype, count=count * components).reshape(count, components)
        if widen:
            values = values.astype("<u2") * 257  # 255 -> 65535
        return values

    def _decompress(self, offset, length, size):
        decode = lz4_backend()[1]
        if self._solo:
            return decode(bytes(self._bin[offset:offset + length]), size)
        out = bytearray(size)
        source, cursor = offset, 0
        while cursor < size:
            raw_size, comp_size = struct.unpack_from("<II", self._bin, source)
            block = bytes(self._bin[source + 8:source + 8 + comp_size])
            out[cursor:cursor + raw_size] = decode(block, raw_size)
            source += 8 + comp_size
            cursor += raw_size
        return bytes(out)


# ------------------------------------------------------------------- scene walk

class Node:
    """One entry of the scene tree, flattened. `parent` indexes into the same list."""

    def __init__(self, source, parent, index):
        self.index = index
        self.parent = parent
        self.name = source.get("name") or "Nomad"
        self.visible = bool(source.get("visible", True))
        self.mesh = source.get("mesh")
        self.group = source.get("group")
        self.light = source.get("light")
        self.camera = source.get("camera")
        matrix = source.get("matrix")
        # nom::mat4 serializes column-major; matrix_from_columns transposes into Matrix rows
        self.matrix = ([float(v) for v in matrix] if isinstance(matrix, list) and len(matrix) == 16
                       else [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1])
        repeats = source.get("repeats")
        # save-side hint only: each entry is one repeat instance's world transform
        self.repeats = [[float(v) for v in m] for m in repeats
                        if isinstance(m, list) and len(m) == 16] if isinstance(repeats, list) else []
        self.source = source


def scene(nom):
    """Depth-first flatten of nom.json["scene"], parents before children."""
    nodes = []

    def walk(entries, parent):
        for entry in entries or []:
            if not isinstance(entry, dict):
                continue
            node = Node(entry, parent, len(nodes))
            nodes.append(node)
            walk(entry.get("children"), node.index)

    walk(nom.json.get("scene"), None)
    return nodes


class Geometry:
    """The arrays needed to rebuild one mesh. Faces are Nomad's i32vec4 with w < 0
    marking a triangle; positions stay in Nomad space."""

    def __init__(self, nom, source):
        for name, kind in GEOMETRY.items():
            setattr(self, name, nom.array(source, name, kind))
        self.count_vertex = int(source.get("count_vertex", 0))
        self.count_face = int(source.get("count_face", 0))
        self.count_uv = int(source.get("count_uv", 0))
        self.material = source.get("material")
        self.active_layer = int(source.get("active_layer", -1))
        # dense per-vertex offsets and paint; a paint-only layer has no offsets, an
        # offsets-only layer no paint -- every entry stays, channels gate on their alpha
        self.layers = [dict({"config": jlayer, "offsets": nom.array(jlayer, "offsets", "f32vec3")},
                            **{key: nom.array(jlayer, key, kind) for key, kind in LAYER_PAINT.items()})
                       for jlayer in source.get("layers") or [] if isinstance(jlayer, dict)]

    def valid(self):
        return self.vertices is not None and self.faces is not None and len(self.faces)


def geometry(nom, mesh_index):
    """Geometry for meshes[mesh_index]. The mesh entry's arrays always hold the level the
    file was saved on (LoadNomad reads them straight into levels[multires_level]); a
    compressed-multires save drops the derivable face/uv arrays there, so graft those from
    the active level's own entry. A level that skipped them too can only be rebuilt by
    subdivision -- not supported, the mesh reports as skipped."""
    meshes = nom.json.get("meshes") or []
    if not isinstance(mesh_index, int) or not 0 <= mesh_index < len(meshes):
        return None
    source = meshes[mesh_index]
    # a triplanar's main slots hold its canvas planes, not the shape: the result regenerates
    # on load. Newer saves also carry the bake as a hint -- import that; without it, skip.
    if source.get("mesh_type") == "triplanar":
        baked = source.get("baked")
        if isinstance(baked, dict):
            found = Geometry(nom, baked)
            if found.valid():
                found.material = source.get("material")
                return found
        return None

    found = Geometry(nom, source)
    levels = source.get("multires_levels")
    if isinstance(levels, list) and levels:
        active = int(source.get("multires_level", 0))
        if 0 <= active < len(levels) and isinstance(levels[active], dict):
            for name in ("faces", "faces_uv", "faces_group", "uvs"):
                if getattr(found, name) is None:
                    setattr(found, name, nom.array(levels[active], name, GEOMETRY[name]))
    return found if found.valid() else None


def entry(nom, collection, index):
    """nom.json[collection][index] if it is a dict, else {} -- materials/lights/cameras."""
    items = nom.json.get(collection) or []
    if not isinstance(index, int) or not 0 <= index < len(items):
        return {}
    return items[index] if isinstance(items[index], dict) else {}


def material(nom, index):
    return entry(nom, "materials", index)


def blob(nom, source):
    """Raw embedded bytes for an {offset, length} entry (images, assets); never lz4."""
    if not isinstance(source, dict):
        return b""
    offset, length = int(source.get("offset", -1)), int(source.get("length", 0))
    if offset < 0 or length <= 0 or offset + length > len(nom._bin):
        return b""
    return bytes(nom._bin[offset:offset + length])


# ------------------------------------------------------------- paint compositing
# Mirrors t_updateRenderLayer + LayerHelper::blend: the file stores the base paint and the
# per-layer paint separately, the composited result Nomad renders is rebuilt here.

_LUM = numpy.array([0.299, 0.587, 0.114])


def _srgb(v):
    v = numpy.maximum(v, 0.0)
    return numpy.where(v < 0.0031308, v * 12.92, 1.055 * numpy.power(v, 1.0 / 2.4) - 0.055)


def _lin(v):
    return numpy.where(v < 0.04045, v / 12.92, numpy.power(numpy.maximum(v + 0.055, 0.0) / 1.055, 2.4))


def _clip_color(c):
    l = (c @ _LUM)[:, None]
    n = c.min(axis=1, keepdims=True)
    x = c.max(axis=1, keepdims=True)
    c = numpy.where(n < 0.0, l + (c - l) * (l / numpy.where(l - n == 0.0, 1e-6, l - n)), c)
    return numpy.where(x > 1.0, l + (c - l) * ((1.0 - l) / numpy.where(x - l == 0.0, 1e-6, x - l)), c)


def _set_lum(c, l):
    return _clip_color(c + (l - (c @ _LUM))[:, None])


def _set_sat(c, s):
    lo = c.min(axis=1, keepdims=True)
    hi = c.max(axis=1, keepdims=True)
    span = numpy.where(hi > lo, hi - lo, 1.0)
    return numpy.where(hi > lo, (c - lo) * (s[:, None] / span), 0.0)


def _blend(a, b, mode, gray):
    """LayerHelper::blend. a/b are linear, (n,) gray or (n,3) rgb; unknown mode = top."""
    eps = 1e-6
    if mode == "normal" or mode == "auto":
        return b
    if mode == "darken":
        return numpy.minimum(a, b)
    if mode == "lighten":
        return numpy.maximum(a, b)
    if mode == "darker_color":
        if gray:
            return numpy.where(a <= b, a, b)
        keep = (_srgb(a) @ _LUM) <= (_srgb(b) @ _LUM)
        return numpy.where(keep[:, None], a, b)
    if mode == "lighter_color":
        if gray:
            return numpy.where(a > b, a, b)
        keep = (_srgb(a) @ _LUM) > (_srgb(b) @ _LUM)
        return numpy.where(keep[:, None], a, b)

    if mode in ("hue", "saturation", "color", "luminosity"):
        # linear, unlike every other mode, to match Photoshop and Procreate
        if gray:
            return a if mode == "luminosity" else b
        sat = lambda c: c.max(axis=1) - c.min(axis=1)
        if mode == "hue":
            return _set_lum(_set_sat(b, sat(a)), a @ _LUM)
        if mode == "saturation":
            return _set_lum(_set_sat(a, sat(b)), a @ _LUM)
        if mode == "color":
            return _set_lum(b, a @ _LUM)
        return _set_lum(a, b @ _LUM)

    sa, sb = _srgb(a), _srgb(b)
    if mode == "multiply":
        out = sa * sb
    elif mode == "color_burn":
        out = numpy.maximum(1.0 - (1.0 - sa) / numpy.maximum(sb, eps), 0.0)
    elif mode == "linear_burn":
        out = numpy.maximum(sa + sb - 1.0, 0.0)
    elif mode == "screen":
        out = 1.0 - (1.0 - sa) * (1.0 - sb)
    elif mode == "color_dodge":
        out = numpy.minimum(sa / numpy.maximum(1.0 - sb, eps), 1.0)
    elif mode == "linear_dodge":
        out = numpy.minimum(sa + sb, 1.0)
    elif mode == "overlay":
        out = numpy.where(sa <= 0.5, sa * sb * 2.0, 1.0 - 2.0 * (1.0 - sa) * (1.0 - sb))
    elif mode == "soft_light":
        out = numpy.where(sb <= 0.5, sa * (sb * 2.0 * (1.0 - sa) + sa),
                          sa * 2.0 * (1.0 - sb) + numpy.sqrt(sa) * (sb * 2.0 - 1.0))
    elif mode == "hard_light":
        out = numpy.where(sb <= 0.5, sa * sb * 2.0, 1.0 - 2.0 * (1.0 - sa) * (1.0 - sb))
    elif mode == "vivid_light":
        out = numpy.where(sb <= 0.5,
                          numpy.maximum(1.0 - (1.0 - sa) / numpy.maximum(sb * 2.0, eps), 0.0),
                          numpy.minimum(sa / numpy.maximum(2.0 - 2.0 * sb, eps), 1.0))
    elif mode == "linear_light":
        out = numpy.clip(sa + sb * 2.0 - 1.0, 0.0, 1.0)
    elif mode == "pin_light":
        out = numpy.where(sb <= 0.5, numpy.minimum(sa, sb * 2.0), numpy.maximum(sa, sb * 2.0 - 1.0))
    elif mode == "hard_mix":
        out = numpy.where(sa + sb >= 1.0, 1.0, 0.0)
    elif mode == "difference":
        out = numpy.abs(sa - sb)
    elif mode == "exclusion":
        out = sa + sb - sa * sb * 2.0
    elif mode == "subtract":
        out = numpy.maximum(sa - sb, 0.0)
    elif mode == "divide":
        out = numpy.minimum(sa / numpy.maximum(sb, eps), 1.0)
    else:
        return b
    return _lin(out)


# channel -> (geometry base attr, layer value key, layer alpha key, config suffix, material key)
_PAINT_CHANNELS = {
    "color": ("colors", "colors", "opacity_color", "color", "color"),
    "roughness": ("roughness", "roughness", "opacity_roughness", "roughness", "roughness"),
    "metalness": ("metalness", "metalness", "opacity_metalness", "metalness", "metalness"),
    "opacity": ("opacity", "opacity", "opacity_opacity", "opacity", "opacity"),
}
_PAINT_DEFAULTS = {"color": (1.0, 1.0, 1.0), "roughness": 0.25, "metalness": 0.0, "opacity": 1.0}


def composited(geometry, material=None, compose=True):
    """{channel: float array} of the paint Nomad renders: every layer blended over the base
    at alpha * saturate(factor * channel factor), bottom to top. A channel present nowhere
    is absent; a base-less channel starts from the material value, linear throughout.
    compose=False decodes the bare base arrays instead, layers ignored."""
    material = material if isinstance(material, dict) else {}
    count = len(geometry.vertices) if geometry.vertices is not None else 0
    out = {}
    for channel, (base_key, value_key, alpha_key, suffix, mat_key) in _PAINT_CHANNELS.items():
        gray = channel != "color"

        def decode(array):
            v = array.astype(numpy.float64)
            return v[:, 0] / 255.0 if gray else v[:, :3] * (v[:, 3:4] / 65025.0)

        active = []
        for layer in geometry.layers if compose else []:
            config = layer["config"]
            if layer[alpha_key] is None or layer[value_key] is None:
                continue
            if not (config.get("visible", True) and config.get("visible_" + suffix, True)):
                continue
            factor = min(max(float(config.get("factor", 1.0))
                             * float(config.get("factor_" + suffix, 1.0)), 0.0), 1.0)
            if not factor:
                continue
            mode = str(config.get("blend_" + suffix, "normal"))
            if gray and mode == "auto":
                mode = str(config.get("blend_color", "normal"))
            active.append((layer, factor, mode))

        base = getattr(geometry, base_key)
        if base is None and not active:
            continue
        if base is not None:
            dst = decode(base)
        else:
            fill = material.get(mat_key, _PAINT_DEFAULTS[channel])
            dst = (numpy.tile(numpy.asarray(fill, numpy.float64)[:3], (count, 1)) if not gray
                   else numpy.full(count, float(fill)))
        for layer, factor, mode in active:
            blended = _blend(dst, decode(layer[value_key]), mode, gray)
            t = layer[alpha_key][:, 0].astype(numpy.float64) / 255.0 * factor
            if not gray:
                t = t[:, None]
            dst = dst * (1.0 - t) + blended * t
        out[channel] = dst
    return out


def environment(nom):
    """(name, bytes) of the embedded environment image, or None. Built-in environments
    travel by name only -- no blob, nothing to build a world from."""
    for asset in nom.json.get("assets") or []:
        if isinstance(asset, dict) and asset.get("collection") == "environments":
            data = blob(nom, asset)
            if data:
                return str(asset.get("name") or "Nomad Environment"), data
    return None


if __name__ == "__main__":
    for path in sys.argv[1:]:
        nom = NomFile(path)
        nodes = scene(nom)
        print(f"{os.path.basename(path)}  v{nom.version}  lz4={lz4_backend()[0]}  "
              f"{len(nodes)} nodes, {len(nom.json.get('meshes') or [])} meshes")
        for node in nodes:
            geom = geometry(nom, node.mesh) if node.mesh is not None else None
            detail = ""
            if geom is not None:
                quads = int((geom.faces[:, 3] >= 0).sum())
                detail = (f"  {len(geom.vertices)}v {len(geom.faces)}f "
                          f"({quads} quad / {len(geom.faces) - quads} tri)"
                          f"{'  uv' if geom.uvs is not None else ''}"
                          f"{'  color' if geom.colors is not None else ''}")
            print(f"  {'  ' * (0 if node.parent is None else 1)}{node.name}{detail}")
