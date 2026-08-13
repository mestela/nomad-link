# SPDX-License-Identifier: MIT
"""Nomad materials, lights and cameras in Maya: python3 tests/test_materials.py"""
import math
import os
import sys

import numpy

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "python"))

import fake_maya  # noqa: E402

SCENE = fake_maya.install()

from nomad_link import convert, materials, scene  # noqa: E402
from nomad_link.client import client  # noqa: E402


def check(condition, message):
    if not condition:
        raise AssertionError(message)
    print("ok  " + message)


def attr(node, name):
    return SCENE["attributes"].get("%s.%s" % (node, name))


# ---- a plain material
shader, group = materials.build({"color": [0.8, 0.1, 0.1], "roughness": 0.4,
                                 "metalness": 1.0, "refraction_ior": 1.45,
                                 "reflectance": 0.5}, name="plain")
check(SCENE["shaders"][shader]["type"] == "standardSurface", "a standardSurface is created")
check((shader + ".outColor", group + ".surfaceShader") in SCENE["connections"],
      "wired to its shading group")
check(attr(shader, "baseColor") == (0.8, 0.1, 0.1), "base colour")
check(abs(attr(shader, "specularRoughness") - 0.4) < 1e-6, "roughness")
check(abs(attr(shader, "metalness") - 1.0) < 1e-6, "metalness")
check(abs(attr(shader, "specularIOR") - 1.45) < 1e-6, "ior")
check(abs(attr(shader, "specular") - 1.0) < 1e-6,
      "reflectance 0.5 is the 4%% default, so specular weight 1.0")

# ---- subsurface: the tint must not become the albedo
skin, _ = materials.build({"material_type": "subsurface", "color": [0.9, 0.7, 0.6],
                           "subsurface_color": [1.0, 0.2, 0.1],
                           "subsurface_depth": 0.00624, "translucency": True,
                           "translucency_factor": 1.0}, name="skin")
check(abs(attr(skin, "subsurface") - materials.SUBSURFACE_WEIGHT) < 1e-6,
      "scatters at the calibrated weight")
check(attr(skin, "subsurfaceColor") == (0.9, 0.7, 0.6),
      "the scattering albedo follows the surface, not the tint: %s"
      % (attr(skin, "subsurfaceColor"),))
radius = attr(skin, "subsurfaceRadius")
check(abs(radius[0] - 0.00624) < 1e-6 and radius[1] < radius[0] and radius[2] < radius[1],
      "the tint became per-channel scatter distance, red furthest: %s" % (radius,))

opaque, _ = materials.build({"material_type": "opaque", "translucency": True,
                             "translucency_factor": 1.0}, name="opaque")
check(attr(opaque, "subsurface") is None,
      "translucency defaults true on every material, so it does not scatter by itself")

# ---- additive
glow, _ = materials.build({"material_type": "additive", "color": [1.0, 1.0, 1.0],
                           "opacity": 0.5}, name="glow")
check(attr(glow, "emission") == 1.0 and attr(glow, "emissionColor") == (1.0, 1.0, 1.0),
      "additive emits")
check(attr(glow, "baseColor") == (0.0, 0.0, 0.0) and attr(glow, "specular") == 0.0,
      "and is unlit: no diffuse, no specular")
check(abs(attr(glow, "opacity")[0] - 0.5) < 1e-6,
      "opacity is the colour's luminance times the material opacity: %s"
      % (attr(glow, "opacity"),))

# ---- lights and cameras
point = scene.build_light({"name": "Key", "light_type": "POINT", "power": 40.0,
                           "color": [1.0, 0.9, 0.8]})
check(SCENE["nodes"][point.replace("Shape", "")]["type"] == "pointLight"
      or SCENE["nodes"].get(point, {}).get("type") == "pointLight",
      "a POINT light becomes a pointLight: %s" % point)
sun = scene.build_light({"name": "Sun", "light_type": "SUN", "intensity": 3.0})
check(abs(attr(sun + "Shape", "intensity") or attr(sun, "intensity") or 3.0) == 3.0,
      "a SUN carries Nomad's normalized intensity")
spot = scene.build_light({"name": "Spot", "light_type": "SPOT", "power": 2.0,
                          "spot_angle": 1.0, "spot_softness": 0.5})
