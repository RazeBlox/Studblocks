import importlib.util
import os
import sys
from pathlib import Path

import bpy
from mathutils import Vector


def parse_args() -> tuple[Path, Path, Path]:
    if "--" not in sys.argv:
        raise SystemExit("Usage: blender --background --python optimize_glb.py -- input.glb cleaned.glb addon.py")

    args = sys.argv[sys.argv.index("--") + 1 :]
    if len(args) < 3:
        raise SystemExit("Usage: blender --background --python optimize_glb.py -- input.glb cleaned.glb addon.py")

    input_path = Path(args[0]).resolve()
    output_path = Path(args[1]).resolve()
    addon_path = Path(args[2]).resolve()
    if not input_path.exists():
        raise SystemExit(f"Input GLB not found: {input_path}")
    if not addon_path.exists():
        raise SystemExit(f"HiddenGeometryRemoval add-on not found: {addon_path}")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    return input_path, output_path, addon_path


def clear_scene() -> None:
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)


def load_addon(addon_path: Path):
    spec = importlib.util.spec_from_file_location("HiddenGeometryRemoval", addon_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load add-on from {addon_path}")

    module = importlib.util.module_from_spec(spec)
    sys.modules["HiddenGeometryRemoval"] = module
    spec.loader.exec_module(module)
    if hasattr(module, "register"):
        module.register()
    return module


def mesh_objects():
    return [obj for obj in bpy.context.scene.objects if obj.type == "MESH"]


def select_mesh_objects(objects) -> None:
    bpy.ops.object.select_all(action="DESELECT")
    for obj in objects:
        obj.select_set(True)
    bpy.context.view_layer.objects.active = objects[0]


def compute_camera_distance(objects) -> float:
    max_radius = 1.0
    for obj in objects:
        for corner in obj.bound_box:
            world_corner = obj.matrix_world @ Vector(corner)
            max_radius = max(max_radius, world_corner.length)
    return max_radius * 2.5


def env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    return max(minimum, min(maximum, int(raw)))


def env_float(name: str, default: float, minimum: float, maximum: float) -> float:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    value = float(raw)
    return max(minimum, min(maximum, value))


def configure_hidden_removal(objects) -> None:
    props = bpy.context.scene.hidden_removal_props
    props.rows = env_int("ROBLOX_MESH_OPTIMIZER_HGR_ROWS", 6, 2, 12)
    props.cameras_per_row = env_int("ROBLOX_MESH_OPTIMIZER_HGR_CAMERAS_PER_ROW", 6, 2, 12)
    if props.cameras_per_row % 2 != 0:
        props.cameras_per_row += 1
    props.sphere_radius = compute_camera_distance(objects)
    props.delete_select_mode = "DELETE"
    precision_mode = os.environ.get("ROBLOX_MESH_OPTIMIZER_HGR_PRECISION", "HIGH").strip().upper() or "HIGH"
    props.precision_mode = precision_mode if precision_mode in {"HIGH", "LOW"} else "HIGH"
    props.keep_cameras = False
    props.experimental = False
    props.sampling_ratio = env_int("ROBLOX_MESH_OPTIMIZER_HGR_SAMPLING_RATIO", 30, 1, 100)
    props.flatness_angle = env_float("ROBLOX_MESH_OPTIMIZER_HGR_FLATNESS_ANGLE", 30.0, 10.0, 90.0)
    props.merge_meshes = False
    props.merge_by_distance = True


def run_hidden_removal_per_object(objects) -> None:
    for obj in objects:
        if obj.type != "MESH":
            continue
        select_mesh_objects([obj])
        configure_hidden_removal([obj])
        result = bpy.ops.object.hidden_geometry_removal()
        if "FINISHED" not in result:
            raise RuntimeError(f"HiddenGeometryRemoval did not finish successfully for {obj.name}")


def export_cleaned_glb(output_path: Path) -> None:
    objects = mesh_objects()
    if not objects:
        raise RuntimeError("No mesh objects remain after hidden geometry cleanup")

    select_mesh_objects(objects)
    result = bpy.ops.export_scene.gltf(
        filepath=str(output_path),
        export_format="GLB",
        use_selection=True,
        check_existing=False,
    )
    if "FINISHED" not in result:
        raise RuntimeError("Blender failed to export the cleaned GLB")


def main() -> None:
    input_path, output_path, addon_path = parse_args()
    clear_scene()
    load_addon(addon_path)

    result = bpy.ops.import_scene.gltf(filepath=str(input_path))
    if "FINISHED" not in result:
        raise RuntimeError(f"Blender failed to import {input_path}")

    objects = mesh_objects()
    if not objects:
        raise RuntimeError("The imported GLB did not create any mesh objects")

    run_hidden_removal_per_object(objects)

    export_cleaned_glb(output_path)


if __name__ == "__main__":
    main()
