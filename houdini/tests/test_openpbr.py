# SPDX-License-Identifier: MIT
"""Nomad materials as MaterialX OpenPBR: hython tests/test_openpbr.py"""
import os
import sys

import numpy
from pxr import Usd, UsdShade

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "python"))

from nomad_link import openpbr, usd  # noqa: E402
from fixtures import Cache, quad_and_tri  # noqa: E402


def check(condition, message):
    if not condition:
        raise AssertionError(message)
    print("ok  " + message)


def nodedefs_exist():
    """Every shader id we author must be a real MaterialX nodedef."""
    import MaterialX
    doc = MaterialX.createDocument()
    MaterialX.loadLibraries(["libraries"], MaterialX.getDefaultDataSearchPath(), doc)
    return doc


doc = nodedefs_exist()
for node_id in (openpbr.SURFACE, openpbr.MULTIPLY_COLOR, openpbr.GEOMPROP_COLOR,
                openpbr.GEOMPROP_FLOAT, openpbr.IMAGE_COLOR, openpbr.IMAGE_FLOAT,
                openpbr.GEOMPROP_UV, "ND_constant_color3"):
    check(doc.getNodeDef(node_id) is not None, "%s is a real nodedef" % node_id)

surface_def = doc.getNodeDef(openpbr.SURFACE)
inputs = {i.getName() for i in surface_def.getInputs()}
for _key, target in openpbr.SCALARS:
    check(target in inputs, "OpenPBR has an input called %s" % target)
for target in ("subsurface_weight", "subsurface_color", "subsurface_radius",
               "transmission_weight", "transmission_color", "transmission_depth",
               "specular_weight", "emission_luminance", "base_color"):
    check(target in inputs, "OpenPBR has an input called %s" % target)

# a painted, tinted, subsurface material: the case UsdPreviewSurface cannot express
cache = Cache()
mesh = quad_and_tri(mesh_id="m1", name="Skin")
cache.add_mesh(mesh)
cache.materials["m1"] = {
    "color": [0.9, 0.6, 0.5], "roughness": 0.42, "metalness": 0.0,
    "material_type": "subsurface", "subsurface_color": [1.0, 0.2, 0.1],
    "subsurface_depth": 0.15, "translucency": True, "translucency_factor": 0.8,
    "reflectance": 0.5, "refraction_ior": 1.4,
}
stage = Usd.Stage.CreateInMemory()
usd.author_scene(stage, cache, material_style="openpbr")
material = UsdShade.Material(stage.GetPrimAtPath("/nomad/Materials/Skin"))
shader = UsdShade.Shader(stage.GetPrimAtPath("/nomad/Materials/Skin/OpenPBR"))
check(bool(material) and bool(shader), "an OpenPBR material was authored")
check(shader.GetIdAttr().Get() == openpbr.SURFACE, "the shader is the OpenPBR surface")
check(bool(material.GetSurfaceOutput("mtlx").GetConnectedSource()),
      "it is connected on the mtlx render context, which is what Karma reads")

check(abs(shader.GetInput("subsurface_weight").Get() - 0.8) < 1e-6,
      "translucency became subsurface_weight")
check(abs(shader.GetInput("subsurface_radius").Get() - 0.15) < 1e-6,
      "subsurface_depth became subsurface_radius")
check(abs(shader.GetInput("specular_ior").Get() - 1.4) < 1e-6, "ior transferred")
check(abs(shader.GetInput("specular_weight").Get() - 1.0) < 1e-6,
      "reflectance 0.5 became specular_weight 1.0")
check(abs(shader.GetInput("specular_roughness").Get() - 0.42) < 1e-6, "roughness transferred")

# the compositing UsdPreviewSurface could not do: tint x vertex paint
source, _name, _kind = shader.GetInput("base_color").GetConnectedSource()
mix = UsdShade.Shader(source.GetPrim())
check(mix.GetIdAttr().Get() == openpbr.MULTIPLY_COLOR,
      "base_color is a multiply, not one source overriding the other")
feeds = [UsdShade.Shader(mix.GetInput(name).GetConnectedSource()[0].GetPrim()).GetIdAttr().Get()
         for name in ("in1", "in2")]
check("ND_constant_color3" in feeds and openpbr.GEOMPROP_COLOR in feeds,
      "the tint and the displayColor primvar are both multiplied in: %s" % feeds)
paint = UsdShade.Shader(stage.GetPrimAtPath("/nomad/Materials/Skin/paint"))
check(paint.GetInput("geomprop").Get() == "displayColor", "the primvar name is right")

# refraction with absorption
glass = Cache()
glass.add_mesh(quad_and_tri(mesh_id="g1", name="Glass"))
glass.materials["g1"] = {
    "material_type": "refraction", "refraction_ior": 1.52,
    "refraction_surface_roughness": 0.05, "absorption_enable": True,
    "absorption_color": [0.2, 0.9, 0.6], "absorption_factor": 4.0,
}
glass_stage = Usd.Stage.CreateInMemory()
usd.author_scene(glass_stage, glass, material_style="openpbr")
gs = UsdShade.Shader(glass_stage.GetPrimAtPath("/nomad/Materials/Glass/OpenPBR"))
check(abs(gs.GetInput("transmission_weight").Get() - 1.0) < 1e-6, "refraction transmits")
check(abs(gs.GetInput("transmission_depth").Get() - 0.25) < 1e-6,
      "absorption_factor 4 became transmission_depth 0.25")
check(abs(gs.GetInput("specular_roughness").Get() - 0.05) < 1e-6,
      "surface roughness drives specular_roughness")

# the preview surface path must still work
preview_stage = Usd.Stage.CreateInMemory()
usd.author_scene(preview_stage, cache, material_style="preview")
check(bool(UsdShade.Shader(preview_stage.GetPrimAtPath("/nomad/Materials/Skin/Preview"))),
      "UsdPreviewSurface is still available as a style")

print("\nall good")
