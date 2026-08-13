# SPDX-License-Identifier: MIT
"""Build the Nomad scene in Maya.

Maya is a scene, not a graph: objects are created once and then patched, so a
sculpt stroke moves points on the mesh that already exists rather than
rebuilding it. That is the opposite of the Houdini bridge and much closer to
how the Blender extension works.

Conventions, which differ from the Houdini side:

- **Winding is not flipped.** Maya's polygons are counter-clockwise
  front-facing, the same as glTF and therefore Nomad.
- **v is flipped.** Maya's uv origin is bottom-left, Nomad's is top-left.
- **Units.** Maya is centimetres by default and Nomad's units are arbitrary,
  hence `SCALE`.
- **Matrices** are row-major with row vectors, like Houdini and USD, which
  makes Nomad's column-major list transfer across unchanged.

maya.cmds does the structural work (creating, parenting, attributes) because it
is forgiving and readable; OpenMaya does the per-vertex work, where cmds would
be far too slow.
"""
import maya.cmds as cmds
from maya.api import OpenMaya

from . import convert, materials
from .client import client

ROOT = "nomad"
SCALE = 1.0          # multiplies incoming positions; Maya's unit is the centimetre
_built = {}          # link_id -> maya transform path
_shaders = {}        # mesh_id -> (shader, shading group)
_revision = -1


# ------------------------------------------------------------------ utilities

def valid_name(name):
    """Maya names allow letters, digits and underscores, and cannot lead with a digit."""
    cleaned = "".join(character if character.isalnum() else "_" for character in (name or "obj"))
    return ("n_" + cleaned) if cleaned[:1].isdigit() else (cleaned or "obj")


def matrix_values(values, scale=1.0):
    """Nomad's column-major 16 floats -> the list MMatrix wants.

    Maya multiplies row vectors, which makes its layout the transpose of
    glTF's, so the flat list transfers straight across. Only the translation
    needs the scene scale applied.
    """
    out = [float(v) for v in values]
    out[12] *= scale
    out[13] *= scale
    out[14] *= scale
    return out


def dag_path(name):
    """MObject for a node, or None."""
    try:
        selection = OpenMaya.MSelectionList()
        selection.add(name)
        return selection.getDagPath(0)
    except Exception:
        return None


# --------------------------------------------------------------------- meshes

def point_array(positions):
    """MPointArray from (n, 3) floats.

    The constructor converts a whole sequence in C++; appending an MPoint per
    vertex means hundreds of thousands of Python objects for one sculpt.
    """
    values = positions.tolist()
    try:
        return OpenMaya.MPointArray(values)
    except (TypeError, ValueError):
        array = OpenMaya.MPointArray()
        for x, y, z in values:
            array.append(OpenMaya.MPoint(x, y, z))
        return array


def int_array(values):
    numbers = [int(v) for v in values.tolist()]
    try:
        return OpenMaya.MIntArray(numbers)
    except (TypeError, ValueError):
        array = OpenMaya.MIntArray()
        for number in numbers:
            array.append(number)
        return array


def mesh_arrays(mesh, scale=SCALE):
    """Nomad's arrays -> (points, face counts, face connects) for MFnMesh."""
    return (point_array(mesh["positions"] * scale),
            int_array(mesh["sizes"]), int_array(mesh["corners"]))


def build_mesh(mesh, parent=None, scale=SCALE):
    """Create the transform and mesh shape for one Nomad object."""
    points, counts, connects = mesh_arrays(mesh, scale)
    transform = OpenMaya.MFnTransform().create()
    fn = OpenMaya.MFnMesh()
    fn.create(points, counts, connects, parent=transform)

    dag = OpenMaya.MFnDagNode(transform)
    dag.setName(valid_name(mesh["name"]))
    path = dag.fullPathName()
    if parent:
        path = cmds.parent(path, parent)[0]
        path = cmds.ls(path, long=True)[0]

    apply_uvs(fn, mesh)
    apply_colours(fn, mesh)
    assign_default_shader(path)
    return path


