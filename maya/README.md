# Nomad Link for Maya

Reads a [Nomad Sculpt](https://nomadsculpt.com) scene into Maya over
[Nomad Link](https://github.com/stephomi/nomad-link), including live sculpt
updates. Import only for now: nothing is sent back to Nomad.

Needs Nomad 0.11.37 or newer for hierarchy and visibility; older builds send a
flat scene.

## Install

1. Put this folder somewhere permanent, say `~/nomad-link-maya`.
2. Point Maya's Python at it. Either add to `Maya.env`:

   ```
   PYTHONPATH = /Users/you/nomad-link-maya/python
   ```

   or, from the script editor, once per session:

   ```python
   import sys; sys.path.append("/Users/you/nomad-link-maya/python")
   ```

3. Then:

   ```python
   import nomad_link
   nomad_link.ui()          # a small window
   ```

   or drive it directly:

   ```python
   nomad_link.connect()     # empty = discover Nomad on the network
   nomad_link.get_scene()
   nomad_link.report()
   ```

**numpy is required.** The codecs are numpy throughout. Check with
`import numpy` in the script editor; if it is missing, install it into Maya's
Python (`mayapy -m pip install numpy`).

## Reloading after an update

Maya caches imported modules, so a new build does not take effect until the old
one is purged (or Maya restarts). `importlib.reload` is not enough -- it does
not touch submodules:

```python
import sys
for name in [n for n in sys.modules if n == "nomad_link" or n.startswith("nomad_link.")]:
    del sys.modules[name]
import nomad_link
```

Unzipping over an older copy also leaves behind files that no longer exist in
the new one. If something behaves like an older version, look for strays in
`python/nomad_link/`.

## What arrives

| Nomad | Maya |
|---|---|
| meshes | polygon meshes, quads and n-gons kept |
| hierarchy | DAG parenting, under a `nomad` group |
| visibility | the transform's `visibility` |
| UVs | the default uv set, v flipped to Maya's origin |
| vertex colour and opacity | a `nomad` colour set, alpha included |
| sculpt layers | applied, so a posed character arrives posed |
| live strokes | patched onto the existing mesh, not rebuilt |
| materials | `standardSurface`, one per Nomad material, shared by instances |
| lights | point, directional, spot and area |
| cameras | with `fov_y` as a focal length |

Not yet: textures, the environment (Maya has no native dome light), and sending
anything back to Nomad.

### Materials

`standardSurface` is Autodesk's version of the model OpenPBR describes, so the
mapping is the Houdini bridge's with the names changed. The attribute names sit
in a table per surface type in `materials.py`, so another renderer -- VRayMtl,
for instance -- is a table and a node name rather than a rewrite.

With Arnold loaded the bridge builds `aiStandardSurface` instead, which is the
same model and the same attribute names. That is not a preference: the vertex
colour readers are Arnold nodes, and feeding one into a non-Arnold shader is a
hybrid mtoa need not translate. Pin it with `nomad_link.materials.SURFACE =
"standardSurface"` if you want Maya's own.

Two things worth knowing:

- **Vertex paint renders, but does not preview.** It is the other way round
  from what you might expect. Maya has no native node that reads a colour set
  into a shader, so this uses the renderer's -- `aiUserDataColor` for Arnold,
  `VRayVertexColors` for V-Ray -- and **Viewport 2.0 does not evaluate those**:
  the base colour falls back to its default until you render or open IPR. To
  see the paint in the viewport, display the colour set directly instead
  (Display > Polygons > Color Display).

  Each painted channel rides in its own colour set, since a colour set is the
  only per-vertex channel a Maya shader can read: `nomad` carries colour and
  alpha, `nomad_rough`, `nomad_metallic`, `nomad_mask` and `nomad_density` carry
  a scalar as grey. Colour, roughness and metalness are wired to the shader;
  mask and density are carried but not connected, as in Houdini.

  mtoa also ignores colour sets unless the shape has `aiExportColors` set, which
  the bridge does when it writes them.
- Nomad's `subsurface_color` is the colour of light bleeding through, not a
  scattering albedo. It drives the scatter radius per channel; the albedo
  follows the surface colour. Using it as an albedo turns skin into red wax.

## Conventions

- **Winding is not flipped.** Maya is counter-clockwise front-facing, the same
  as glTF and therefore Nomad. (The Houdini bridge does flip -- Houdini is
  clockwise.)
- **v is flipped**, since Maya's uv origin is bottom-left and Nomad's is top-left.
- **Units.** Maya is centimetres by default and Nomad's are arbitrary. `SCALE`
  in `scene.py` multiplies incoming positions and defaults to 1.0, which makes a
  two-unit character two centimetres tall. Geometry does not care, but anything
  calibrated to real scale does: Arnold's subsurface radius and physically based
  lights both assume centimetres. `nomad_link.scene.SCALE = 100.0` treats a Nomad
  unit as a metre, which may behave better -- set it before pulling a scene.
- **Subsurface weight** is scaled by `materials.SUBSURFACE_WEIGHT`, 0.05, matched
  by eye against an Arnold render. Arnold's subsurface is far stronger than
  Nomad's at the same weight -- strong enough at 0.5 to bury the vertex paint
  completely. The Houdini bridge wants 0.5 for OpenPBR in Karma, so it is a
  per-renderer number rather than anything about Nomad.
- **Matrices** are row-major with row vectors, like Houdini and USD, so Nomad's
  column-major list transfers unchanged.

## Transfers

Two things stall a scene transfer, both measured against Nomad 2.9.25 while
building the Houdini bridge:

- **Another client connected.** Peers share Nomad's sender and a lingering one
  holds up everybody. `nomad_link.report()` lists them under `other peers`;
  restarting Nomad's Link host clears them.
- **Anything sent mid-transfer**, which makes Nomad restart from the beginning.
  This bridge holds its own requests until the link is quiet.

## Tests

```
python3 tests/run_all.py
```

Neither module needs Maya: `test_link.py` drives the client against a mock
Nomad, and `test_scene.py` builds the scene against a fake `maya` module that
mimics the shapes of the real API. That catches the array bookkeeping -- winding,
uv flip, hierarchy, transforms, live point updates -- but not Maya's own
behaviour, so **the first run in Maya is still the real test**.

`transport.py`, `convert.py` and `client.py` are shared with the Houdini bridge
and should be kept in sync.
