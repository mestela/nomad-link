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

from . import convert
from .client import client

ROOT = "nomad"
SCALE = 1.0          # multiplies incoming positions; Maya's unit is the centimetre
_built = {}          # link_id -> maya transform path
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

def mesh_arrays(mesh, scale=SCALE):
    """Nomad's arrays -> (points, face counts, face connects) for MFnMesh."""
    positions = mesh["positions"] * scale
    points = OpenMaya.MPointArray()
    for x, y, z in positions.tolist():
        points.append(OpenMaya.MPoint(x, y, z))
    counts = OpenMaya.MIntArray()
    for size in mesh["sizes"].tolist():
        counts.append(int(size))
    connects = OpenMaya.MIntArray()
    for corner in mesh["corners"].tolist():
        connects.append(int(corner))
    return points, counts, connects


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
    return path


def apply_uvs(fn, mesh):
    """Nomad's per-corner uvs, with v flipped to Maya's bottom-left origin."""
    if "texcoords" not in mesh:
        return
    texcoords = mesh["texcoords"]
    us = OpenMaya.MFloatArray()
    vs = OpenMaya.MFloatArray()
    for u, v in texcoords.tolist():
        us.append(float(u))
        vs.append(1.0 - float(v))
    fn.setUVs(us, vs)
    counts = OpenMaya.MIntArray()
    for size in mesh["sizes"].tolist():
        counts.append(int(size))
    ids = OpenMaya.MIntArray()
    for index in mesh["corner_uv"].tolist():
        ids.append(int(index))
    fn.assignUVs(counts, ids)


def apply_colours(fn, mesh):
    """Vertex paint as a colour set, alpha included."""
    if "color" not in mesh:
        return
    colours = OpenMaya.MColorArray()
    alpha = mesh.get("alpha")
    for index, (r, g, b) in enumerate(mesh["color"].tolist()):
        a = float(alpha[index]) if alpha is not None else 1.0
        colours.append(OpenMaya.MColor((float(r), float(g), float(b), a)))
    vertices = OpenMaya.MIntArray()
    for index in range(len(colours)):
        vertices.append(index)
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


# ------------------------------------------------------------------ the scene

def root():
    if not cmds.objExists(ROOT):
        cmds.createNode("transform", name=ROOT)
    return ROOT


def clear():
    """Remove everything this bridge built."""
    if cmds.objExists(ROOT):
        cmds.delete(ROOT)
    _built.clear()


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

    for mesh_id, path in list(_built.items()):
        if mesh_id not in link.meshes and cmds.objExists(path):
            cmds.delete(path)
            _built.pop(mesh_id, None)


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


def status_changed():
    """Called every pump; cheap by design."""
    return
