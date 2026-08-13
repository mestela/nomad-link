# SPDX-License-Identifier: MIT
"""Just enough `maya.cmds` and `maya.api.OpenMaya` to exercise scene.py.

A test double, not an emulator: it records what the bridge asks Maya to do, so
the array bookkeeping can be checked without a licence. It deliberately mimics
the shapes of the real API -- MPointArray appends, MFnMesh.create taking the
three arrays, MMatrix taking 16 floats -- so a mistake in those shapes shows up
here rather than on someone's machine.
"""
import sys
import types


class MPoint:
    def __init__(self, x=0.0, y=0.0, z=0.0, w=1.0):
        self.x, self.y, self.z, self.w = float(x), float(y), float(z), float(w)

    def __repr__(self):
        return "MPoint(%.3f, %.3f, %.3f)" % (self.x, self.y, self.z)


class MColor:
    def __init__(self, values=(0.0, 0.0, 0.0, 1.0)):
        self.values = tuple(float(v) for v in values)


class _Array(list):
    def __init__(self, values=None):
        # the real API 2.0 arrays take a whole sequence, which is the fast path
        list.__init__(self, values or [])

    def append(self, value):
        list.append(self, value)


class MPointArray(_Array):
    pass


class MIntArray(_Array):
    pass


class MFloatArray(_Array):
    pass


class MColorArray(_Array):
    pass


class MMatrix:
    def __init__(self, values=None):
        self.values = list(values) if values else [1.0 if i % 5 == 0 else 0.0 for i in range(16)]
        if len(self.values) != 16:
            raise ValueError("MMatrix wants 16 floats, got %d" % len(self.values))


class MTransformationMatrix:
    def __init__(self, matrix=None):
        self.matrix = matrix


class MObject:
    def __init__(self, name="node"):
        self.name = name


class MDagPath:
    def __init__(self, name):
        self.name = name


class MSelectionList:
    def __init__(self):
        self.names = []

    def add(self, name):
        if name not in SCENE["nodes"]:
            raise RuntimeError("no object matches name: %s" % name)
        self.names.append(name)

    def getDagPath(self, index):
        return MDagPath(self.names[index])


class MFnTransform:
    def __init__(self, path=None):
        self.path = path

    def create(self, parent=None):
        name = "|transform%d" % len(SCENE["nodes"])
        SCENE["nodes"][name] = {"type": "transform", "parent": parent}
        return MObject(name)

    def setTransformation(self, transformation):
        name = self.path.name if self.path else "?"
        SCENE["transforms"][name] = transformation.matrix.values


class MFnMesh:
    """Holds the mesh data itself, not its name.

    A real function set wraps an MObject, so renaming the node it belongs to is
    invisible to it. Keying by name here would make the fake break where Maya
    would not.
    """

    def __init__(self, path=None):
        self.path = path
        self.data = SCENE["meshes"].get(path.name) if path else None
        self.numVertices = len(self.data["points"]) if self.data else 0

    def create(self, points, counts, connects, parent=None):
        name = parent.name if parent else "|mesh%d" % len(SCENE["meshes"])
        self.data = {"points": list(points), "counts": list(counts),
                     "connects": list(connects)}
        SCENE["meshes"][name] = self.data
        SCENE["nodes"].setdefault(name, {"type": "mesh"})
        self.path = MDagPath(name)
        self.numVertices = len(points)
        return MObject(name)

    def setUVs(self, us, vs):
        self.data["uvs"] = list(zip(list(us), list(vs)))

    def assignUVs(self, counts, ids):
        self.data["uv_ids"] = list(ids)

    def createColorSet(self, name, per_instance):
        self.data["colour_set"] = name

    def setCurrentColorSetName(self, name):
        pass

    def setVertexColors(self, colours, vertices):
        # the real MColorArray holds MColor; built from a sequence its entries
        # are the raw tuples, so accept both
        self.data["colours"] = [getattr(c, "values", c) for c in colours]

    def setPoints(self, points):
        self.data["points"] = list(points)
        SCENE["updates"].append(self.path.name)