cone = attr(spot + "Shape", "coneAngle") or attr(spot, "coneAngle")
check(abs(cone - math.degrees(1.0)) < 0.01, "the spot cone is in degrees: %s" % cone)
check(scene.build_light({"light_type": "ENVIRONMENT"}) is None,
      "ENVIRONMENT is skipped: Maya has no native dome light")

camera = scene.build_camera({"name": "Shot", "fov_y": 35.0})
focal = attr(camera + "Shape", "focalLength") or attr(camera, "focalLength")
aperture = 0.9448818897637796 * 25.4
fov = math.degrees(2.0 * math.atan((aperture / 2.0) / focal))
check(abs(fov - 35.0) < 0.1, "fov_y survives the focal length round trip: %.2f" % fov)

# ---- instances share the original's shader
link = client()
link.meshes.clear()
del link.order[:]
link.materials.clear()
scene._built.clear()
scene._shaders.clear()


def mesh(mesh_id, name, **extra):
    header, binary = convert.encode_mesh(
        mesh_id=mesh_id, geometry_id="shared", name=name,
        positions=numpy.zeros((3, 3), "f4"), sizes=numpy.array([3], "i4"),
        corners=numpy.array([0, 1, 2], "i4"), ngon=True)
    header.update(extra)
    return convert.decode_mesh(header, binary)


original = mesh("m1", "Bolt")
copy = mesh("m2", "Bolt Copy")
copy["material_source"] = "m1"
link._store(original)
link._store(copy)
link.materials["m1"] = {"color": [0.2, 0.4, 0.9]}
SCENE["assignments"][:] = []
scene.rebuild(link.revision + 1)
check(len(scene._shaders) == 1, "one shader for the pair, not one each")
groups = {group for _shapes, group in SCENE["assignments"] if group != "initialShadingGroup"}
check(len(groups) == 1, "and both meshes were assigned it: %s" % groups)

# ---- lights and cameras must survive the sweep that removes stale meshes
link.lights["L"] = {"link_id": "L", "name": "Key", "light_type": "POINT", "power": 5.0,
                    "world_matrix": list(convert.IDENTITY)}
link.cameras["C"] = {"link_id": "C", "name": "Shot", "fov_y": 40.0,
                     "world_matrix": list(convert.IDENTITY)}
SCENE["deleted"][:] = []
scene.rebuild(link.revision + 1)
check("L" in scene._built and "C" in scene._built, "the light and camera were built")
scene.rebuild(link.revision + 2)
check("L" in scene._built and "C" in scene._built,
      "and are still there after another rebuild, not swept away as stale meshes")
check(not [d for d in SCENE["deleted"] if "Key" in d or "Shot" in d],
      "neither was deleted: %s" % SCENE["deleted"])

# ---- a painted mesh gets a colour reader when a renderer supplies one
SCENE["plugins"] = ["mtoa"]
scene._shaders.clear()
painted = mesh("m3", "Painted")
painted["color"] = numpy.tile([0.5, 0.5, 0.5], (3, 1))
link._store(painted)
link.materials["m3"] = {"color": [1.0, 1.0, 1.0]}
scene.rebuild(link.revision + 3)
readers = [name for name, node in SCENE["shaders"].items() if node["type"] == "aiUserDataColor"]
check(readers, "a colour reader is created when Arnold is loaded: %s" % readers)
check(any(source.startswith(readers[0]) for source, _dest in SCENE["connections"]),
      "and it drives the shader")
check(any("aiExportColors" in plug for plug in SCENE["attributes"]),
      "the shape is told to export its colour sets, or Arnold never sees them")

# every painted channel Nomad sends should reach the shader, not just colour
scene._shaders.clear()
SCENE["connections"][:] = []
SCENE["shaders"].clear()
full = mesh("m4", "Full")
full["color"] = numpy.tile([0.5, 0.5, 0.5], (3, 1))
full["rough"] = numpy.linspace(0, 1, 3)
full["metallic"] = numpy.linspace(0, 1, 3)
link._store(full)
link.materials["m4"] = {"color": [1.0, 1.0, 1.0]}
scene.rebuild(link.revision + 4)
targets = [dest.split(".")[-1] for _source, dest in SCENE["connections"]]
check("baseColor" in targets, "painted colour drives base colour")
check("specularRoughness" in targets, "painted roughness drives roughness: %s" % targets)
check("metalness" in targets, "painted metalness drives metalness")
sets = [name for name in SCENE["shaders"] if name.startswith("nomad_nomad")]
check(len(sets) >= 3, "one reader per colour set: %s" % sets)

print("\nall good")
