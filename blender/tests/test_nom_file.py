# SPDX-License-Identifier: GPL-3.0-or-later
"""Reader checks for the .nom importer. Runs outside Blender: python3 tests/test_nom_file.py

The corpus tests need Nomad's assets/ directory; they skip when it is not beside this repo.
"""
import glob
import json
import os
import struct
import sys

import numpy

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "nomad_blender_link"))

import nom_file  # noqa: E402

ASSETS = os.path.join(os.path.dirname(os.path.abspath(__file__)), *[".."] * 4, "assets")
TMP = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_tmp.nom")


def check(condition, message):
    if not condition:
        raise AssertionError(message)
    print("ok  " + message)


def corpus():
    return sorted(glob.glob(os.path.join(ASSETS, "**", "*.nom"), recursive=True))


def write_nom(js, binary, version=6):
    """A minimal well-formed v6 container, so the header tests need no fixture file."""
    payload = json.dumps(js).encode()
    head = struct.pack("<12sI", nom_file.MAGIC, version)
    offsets = struct.pack("<5Q", 72 + len(payload) + len(binary), 72, len(payload),
                          72 + len(payload), len(binary))
    raw = head + offsets + struct.pack("<2Q", 0, 0) + payload + binary
    with open(TMP, "wb") as handle:
        handle.write(raw)
    return raw


def test_header():
    write_nom({"scene": [], "meshes": []}, b"")
    nom = nom_file.NomFile(TMP)
    check(nom.version == 6, "version reads back")
    check(nom.json["scene"] == [], "json block parses")

    with open(TMP, "r+b") as handle:
        handle.write(b"Blender Sculp")
    try:
        nom_file.NomFile(TMP)
        raise AssertionError("a foreign magic must not parse")
    except ValueError:
        print("ok  a foreign magic is refused")

    raw = write_nom({"scene": []}, b"")
    with open(TMP, "wb") as handle:
        handle.write(raw[:len(raw) - 10])  # truncate into the json block
    try:
        nom_file.NomFile(TMP)
        raise AssertionError("a truncated file must not parse")
    except ValueError:
        print("ok  a truncated file is refused")
    os.remove(TMP)


def test_arrays():
    positions = numpy.array([[0, 1, 2], [3, 4, 5]], "<f4")
    js = {"mesh": {"vertices": {"count": 2, "type": "f32vec3", "offset": 0,
                                "length": positions.nbytes},
                   "masks": {"count": 4, "type": "u16", "only_zeros": True},
                   "faces": {"count": 1, "type": "f32vec2", "offset": 0, "length": 8},
                   "uvs": {"count": 1, "type": "f32vec2", "offset": 900, "length": 8}}}
    write_nom(js, positions.tobytes())
    nom = nom_file.NomFile(TMP)

    check(numpy.array_equal(nom.array(nom.json["mesh"], "vertices", "f32vec3"), positions),
          "an uncompressed array reads back")
    check(nom.array(nom.json["mesh"], "absent", "f32vec3") is None, "a missing array is None")
    zeros = nom.array(nom.json["mesh"], "masks", "u16")
    check(zeros.shape == (4, 1) and not zeros.any(), "only_zeros expands without touching the blob")

    for name, kind, why in (("faces", "i32vec4", "a type mismatch is caught"),
                            ("uvs", "f32vec2", "an out-of-range offset is caught")):
        try:
            nom.array(nom.json["mesh"], name, kind)
            raise AssertionError(why + " -- but it did not raise")
        except ValueError:
            print("ok  " + why)
    os.remove(TMP)


def lz4_literal_block(data):
    """A valid literal-only LZ4 block (a block may end after its literals)."""
    out = bytearray([min(len(data), 15) << 4])
    if len(data) >= 15:
        rest = len(data) - 15
        while rest >= 255:
            out.append(255)
            rest -= 255
        out.append(rest)
    return bytes(out) + data


def chunked_lz4(data, chunk):
    """v6 framing: [u32 raw][u32 comp][block] per chunk."""
    out = b""
    for start in range(0, len(data), chunk):
        piece = data[start:start + chunk]
        block = lz4_literal_block(piece)
        out += struct.pack("<II", len(piece), len(block)) + block
    return out