def assign_default_shader(path):
    """Without a shading group Maya draws the mesh flat green."""
    try:
        shapes = cmds.listRelatives(path, shapes=True, fullPath=True) or [path]
        cmds.sets(shapes, edit=True, forceElement="initialShadingGroup")
    except Exception:
        pass


def float_array(values):
    numbers = [float(v) for v in values]
    try:
        return OpenMaya.MFloatArray(numbers)
    except (TypeError, ValueError):
        array = OpenMaya.MFloatArray()
        for number in numbers:
            array.append(number)
        return array


def apply_uvs(fn, mesh):
    """Nomad's per-corner uvs, with v flipped to Maya's bottom-left origin."""
    if "texcoords" not in mesh:
        return
    texcoords = mesh["texcoords"]
    fn.setUVs(float_array(texcoords[:, 0]), float_array(1.0 - texcoords[:, 1]))
    fn.assignUVs(int_array(mesh["sizes"]), int_array(mesh["corner_uv"]))


def apply_colours(fn, mesh):
    """Vertex paint as a colour set, alpha included."""
    if "color" not in mesh:
        return
    import numpy

    rgb = mesh["color"]
    alpha = mesh.get("alpha")
    if alpha is None:
        alpha = numpy.ones(len(rgb), rgb.dtype)
    rgba = numpy.column_stack((rgb, alpha)).tolist()
    try:
        colours = OpenMaya.MColorArray(rgba)
    except (TypeError, ValueError):
        colours = OpenMaya.MColorArray()
        for values in rgba:
            colours.append(OpenMaya.MColor(values))
    vertices = int_array(numpy.arange(len(rgba), dtype="i4"))
    try:
        fn.createColorSet("nomad", False)
        fn.setCurrentColorSetName("nomad")
    except Exception:
        pass
    fn.setVertexColors(colours, vertices)


def update_points(path, mesh, scale=SCALE):
    """A sculpt stroke moved points: patch the existing mesh rather than rebuild."""
    dag = dag_path(path)
    if dag is None:
        return False
    try:
        fn = OpenMaya.MFnMesh(dag)
        points, _counts, _connects = mesh_arrays(mesh, scale)
        if fn.numVertices != len(points):
            return False  # topology changed; the caller rebuilds
        fn.setPoints(points)
        return True
    except Exception:
        return False


# ------------------------------------------------------- lights and cameras

# Nomad light type -> the Maya node that behaves most like it
LIGHTS = {"POINT": "pointLight", "SUN": "directionalLight", "SPOT": "spotLight",
          "AREA": "areaLight"}


def build_light(light):
    """Nomad lights aim down -Z with +Y up, as Maya's do, so the matrix transfers."""
    kind = str(light.get("light_type", "POINT")).upper()
    if kind == "ENVIRONMENT":
        return None  # Maya has no native dome light; Arnold or V-Ray supplies one
    shape = cmds.shadingNode(LIGHTS.get(kind, "pointLight"), asLight=True,
                             name=valid_name(light.get("name", "light")))
    transform = cmds.listRelatives(shape, parent=True, fullPath=True)
    path = transform[0] if transform else shape

    colour = light.get("color")
    if colour is not None:
        cmds.setAttr(shape + ".color", float(colour[0]), float(colour[1]), float(colour[2]),
                     type="double3")
    # a sun carries Nomad's normalized intensity, everything else its power
    intensity = light.get("intensity" if kind == "SUN" else "power")
    if intensity is not None:
        cmds.setAttr(shape + ".intensity", float(intensity))
    if kind == "SPOT" and "spot_angle" in light:
        import math
        cmds.setAttr(shape + ".coneAngle", math.degrees(float(light["spot_angle"])))
        if "spot_softness" in light:
            cmds.setAttr(shape + ".penumbraAngle",
                         math.degrees(float(light["spot_angle"])) * float(light["spot_softness"]))
    return path


