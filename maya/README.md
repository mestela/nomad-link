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

Not yet: materials, lights, cameras, textures, and sending anything back.

## Conventions

- **Winding is not flipped.** Maya is counter-clockwise front-facing, the same
  as glTF and therefore Nomad. (The Houdini bridge does flip -- Houdini is
  clockwise.)
- **v is flipped**, since Maya's uv origin is bottom-left and Nomad's is top-left.
- **Units.** Maya is centimetres by default and Nomad's are arbitrary; `SCALE`
  in `scene.py` multiplies incoming positions.
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
