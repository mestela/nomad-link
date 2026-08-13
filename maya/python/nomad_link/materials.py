# SPDX-License-Identifier: MIT
"""Nomad materials as Maya surface shaders.

standardSurface is Autodesk's version of the same model OpenPBR describes, so
the mapping worked out for the Houdini bridge transfers almost as a rename. The
attribute names live in a table per surface type, so another renderer -- VRayMtl,
aiStandardSurface -- is a table and a node name rather than a rewrite.

The judgement calls, which are the same ones as on the Houdini side:

    reflectance      -> specular weight, doubled (Nomad's 0.5 is the 4% default)
    subsurface_color -> scatter radius tint, NOT the albedo: it is the colour of
                        light bleeding through, and using it as an albedo turns
                        skin into red wax
    subsurface_depth -> absent means Nomad's 0.15, negative means auto and the
                        magnitude is used
    translucency     -> ignored: it defaults to true on every material, so only
                        material_type == subsurface scatters
    additive         -> unlit emission with opacity from the image's luminance
"""
import maya.cmds as cmds

SUBSURFACE_WEIGHT = 0.5   # Nomad's subsurface reads about twice as strong

# surface type -> the attributes this bridge sets
SURFACES = {
    "standardSurface": {
        "node": "standardSurface",
        "base_color": "baseColor",
        "roughness": "specularRoughness",
        "metalness": "metalness",
        "opacity": "opacity",            # a colour on standardSurface
        "opacity_is_colour": True,
        "ior": "specularIOR",
        "specular_weight": "specular",
        "transmission": "transmission",
        "transmission_color": "transmissionColor",
        "transmission_depth": "transmissionDepth",
        "subsurface": "subsurface",
        "subsurface_color": "subsurfaceColor",
        "subsurface_radius": "subsurfaceRadius",
        "emission": "emission",
        "emission_color": "emissionColor",
    },
}
DEFAULT_SURFACE = "standardSurface"

# Nothing reads a colour set into a shader natively, so it depends on what is
# loaded. Each entry is (plugin, node type, the attribute naming the colour set).
COLOUR_READERS = (
    ("mtoa", "aiUserDataColor", "colorAttrName"),
    ("vrayformaya", "VRayVertexColors", "vertexColorSetName"),
)
COLOUR_SET = "nomad"


def colour_reader():
    """A node that reads the vertex colour set, if a renderer supplies one."""
    for plugin, node_type, attribute in COLOUR_READERS:
        try:
            if not cmds.pluginInfo(plugin, query=True, loaded=True):
                continue
        except Exception:
            continue
        try:
            node = cmds.shadingNode(node_type, asUtility=True, name="nomad_vertex_colour")
            cmds.setAttr("%s.%s" % (node, attribute), COLOUR_SET, type="string")
            return node
        except Exception:
            continue
    return None


def surface_table(kind=DEFAULT_SURFACE):
    return SURFACES.get(kind, SURFACES[DEFAULT_SURFACE])


def set_value(node, attribute, value):
    try:
        cmds.setAttr("%s.%s" % (node, attribute), value)
    except Exception:
        pass


def set_colour(node, attribute, rgb):
    try:
        cmds.setAttr("%s.%s" % (node, attribute), float(rgb[0]), float(rgb[1]),
                     float(rgb[2]), type="double3")
    except Exception:
        pass


def build(block, name="nomad_material", kind=DEFAULT_SURFACE, painted=False):
    """Create a shader and its shading group. Returns (shader, shading group)."""
    table = surface_table(kind)
    shader = cmds.shadingNode(table["node"], asShader=True, name=name)
    group = cmds.sets(renderable=True, noSurfaceShader=True, empty=True,
                      name=shader + "SG")
    cmds.connectAttr(shader + ".outColor", group + ".surfaceShader", force=True)
    apply_block(shader, block, kind)
    if painted:
        # Nomad paints per vertex; the base colour is only the tint under it
        reader = colour_reader()
        if reader:
            try:
                cmds.connectAttr(reader + ".outColor",
                                 "%s.%s" % (shader, table["base_color"]), force=True)
            except Exception:
                pass
    return shader, group