def build_camera(camera):
    """fov_y is vertical, which is what Maya's verticalFilmAperture describes."""
    import math

    transform, shape = cmds.camera(name=valid_name(camera.get("name", "camera")))[:2]
    if camera.get("orthographic"):
        cmds.setAttr(shape + ".orthographic", True)
        cmds.setAttr(shape + ".orthographicWidth", float(camera.get("ortho_scale", 1.0)) * SCALE)
    else:
        aperture = cmds.getAttr(shape + ".verticalFilmAperture") * 25.4  # inches -> mm
        fov = math.radians(float(camera.get("fov_y", 50.0)))
        cmds.setAttr(shape + ".focalLength", (aperture / 2.0) / math.tan(fov / 2.0))
    return cmds.ls(transform, long=True)[0]


# ------------------------------------------------------------------ the scene

def root():
    if not cmds.objExists(ROOT):
        cmds.createNode("transform", name=ROOT)
    return ROOT


def clear():
    """Remove everything this bridge built."""
    if cmds.objExists(ROOT):
        cmds.delete(ROOT)
    for shader, group in _shaders.values():
        for node in (shader, group):
            if cmds.objExists(node):
                cmds.delete(node)
    _built.clear()
    _shaders.clear()


def rebuild(revision=None):
    """Bring the Maya scene into line with the cache."""
    global _revision
    link = client()
    if revision is not None and revision == _revision:
        return
    _revision = link.revision

    parent_of = {}
    order = []
    for mesh_id in link.order:
        mesh = link.meshes.get(mesh_id)
        if mesh is not None:
            order.append((mesh_id, mesh))
            parent_of[mesh_id] = mesh.get("parent_id")

    root_path = root()
    for mesh_id, mesh in order:
        existing = _built.get(mesh_id)
        if existing and cmds.objExists(existing):
            if not update_points(existing, mesh):
                cmds.delete(existing)
                existing = None
        if not existing:
            parent = _built.get(parent_of.get(mesh_id)) or root_path
            _built[mesh_id] = build_mesh(mesh, parent=parent)
        apply_transform(_built[mesh_id], mesh, link)
        apply_material(mesh_id, mesh, link)

    for link_id, light in link.lights.items():
        if link_id in _built:
            continue
        path = build_light(light)
        if path:
            _built[link_id] = cmds.parent(path, root_path)[0]
            apply_transform(_built[link_id], light, link)
    for link_id, camera in link.cameras.items():
        if link_id in _built:
            continue
        _built[link_id] = cmds.parent(build_camera(camera), root_path)[0]
        apply_transform(_built[link_id], camera, link)

    for mesh_id, path in list(_built.items()):
        if mesh_id not in link.meshes and cmds.objExists(path):
            cmds.delete(path)
            _built.pop(mesh_id, None)


def apply_material(mesh_id, mesh, link):
    """One shader per Nomad material; instances share the original's."""
    key = mesh_id if mesh_id in link.materials else mesh.get("material_source")
    if key not in link.materials:
        return
    if key not in _shaders:
        _shaders[key] = materials.build(
            link.materials[key], name=valid_name(mesh["name"]) + "_mat")
    materials.assign(_shaders[key][1], _built[mesh_id])


def apply_transform(path, mesh, link):
    """Place the object, using the local transform when it belongs to our parent."""
    world = mesh.get("world_matrix", convert.IDENTITY)
    parent_id = mesh.get("parent_id")
    parent = link.meshes.get(parent_id) if parent_id else None
    local = world
    if parent is not None:
        parent_world = parent.get("world_matrix", convert.IDENTITY)
        frame = mesh.get("world_matrix_parent")
        pair = mesh.get("local_matrix")
        if pair is not None and frame is not None and convert.matrices_close(frame, parent_world):
            local = pair
        else:
            local = convert.compose_local(world, parent_world)
    try:
        transform = OpenMaya.MFnTransform(dag_path(path))
        transform.setTransformation(OpenMaya.MTransformationMatrix(
            OpenMaya.MMatrix(matrix_values(local, SCALE))))
    except Exception:
        pass
    cmds.setAttr(path + ".visibility", bool(mesh.get("visible", True)))


_watchers = []


def on_status(callback):
    """Register something to be told when the link's state changes."""
    if callback not in _watchers:
        _watchers.append(callback)


def status_changed():
    """Called every pump, so this stays cheap: the window redraws one label."""
    for callback in list(_watchers):
        try:
            callback()
        except Exception:
            _watchers.remove(callback)