def test_lz4_chunked():
    """The v6 chunk loop -- the shipped corpus is all v4 solo, so exercise it synthetically."""
    values = numpy.arange(300, dtype="<f4").reshape(100, 3)
    payload = chunked_lz4(values.tobytes(), 128)
    js = {"mesh": {"vertices": {"count": 100, "type": "f32vec3", "offset": 0,
                                "length": len(payload), "lz4": True}}}
    write_nom(js, payload, version=6)
    backends = {"python": nom_file._lz4_python}
    for name, factory in (("lz4.block", nom_file._lz4_native), ("ctypes", nom_file._lz4_ctypes)):
        try:
            backends[name] = factory()
        except Exception:
            pass
    for name, decode in backends.items():
        nom_file._backend = (name, decode)
        nom = nom_file.NomFile(TMP)
        got = nom.array(nom.json["mesh"], "vertices", "f32vec3")
        if not numpy.array_equal(got, values):
            raise AssertionError(f"{name}: chunked decode mismatch")
    nom_file._backend = None
    check(True, f"v6 chunked framing decodes on: {', '.join(sorted(backends))}")

    # solo framing (pre-v6): the same bytes as one block, no chunk headers
    solo = lz4_literal_block(values.tobytes())
    js["mesh"]["vertices"]["length"] = len(solo)
    write_nom(js, solo, version=5)
    nom = nom_file.NomFile(TMP)
    check(numpy.array_equal(nom.array(nom.json["mesh"], "vertices", "f32vec3"), values),
          "pre-v6 solo framing decodes")
    os.remove(TMP)


def test_masks_u8():
    """Legacy files store u16 paint as bytes; the reader widens like BinJson does."""
    stored = numpy.array([0, 1, 128, 255], "u1")
    js = {"mesh": {"masks": {"count": 4, "type": "u8", "offset": 0, "length": 4},
                   "colors": {"count": 1, "type": "u8", "offset": 0, "length": 4}}}
    write_nom(js, stored.tobytes())
    nom = nom_file.NomFile(TMP)
    masks = nom.array(nom.json["mesh"], "masks", "u16")
    check(masks.dtype == numpy.uint16 and list(masks[:, 0]) == [0, 257, 32896, 65535],
          "u8 masks widen to u16 (255 -> 65535)")
    try:
        nom.array(nom.json["mesh"], "colors", "u8rgbm")
        raise AssertionError("widening must stay masks-only -- but u8rgbm accepted u8")
    except ValueError:
        print("ok  widening is u16-only, other mismatches still raise")
    os.remove(TMP)


def test_multires_graft():
    """A compressed-multires save has vertices at the mesh entry and faces only on the
    active level; the two must pair up. A level that dropped them too is unrecoverable."""
    positions = numpy.array([[0, 0, 0], [1, 0, 0], [1, 1, 0], [0, 1, 0]], "<f4")
    faces = numpy.array([[0, 1, 2, 3]], "<i4")
    binary = positions.tobytes() + faces.tobytes()
    entry = {"count_vertex": 4, "count_face": 1,
             "vertices": {"count": 4, "type": "f32vec3", "offset": 0, "length": positions.nbytes},
             "multires_level": 1,
             "multires_levels": [{}, {"faces": {"count": 1, "type": "i32vec4",
                                                "offset": positions.nbytes,
                                                "length": faces.nbytes}}]}
    write_nom({"scene": [{"name": "m", "mesh": 0}], "meshes": [entry]}, binary)
    nom = nom_file.NomFile(TMP)
    got = nom_file.geometry(nom, 0)
    check(got is not None and numpy.array_equal(got.faces, faces),
          "mesh vertices pair with the active level's faces")

    entry["multires_levels"][1] = {}  # the level dropped its faces too (derivable)
    write_nom({"scene": [{"name": "m", "mesh": 0}], "meshes": [entry]}, binary)
    nom = nom_file.NomFile(TMP)
    check(nom_file.geometry(nom, 0) is None,
          "a level without faces reports unsupported instead of misreading")
    os.remove(TMP)


def test_triplanar_baked():
    """A triplanar's main slots are the canvas; the bake hint is what imports."""
    canvas = numpy.zeros((9, 3), "<f4")
    baked_v = numpy.array([[0, 0, 0], [1, 0, 0], [1, 1, 0]], "<f4")
    baked_f = numpy.array([[0, 1, 2, -1]], "<i4")
    binary = canvas.tobytes() + baked_v.tobytes() + baked_f.tobytes()
    entry = {"mesh_type": "triplanar", "count_vertex": 9, "count_face": 0, "material": 3,
             "vertices": {"count": 9, "type": "f32vec3", "offset": 0, "length": canvas.nbytes},
             "baked": {"vertices": {"count": 3, "type": "f32vec3", "offset": canvas.nbytes,
                                    "length": baked_v.nbytes},
                       "faces": {"count": 1, "type": "i32vec4",
                                 "offset": canvas.nbytes + baked_v.nbytes, "length": baked_f.nbytes}}}
    write_nom({"scene": [{"name": "t", "mesh": 0}], "meshes": [entry]}, binary)
    nom = nom_file.NomFile(TMP)
    got = nom_file.geometry(nom, 0)
    check(got is not None and numpy.array_equal(got.vertices, baked_v)
          and numpy.array_equal(got.faces, baked_f), "the bake imports, not the canvas")
    check(got.material == 3, "material rides the mesh entry, not the hint")

    del entry["baked"]  # older save: no hint, skip as before
    write_nom({"scene": [{"name": "t", "mesh": 0}], "meshes": [entry]}, binary)
    check(nom_file.geometry(nom_file.NomFile(TMP), 0) is None, "no hint still skips")
    os.remove(TMP)