class MFnDagNode:
    def __init__(self, obj):
        self.obj = obj

    def setName(self, name):
        entry = SCENE["nodes"].pop(self.obj.name, {"type": "transform"})
        mesh = SCENE["meshes"].pop(self.obj.name, None)
        self.obj.name = "|" + name
        SCENE["nodes"][self.obj.name] = entry
        if mesh is not None:
            SCENE["meshes"][self.obj.name] = mesh
        return self.obj.name

    def fullPathName(self):
        return self.obj.name


class MMessage:
    @staticmethod
    def removeCallback(identifier):
        SCENE["callbacks"].pop(identifier, None)


class MTimerMessage:
    @staticmethod
    def addTimerCallback(period, function):
        identifier = len(SCENE["callbacks"]) + 1
        SCENE["callbacks"][identifier] = (period, function)
        return identifier


SCENE = {"nodes": {}, "meshes": {}, "transforms": {}, "attributes": {},
         "updates": [], "callbacks": {}, "deleted": [], "shading": []}


def reset():
    for value in SCENE.values():
        value.clear()


# ---- maya.cmds

def objExists(name):
    return name in SCENE["nodes"]


def createNode(kind, name=None, **kwargs):
    path = name if name else "%s%d" % (kind, len(SCENE["nodes"]))
    SCENE["nodes"][path] = {"type": kind}
    return path


def parent(child, new_parent, **kwargs):
    SCENE["nodes"].setdefault(child, {})["parent"] = new_parent
    return [child]


def ls(name, **kwargs):
    return [name]


def delete(name):
    SCENE["deleted"].append(name)
    SCENE["nodes"].pop(name, None)
    SCENE["meshes"].pop(name, None)


def setAttr(plug, value, **kwargs):
    SCENE["attributes"][plug] = value


def internalVar(**kwargs):
    return "/tmp/"


def listRelatives(path, **kwargs):
    return [path + "Shape"]


def sets(shapes, **kwargs):
    SCENE["shading"].extend(shapes if isinstance(shapes, list) else [shapes])
    return "initialShadingGroup"


def window(*args, **kwargs):
    return args[0] if args else "window"


def deleteUI(*args, **kwargs):
    return None


def columnLayout(*args, **kwargs):
    return "layout"


def text(*args, **kwargs):
    return args[0] if args else "text"


def textFieldGrp(*args, **kwargs):
    return args[0] if args else "field"


def button(*args, **kwargs):
    return "button"


def separator(*args, **kwargs):
    return "separator"


def showWindow(*args, **kwargs):
    return None


def scriptJob(*args, **kwargs):
    return 1


def install():
    """Put the fakes in sys.modules so `import maya.cmds` picks them up."""
    maya = types.ModuleType("maya")
    cmds = types.ModuleType("maya.cmds")
    for name in ("objExists", "createNode", "parent", "ls", "delete", "setAttr",
                 "internalVar", "listRelatives", "sets", "window", "deleteUI",
                 "columnLayout", "text",
                 "textFieldGrp", "button", "separator", "showWindow", "scriptJob"):
        setattr(cmds, name, globals()[name])
    api = types.ModuleType("maya.api")
    openmaya = types.ModuleType("maya.api.OpenMaya")
    for name in ("MPoint", "MColor", "MPointArray", "MIntArray", "MFloatArray",
                 "MColorArray", "MMatrix", "MTransformationMatrix", "MObject",
                 "MSelectionList", "MFnTransform", "MFnMesh", "MFnDagNode",
                 "MMessage", "MTimerMessage"):
        setattr(openmaya, name, globals()[name])
    maya.cmds = cmds
    maya.api = api
    api.OpenMaya = openmaya
    sys.modules["maya"] = maya
    sys.modules["maya.cmds"] = cmds
    sys.modules["maya.api"] = api
    sys.modules["maya.api.OpenMaya"] = openmaya
    return SCENE
