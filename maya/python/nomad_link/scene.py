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
# Multiplies incoming positions. Nomad's units are arbitrary and Maya's are
# centimetres, so 1.0 makes a two-unit character two centimetres tall. That is
# fine for geometry but not for anything calibrated to real scale -- Arnold's
# subsurface radius and physical lights both assume centimetres -- so 100.0
# (a Nomad unit as a metre) may behave better. Set nomad_link.scene.SCALE before
# pulling a scene.
SCALE = 1.0
_built = {}          # link_id -> MObjectHandle for the transform
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
    """MDagPath for a node, or None."""
    try:
        selection = OpenMaya.MSelectionList()
        selection.add(name)
        return selection.getDagPath(0)
    except Exception:
        return None


def handle_of(name_or_object):
    """A handle that survives renaming.

    Maya makes names unique on collision -- a scene with several objects called
    Sphere gets a Sphere1 -- so a stored path goes stale the moment a later
    object claims the name. A handle does not.
    """
    if isinstance(name_or_object, str):
        found = dag_path(name_or_object)
        if found is None:
            return None
        name_or_object = found.node()
    try:
        return OpenMaya.MObjectHandle(name_or_object)
    except Exception:
        return None


def path_of(handle):
    """The current full path for a handle, or None if the node is gone."""
    if handle is None:
        return None
    try:
        if not handle.isValid():
            return None
        return OpenMaya.MFnDagNode(handle.object()).fullPathName() or None
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
    # created under the parent rather than reparented after: cmds.parent plus
    # cmds.ls per object is a real cost across a few hundred
    parent_object = None
    if parent:
        found = dag_path(parent)
        parent_object = found.node() if found is not None else None
    transform = (OpenMaya.MFnTransform().create(parent_object) if parent_object is not None
                 else OpenMaya.MFnTransform().create())
    fn = OpenMaya.MFnMesh()
    fn.create(points, counts, connects, parent=transform)

    dag = OpenMaya.MFnDagNode(transform)
    dag.setName(valid_name(mesh["name"]))  # Maya makes it unique if it has to
    apply_uvs(fn, mesh)
    if apply_colours(fn, mesh):
        export_colours(dag.fullPathName())
    return handle_of(transform)


def export_colours(path):
    """mtoa does not hand colour sets to Arnold unless the shape says so."""
    try:
        for shape in cmds.listRelatives(path, shapes=True, fullPath=True) or []:
            if cmds.attributeQuery("aiExportColors", node=shape, exists=True):
                cmds.setAttr(shape + ".aiExportColors", True)
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


# Nomad paints scalars per vertex, and a colour set is the only per-vertex
# channel a Maya shader can read, so each rides in one as grey.
#   mesh key -> colour set name
PAINT_SETS = (("color", "nomad"), ("rough", "nomad_rough"),
              ("metallic", "nomad_metallic"), ("mask", "nomad_mask"),
              ("density", "nomad_density"))


def colour_array(rgba):
    try:
        return OpenMaya.MColorArray(rgba)
    except (TypeError, ValueError):
        array = OpenMaya.MColorArray()
        for values in rgba:
            array.append(OpenMaya.MColor(values))
        return array


def apply_colours(fn, mesh):
    """Vertex paint as colour sets: colour with its alpha, scalars as grey."""
    import numpy

    written = []
    for key, set_name in PAINT_SETS:
        values = mesh.get(key)
        if values is None:
            continue
        if key == "color":
            alpha = mesh.get("alpha")
            if alpha is None:
                alpha = numpy.ones(len(values), values.dtype)
            rgba = numpy.column_stack((values, alpha))
        else:
            grey = numpy.asarray(values, "f4")
            rgba = numpy.column_stack((grey, grey, grey, numpy.ones(len(grey), "f4")))
        vertices = int_array(numpy.arange(len(rgba), dtype="i4"))
        try:
            fn.createColorSet(set_name, False)
            fn.setCurrentColorSetName(set_name)
            fn.setVertexColors(colour_array(rgba.tolist()), vertices)
            written.append(set_name)
        except Exception:
            continue
    if written:
        try:  # paint the viewport with the colour set, and let Arnold see it
            fn.setCurrentColorSetName(written[0])
        except Exception:
            pass
    return written


