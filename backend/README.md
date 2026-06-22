# Mesh To Part Backend

Local Windows app for the `Mesh To Part` Roblox plugin.

## What it does

- Hosts `http://127.0.0.1:8790`
- Accepts the plugin payload
- Builds an optimized mesh from block parts
- Removes only fully hidden coplanar faces
- Merges safe coplanar rectangles
- Bakes a stud texture atlas using `dudeax/Roblox-HD-Studs`
- Uploads the result to Roblox Open Cloud as a `Model` asset

## Build

```powershell
cd C:\Users\User\Documents\Playground\mesh_to_part_app
.\build_exe.ps1
```

The packaged executable is written to `dist\Mesh To Part Backend.exe`.