def test_repeats_hint():
    matrix = [1.0, 0, 0, 0, 0, 1.0, 0, 0, 0, 0, 1.0, 0, 2.0, 0, 0, 1.0]
    write_nom({"scene": [{"name": "a", "repeats": [matrix, [1, 2], "junk"]},
                         {"name": "b"}]}, b"")
    nodes = nom_file.scene(nom_file.NomFile(TMP))
    check(nodes[0].repeats == [matrix], "well-formed repeat matrices parse, junk is dropped")
    check(nodes[1].repeats == [], "no hint means no repeats")
    os.remove(TMP)


def test_composited():
    """The numpy compositor against a literal scalar transcription of LayerHelper::blend."""
    import math as pymath

    def tos(v):
        return v * 12.92 if v < 0.0031308 else 1.055 * (max(v, 0.0) ** (1 / 2.4)) - 0.055

    def tol(v):
        return v / 12.92 if v < 0.04045 else ((v + 0.055) / 1.055) ** 2.4

    def blend_scalar(a, b, mode):
        if mode == "normal":
            return b
        if mode == "multiply":
            return tol(tos(a) * tos(b))
        if mode == "screen":
            return tol(1.0 - (1.0 - tos(a)) * (1.0 - tos(b)))
        if mode == "overlay":
            sa, sb = tos(a), tos(b)
            return tol(sa * sb * 2.0 if sa <= 0.5 else 1.0 - 2.0 * (1.0 - sa) * (1.0 - sb))
        raise AssertionError(mode)

    # geometry: base color + gray, one layer per blend mode, half alpha, factor 0.7
    rng = [0.03, 0.2, 0.5, 0.8, 1.0]
    count = len(rng)
    base_rgb = numpy.array([[v, 1.0 - v, 0.4] for v in rng])
    layer_rgb = numpy.array([[0.6, v, 0.1] for v in rng])
    alpha = numpy.array([0, 64, 128, 192, 255], "u1")

    def encode_rgbm(rgb):
        m = numpy.clip(numpy.ceil(rgb.max(axis=1) * 255.0), 1.0, 255.0)
        return numpy.concatenate((rgb * (65025.0 / m)[:, None] + 0.5, m[:, None] + 0.5), 1).astype("u1")

    class FakeGeometry:
        vertices = numpy.zeros((count, 3), "<f4")
        colors = encode_rgbm(base_rgb)
        roughness = numpy.array([[int(v * 255)] for v in rng], "u1")
        metalness = None
        opacity = None
        masks = None
        layers = []

    for mode in ("normal", "multiply", "screen", "overlay"):
        FakeGeometry.layers = [{
            "config": {"factor": 0.7, "blend_color": mode, "blend_roughness": "auto"},
            "colors": encode_rgbm(layer_rgb),
            "opacity_color": alpha.reshape(-1, 1),
            "roughness": numpy.array([[200]] * count, "u1"),
            "opacity_roughness": alpha.reshape(-1, 1),
            "metalness": None, "opacity_metalness": None,
            "opacity": None, "opacity_opacity": None,
            "offsets": None,
        }]
        got = nom_file.composited(FakeGeometry)
        base_dec = FakeGeometry.colors.astype(float)
        base_dec = base_dec[:, :3] * (base_dec[:, 3:4] / 65025.0)
        layer_dec = FakeGeometry.layers[0]["colors"].astype(float)
        layer_dec = layer_dec[:, :3] * (layer_dec[:, 3:4] / 65025.0)
        for i in range(count):
            t = alpha[i] / 255.0 * 0.7
            want = [base_dec[i][c] * (1 - t) + blend_scalar(base_dec[i][c], layer_dec[i][c], mode) * t
                    for c in range(3)]
            if not numpy.allclose(got["color"][i], want, atol=1e-9):
                raise AssertionError(f"{mode} row {i}: {got['color'][i]} != {want}")
            tr = alpha[i] / 255.0 * 0.7  # roughness rides blend_roughness=auto -> blend_color
            base_r = FakeGeometry.roughness[i][0] / 255.0
            want_r = base_r * (1 - tr) + blend_scalar(base_r, 200 / 255.0, mode) * tr
            if abs(got["roughness"][i] - want_r) > 1e-9:
                raise AssertionError(f"{mode} gray row {i}")
        check(True, f"{mode} matches the scalar reference (color + auto-fallback gray)")

    # hidden layer and factor 0 leave the base untouched
    FakeGeometry.layers[0]["config"] = {"factor": 0.7, "visible": False}
    got = nom_file.composited(FakeGeometry)
    check(numpy.allclose(got["color"], base_dec, atol=1e-12), "hidden layer leaves the base")

    # layer-only channel starts from the material value
    FakeGeometry.colors = None
    FakeGeometry.roughness = None
    FakeGeometry.layers[0]["config"] = {"factor": 1.0}
    got = nom_file.composited(FakeGeometry, {"color": [0.2, 0.3, 0.4]})
    t = (alpha / 255.0)[:, None]
    want = numpy.array([0.2, 0.3, 0.4]) * (1 - t) + layer_dec * t
    check(numpy.allclose(got["color"], want, atol=1e-9), "base-less channel starts from the material")