def update_points(path, mesh, scale=SCALE):
    """A sculpt stroke moved points: patch the existing mesh rather than rebuild."""
    dag = dag_path(path) if path else None
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
    """Bring the Maya scene into line with the cache.

    Maya redraws and records undo for every node created, which dominates the
    cost across a few hundred objects. Both are restored whatever happens.
    """
    link = client()
    if revision is not None and revision == _revision:
        return
    undo = True
    try:
        cmds.refresh(suspend=True)
        undo = cmds.undoInfo(query=True, state=True)
        cmds.undoInfo(stateWithoutFlush=False)
    except Exception:
        pass
    try:
        _rebuild(link)
    finally:
        try:
            cmds.undoInfo(stateWithoutFlush=undo)
            cmds.refresh(suspend=False)
            cmds.refresh()
        except Exception:
            pass


def _rebuild(link):
    global _revision
    _revision = link.revision

    parent_of = {}
    order = []
    for mesh_id in link.order:
        mesh = link.meshes.get(mesh_id)
        if mesh is not None:
            order.append((mesh_id, mesh))
            parent_of[mesh_id] = mesh.get("parent_id")

    root_path = root()
    pending = {}
    for mesh_id, mesh in order:
        path = path_of(_built.get(mesh_id))
        if path and not update_points(path, mesh):
            cmds.delete(path)   # topology changed: rebuild it
            path = None
            _built.pop(mesh_id, None)
        if not path:
            parent = path_of(_built.get(parent_of.get(mesh_id))) or root_path
            try:
                _built[mesh_id] = build_mesh(mesh, parent=parent)
            except Exception as exc:  # one bad object must not stop the scene
                link.note("could not build %s: %s" % (mesh.get("name", mesh_id), exc))
                _built.pop(mesh_id, None)
                continue
            path = path_of(_built[mesh_id])
        if not path:
            link.note("built %s but cannot find it: the node went somewhere unexpected"
                      % mesh.get("name", mesh_id))
            continue
        apply_transform(path, mesh, link)
        pending.setdefault(material_group(mesh_id, mesh, link), []).append(path)

    for link_id, light in link.lights.items():
        if link_id in _built:
            continue
        built = build_light(light)
        if built:
            _built[link_id] = handle_of(cmds.parent(built, root_path)[0])
            apply_transform(path_of(_built[link_id]), light, link)
    for link_id, camera in link.cameras.items():
        if link_id in _built:
            continue
        _built[link_id] = handle_of(cmds.parent(build_camera(camera), root_path)[0])
        apply_transform(path_of(_built[link_id]), camera, link)

    for group, paths in pending.items():
        materials.assign(group, paths)

    for link_id, handle in list(_built.items()):
        known = (link_id in link.meshes or link_id in link.lights
                 or link_id in link.cameras or link_id in link.groups)
        if known:
            continue
        path = path_of(handle)
        if path:
            cmds.delete(path)
        _built.pop(link_id, None)


def material_group(mesh_id, mesh, link):
    """The shading group this object belongs in; instances share the original's.

    An unassigned mesh draws flat green, so everything gets a group even when
    Nomad sent no material.
    """
    key = mesh_id if mesh_id in link.materials else mesh.get("material_source")
    if key not in link.materials:
        return "initialShadingGroup"
    if key not in _shaders:
        _shaders[key] = materials.build(
            link.materials[key], name=valid_name(mesh["name"]) + "_mat",
            painted=[key for key, _set in PAINT_SETS if key in mesh])
    return _shaders[key][1]


def apply_transform(path, mesh, link):
    """Place the object, using the local transform when it belongs to our parent."""
    if not path:
        return
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
    found = dag_path(path)
    if found is None:
        return
    try:
        OpenMaya.MFnTransform(found).setTransformation(
            OpenMaya.MTransformationMatrix(OpenMaya.MMatrix(matrix_values(local, SCALE))))
    except Exception:
        pass
    try:
        cmds.setAttr(path + ".visibility", bool(mesh.get("visible", True)))
    except Exception:
        pass


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