def apply_block(shader, block, kind=DEFAULT_SURFACE):
    """Set what Nomad sent. Only edited fields travel, so absent means default."""
    table = surface_table(kind)

    colour = block.get("color")
    if colour is not None:
        set_colour(shader, table["base_color"], colour)
    if "roughness" in block:
        set_value(shader, table["roughness"], float(block["roughness"]))
    if "metalness" in block:
        set_value(shader, table["metalness"], float(block["metalness"]))
    if "opacity" in block:
        value = float(block["opacity"])
        if table.get("opacity_is_colour"):
            set_colour(shader, table["opacity"], (value, value, value))
        else:
            set_value(shader, table["opacity"], value)
    if "refraction_ior" in block:
        set_value(shader, table["ior"], float(block["refraction_ior"]))
    if "reflectance" in block:
        # Nomad's 0.5 is the 4% F0 default, which is a specular weight of 1
        set_value(shader, table["specular_weight"],
                  max(0.0, min(2.0, float(block["reflectance"]) * 2.0)))

    material_type = block.get("material_type")
    if material_type == "refraction":
        set_value(shader, table["transmission"], 1.0)
        if "refraction_surface_roughness" in block:
            set_value(shader, table["roughness"], float(block["refraction_surface_roughness"]))
        if block.get("absorption_enable"):
            set_colour(shader, table["transmission_color"],
                       block.get("absorption_color", (1.0, 1.0, 1.0)))
            factor = float(block.get("absorption_factor", 1.0))
            set_value(shader, table["transmission_depth"], 1.0 / factor if factor > 1e-6 else 0.0)
    elif material_type == "subsurface":
        apply_subsurface(shader, block, table)
    elif material_type == "additive":
        apply_additive(shader, block, table)
    return shader


def apply_subsurface(shader, block, table):
    """Scattering, with the tint where it belongs.

    Nomad's subsurface_color is the colour of light bleeding through; a surface
    shader's is the scattering albedo. Feeding one to the other washes the whole
    surface with it, so the tint drives the per-channel radius instead and the
    albedo is left following the base colour.
    """
    weight = max(0.0, min(1.0, float(block.get("translucency_factor", 1.0))))
    set_value(shader, table["subsurface"], weight * SUBSURFACE_WEIGHT)

    colour = block.get("color")
    if colour is not None:
        set_colour(shader, table["subsurface_color"], colour)

    tint = block.get("subsurface_color")
    depth = abs(float(block.get("subsurface_depth", 0.15))) or 0.15
    if tint is not None:
        values = [max(0.0, float(channel)) for channel in tint[:3]]
        peak = max(values) or 1.0
        set_colour(shader, table["subsurface_radius"],
                   [depth * value / peak for value in values])
    else:
        set_colour(shader, table["subsurface_radius"], (depth, depth, depth))


def apply_additive(shader, block, table):
    """No additive blend exists here either: unlit emission, faded by opacity."""
    set_value(shader, table["specular_weight"], 0.0)
    colour = block.get("color") or (1.0, 1.0, 1.0)
    set_colour(shader, table["base_color"], (0.0, 0.0, 0.0))
    set_colour(shader, table["emission_color"], colour)
    set_value(shader, table["emission"], 1.0)
    opacity = float(block.get("opacity", 1.0))
    luminance = 0.2126 * colour[0] + 0.7152 * colour[1] + 0.0722 * colour[2]
    value = luminance * opacity
    if table.get("opacity_is_colour"):
        set_colour(shader, table["opacity"], (value, value, value))
    else:
        set_value(shader, table["opacity"], value)


def assign(shading_group, paths):
    """One call per group: cmds.sets per mesh is a real cost across hundreds."""
    if isinstance(paths, str):
        paths = [paths]
    try:
        shapes = cmds.listRelatives(list(paths), shapes=True, fullPath=True) or list(paths)
        cmds.sets(shapes, edit=True, forceElement=shading_group)
    except Exception:
        pass