def test_backends_agree():
    """The three LZ4 paths must be interchangeable: the fast ones are only worth having
    if they are exact."""
    files = corpus()
    if not files:
        print("--  no assets/ beside the repo, skipped")
        return
    available = {"python": nom_file._lz4_python}
    for name, factory in (("lz4.block", nom_file._lz4_native), ("ctypes", nom_file._lz4_ctypes)):
        try:
            available[name] = factory()
        except Exception:
            pass
    check("python" in available, f"backends probed: {', '.join(sorted(available))}")

    for path in files:
        reference = None
        for name, decode in available.items():
            nom_file._backend = (name, decode)
            nom = nom_file.NomFile(path)
            digest = []
            for node in nom_file.scene(nom):
                geometry = nom_file.geometry(nom, node.mesh) if node.mesh is not None else None
                if geometry is None:
                    continue
                digest.append(tuple(None if getattr(geometry, key) is None
                                    else getattr(geometry, key).tobytes()
                                    for key in nom_file.GEOMETRY))
            if reference is None:
                reference = digest
            elif digest != reference:
                raise AssertionError(f"{name} disagrees on {os.path.basename(path)}")
    nom_file._backend = None
    check(True, f"{len(available)} backends agree on all {len(files)} files")


def test_corpus_geometry():
    files = corpus()
    if not files:
        print("--  no assets/ beside the repo, skipped")
        return
    meshes = 0
    for path in files:
        nom = nom_file.NomFile(path)
        for node in nom_file.scene(nom):
            geometry = nom_file.geometry(nom, node.mesh) if node.mesh is not None else None
            if geometry is None:
                continue
            meshes += 1
            faces, vertices = geometry.faces, geometry.vertices
            check_name = f"{os.path.basename(path)}:{node.name}"
            if faces.min() < -1 or faces.max() >= len(vertices):
                raise AssertionError(f"{check_name}: face index out of range")
            if (faces[:, :3] < 0).any():
                raise AssertionError(f"{check_name}: only the 4th corner may be absent")
            if not numpy.isfinite(vertices).all():
                raise AssertionError(f"{check_name}: non-finite position")
            if geometry.uvs is not None and geometry.faces_uv is not None:
                if geometry.faces_uv[:, :3].max() >= len(geometry.uvs):
                    raise AssertionError(f"{check_name}: uv index out of range")
    check(meshes > 0, f"{meshes} meshes across {len(files)} files have sane topology")


def test_scene_tree():
    files = corpus()
    if not files:
        print("--  no assets/ beside the repo, skipped")
        return
    for path in files:
        nodes = nom_file.scene(nom_file.NomFile(path))
        for node in nodes:
            if node.parent is not None and node.parent >= node.index:
                raise AssertionError(f"{path}: parent must come first")
            if len(node.matrix) != 16:
                raise AssertionError(f"{path}: matrix is not a mat4")
    check(True, "every scene tree is flattened parents-first")


if __name__ == "__main__":
    for name, function in sorted(globals().items()):
        if name.startswith("test_"):
            print("--- %s" % name)
            function()
    print("\nall good")
