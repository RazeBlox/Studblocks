# Roblox Mesh Optimizer

This project gives you:

- a local backend at `http://127.0.0.1:8788`
- a Studio plugin source file that sends the selected model to that backend
- a one-click flow that uploads the generated mesh as a Roblox `Model` asset, loads it back into Studio, and replaces the original selection
- a file-based optimization pipeline:
  1. build a source `.glb`
  2. run headless Blender with HiddenGeometryRemoval to delete hidden/internal faces
  3. upload the Blender-cleaned `.glb` to Roblox
  4. run `gltfpack` as a secondary packed artifact for offline inspection
  5. optionally run `gltf-transform optimize` before `gltfpack`

## Current scope

This version is functional for block-part selections:

- supports `Part` instances with `Shape = Block`
- preserves part colors by baking a simple texture atlas
- keeps Roblox submodels as separate mesh nodes instead of flattening the whole selection into one mesh
- removes hidden inner faces with Blender HiddenGeometryRemoval
- still does lightweight face culling/merging before export for simple axis-aligned blocks
- uploads the Blender-cleaned `.glb` as a Roblox `Model` through Open Cloud
- still writes a `gltfpack` artifact, but does not upload it to Roblox because current Roblox import turns that file into a tiny MeshPart and loses the texture

It does not yet support wedges, cylinders, unions, MeshParts, or arbitrary CSG.

## Files

- `backend/server.py`
- `backend/start_backend.bat`
- `plugin/StudMeshOptimizer.lua`

## Backend setup

Set these environment variables before running the backend:

```powershell
$env:ROBLOX_OPEN_CLOUD_API_KEY = "your-key"
$env:ROBLOX_MESH_OPTIMIZER_BLENDER = "C:\Program Files\Blender Foundation\Blender 4.5\blender.exe"
$env:ROBLOX_MESH_OPTIMIZER_GLTFPACK = "C:\path\to\gltfpack.exe"
python C:\Users\user\Documents\Playground\roblox_mesh_optimizer\backend\server.py
```

The backend expects:

- Blender on PATH, in `ROBLOX_MESH_OPTIMIZER_BLENDER`, or in the default `C:\Program Files\Blender Foundation\Blender*\blender.exe` location
- `gltfpack` on PATH or in `ROBLOX_MESH_OPTIMIZER_GLTFPACK`
- the bundled add-on file at `backend/vendor/HiddenGeometryRemoval.py`

Optional:

```powershell
$env:ROBLOX_MESH_OPTIMIZER_USE_GLTF_TRANSFORM = "1"
$env:ROBLOX_MESH_OPTIMIZER_GLTF_TRANSFORM = "C:\Users\user\AppData\Roaming\npm\gltf-transform.cmd"
```

Optional overrides:

```powershell
$env:ROBLOX_MESH_OPTIMIZER_GLTFPACK_ARGS = "-cc -tc"
$env:ROBLOX_MESH_OPTIMIZER_HGR_ROWS = "6"
$env:ROBLOX_MESH_OPTIMIZER_HGR_CAMERAS_PER_ROW = "6"
$env:ROBLOX_MESH_OPTIMIZER_HGR_PRECISION = "HIGH"
```

If you want the optional Node cleanup step:

```powershell
npm install --global @gltf-transform/cli
```

The backend health endpoint now reports both upload readiness and optimizer readiness:

Health check:

```powershell
Invoke-RestMethod http://127.0.0.1:8788/health

# If you see `"uploadConfigured": false`, restart the backend in the same shell
# where ROBLOX_OPEN_CLOUD_API_KEY is set.
# If you see `"optimizerConfigured": false`, install/configure Blender and gltfpack.
```

## Plugin setup

1. Create a new local Studio plugin script.
2. Paste in the contents of `plugin/StudMeshOptimizer.lua`.
3. Enable Studio HTTP requests.
4. Open the widget from the new toolbar button.
5. Fill in:
   - backend URL
   - creator type: `user` or `group`
   - creator ID
6. Select one model made from block parts.
7. Click `Optimize Selected Model`.

## Local test

This writes a sample GLB to `backend/out/selftest.glb`:

```powershell
python C:\Users\user\Documents\Playground\roblox_mesh_optimizer\backend\server.py --self-test
```

The external pipeline keeps intermediate files in `backend/out/jobs/`:

- `source.glb`
- `cleaned.glb` - this is the Roblox upload artifact
- `transformed.glb` when `gltf-transform` is enabled
- `final.glb` - this is the `gltfpack` artifact kept for comparison/debugging
