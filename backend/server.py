import argparse
import base64
import hashlib
import json
import math
import os
import shlex
import shutil
import struct
import subprocess
import threading
import time
import urllib.error
import urllib.request
import uuid
import zlib
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Dict, List, Optional, Tuple

try:
    from PIL import Image
except ImportError:
    Image = None  # type: ignore[assignment]


HOST = "127.0.0.1"
PORT = 8788
OPEN_CLOUD_BASE = "https://apis.roblox.com/assets/v1"
RUNTIME_OPEN_CLOUD_API_KEY: Optional[str] = None
BACKEND_DIR = Path(__file__).resolve().parent
OUT_DIR = BACKEND_DIR / "out"
KEY_FILE_PATH = BACKEND_DIR / "opencloud_key.txt"
FALLBACK_OPEN_CLOUD_API_KEY = "ya3HKER9gkiVAmfkgJXej5MBy2VeupbjU+1H9jmf1yYGne52ZXlKaGJHY2lPaUpTVXpJMU5pSXNJbXRwWkNJNkluTnBaeTB5TURJeExUQTNMVEV6VkRFNE9qVXhPalE1V2lJc0luUjVjQ0k2SWtwWFZDSjkuZXlKaGRXUWlPaUpTYjJKc2IzaEpiblJsY201aGJDSXNJbWx6Y3lJNklrTnNiM1ZrUVhWMGFHVnVkR2xqWVhScGIyNVRaWEoyYVdObElpd2lZbUZ6WlVGd2FVdGxlU0k2SW5saE0waExSVkk1WjJ0cFZrRnRabXRuU2xobGFqVk5Rbmt5Vm1WMWNHSnFWU3N4U0RscWJXWXhlVmxIYm1VMU1pSXNJbTkzYm1WeVNXUWlPaUl5T1RjME1ESXhORFE1SWl3aVpYaHdJam94TnpneE16UXlORFk0TENKcFlYUWlPakUzT0RFek16ZzROamdzSW01aVppSTZNVGM0TVRNek9EZzJPSDAuR2hBNVpvUnM4d3QyR2dJdmRnNFA1OGdtdjRoclNJNXM2N1VFVll5RTF1SWpTbmJfWlBGdWNYX2Y5YWM0dEtuaEJXX3kzREk4c1JTcjN1aHZPbXpDcnlhWEFpTVlVdmkzZlFSLVpJNFJOWkdzTjR5M2lnWDIyV3ZEOHhVa1FYaTRIVkJUVTJmR2djTDN4Y21USDRsOTdKMXVoSDV1b2dsM2stbWpnTzNBZG82QXhQSGRGNVpKaTh5NEk4QzJqbnpmOHZ2NHNkemdmdHVzOWxyZ0xMdUdTOGhCTk1FUGRyZU1XUUlCalhMbXBEdzZrenYzVXVBVDJ5NXJaUlBkck44SnZpYWhxOUk2QjV3dXgyNjVoM0RKaTZjdTBEa0FsUGZ4N1Q2VG4xeTRUYy1uMWxxX3NjRGp3V0Z5SnFGb25LaE1LUU5yeVR1VmVYZDJpS1V4R3JZaU9n"
STUD_TEXTURE_PATH = BACKEND_DIR / "stud_overlay_soft.png"
VENDOR_DIR = BACKEND_DIR / "vendor"
HIDDEN_GEOMETRY_ADDON_PATH = VENDOR_DIR / "HiddenGeometryRemoval.py"
BLENDER_OPTIMIZER_SCRIPT_PATH = BACKEND_DIR / "optimize_glb.py"
DEFAULT_GLTFPACK_ARGS = ("-cc", "-tc")
MAX_AXIS_ALIGNED_CELLS = 250000


@dataclass
class Part:
    name: str
    mesh_group: str
    size: Tuple[float, float, float]
    pos: Tuple[float, float, float]
    rot: Tuple[float, float, float]
    rotation_matrix: Optional[Tuple[Tuple[float, float, float], Tuple[float, float, float], Tuple[float, float, float]]]
    rgb: Tuple[int, int, int]


@dataclass
class Face:
    corners: List[Tuple[float, float, float]]
    mesh_group: str
    rgb: Tuple[int, int, int]
    u_span: float
    v_span: float


@dataclass
class TextureSettings:
    scale: float
    opacity: float


@dataclass
class OptimizerConfig:
    blender_path: Optional[str]
    gltfpack_path: Optional[str]
    gltf_transform_path: Optional[str]
    addon_path: Path
    blender_script_path: Path
    use_gltf_transform: bool
    gltfpack_args: Tuple[str, ...]


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def parse_texture_settings(payload: Dict[str, object]) -> TextureSettings:
    raw = payload.get("texture")
    if not isinstance(raw, dict):
        return TextureSettings(scale=1.25, opacity=0.45)

    scale = clamp(float(raw.get("scale", 1.25)), 0.5, 3.0)
    opacity = clamp(float(raw.get("opacity", 0.45)), 0.0, 5.0)
    return TextureSettings(scale=scale, opacity=opacity)


def parse_env_flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def find_executable(env_name: str, names: Tuple[str, ...], extra_candidates: Optional[List[Path]] = None) -> Optional[str]:
    override = (os.environ.get(env_name) or "").strip()
    if override:
        return override

    for name in names:
        resolved = shutil.which(name)
        if resolved:
            return resolved

    for candidate in extra_candidates or []:
        if candidate.exists():
            return str(candidate)

    return None


def find_blender_executable() -> Optional[str]:
    blender_dir = Path("C:/Program Files/Blender Foundation")
    blender_candidates = sorted(blender_dir.glob("Blender*\\blender.exe"), reverse=True) if blender_dir.exists() else []
    return find_executable(
        "ROBLOX_MESH_OPTIMIZER_BLENDER",
        ("blender", "blender.exe"),
        blender_candidates,
    )


def parse_gltfpack_args() -> Tuple[str, ...]:
    raw = (os.environ.get("ROBLOX_MESH_OPTIMIZER_GLTFPACK_ARGS") or "").strip()
    if not raw:
        return DEFAULT_GLTFPACK_ARGS
    args = tuple(shlex.split(raw))
    return args or DEFAULT_GLTFPACK_ARGS


def get_optimizer_config() -> OptimizerConfig:
    gltf_transform_candidates = [
        Path.home() / "AppData/Roaming/npm/gltf-transform.cmd",
        Path.home() / "AppData/Roaming/npm/gltf-transform.ps1",
    ]
    return OptimizerConfig(
        blender_path=find_blender_executable(),
        gltfpack_path=find_executable("ROBLOX_MESH_OPTIMIZER_GLTFPACK", ("gltfpack", "gltfpack.exe", "gltfpack.cmd")),
        gltf_transform_path=find_executable(
            "ROBLOX_MESH_OPTIMIZER_GLTF_TRANSFORM",
            ("gltf-transform.cmd", "gltf-transform", "gltf-transform.exe"),
            gltf_transform_candidates,
        ),
        addon_path=HIDDEN_GEOMETRY_ADDON_PATH,
        blender_script_path=BLENDER_OPTIMIZER_SCRIPT_PATH,
        use_gltf_transform=parse_env_flag("ROBLOX_MESH_OPTIMIZER_USE_GLTF_TRANSFORM"),
        gltfpack_args=parse_gltfpack_args(),
    )


def get_optimizer_status() -> Dict[str, object]:
    config = get_optimizer_config()
    missing: List[str] = []

    if not config.blender_path:
        missing.append("blender")
    if not config.gltfpack_path:
        missing.append("gltfpack")
    if not config.addon_path.exists():
        missing.append(str(config.addon_path))
    if not config.blender_script_path.exists():
        missing.append(str(config.blender_script_path))
    if config.use_gltf_transform and not config.gltf_transform_path:
        missing.append("gltf-transform")

    return {
        "configured": not missing,
        "missing": missing,
        "blenderPath": config.blender_path,
        "gltfpackPath": config.gltfpack_path,
        "gltfTransformPath": config.gltf_transform_path,
        "useGltfTransform": config.use_gltf_transform,
        "gltfpackArgs": list(config.gltfpack_args),
        "addonPath": str(config.addon_path),
        "blenderScriptPath": str(config.blender_script_path),
    }


def require_optimizer_config() -> OptimizerConfig:
    config = get_optimizer_config()
    missing: List[str] = []
    if not config.blender_path:
        missing.append("blender")
    if not config.gltfpack_path:
        missing.append("gltfpack")
    if not config.addon_path.exists():
        missing.append(str(config.addon_path))
    if not config.blender_script_path.exists():
        missing.append(str(config.blender_script_path))
    if config.use_gltf_transform and not config.gltf_transform_path:
        missing.append("gltf-transform")
    if missing:
        missing_list = ", ".join(missing)
        raise RuntimeError(f"external optimizer pipeline is not configured: missing {missing_list}")
    return config


def rgba_png(width: int, height: int, pixels: bytes) -> bytes:
    stride = width * 4
    raw = bytearray()
    for y in range(height):
        raw.append(0)
        start = y * stride
        raw.extend(pixels[start : start + stride])

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
        )

    header = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    idat = zlib.compress(bytes(raw), level=9)
    return header + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")


def align4(data: bytearray) -> None:
    while len(data) % 4:
        data.append(0)


def deg_to_rad(value: float) -> float:
    return value * math.pi / 180.0


def euler_xyz_matrix(rot: Tuple[float, float, float]) -> Tuple[Tuple[float, float, float], ...]:
    rx, ry, rz = [deg_to_rad(v) for v in rot]
    cx, sx = math.cos(rx), math.sin(rx)
    cy, sy = math.cos(ry), math.sin(ry)
    cz, sz = math.cos(rz), math.sin(rz)

    mx = ((1, 0, 0), (0, cx, -sx), (0, sx, cx))
    my = ((cy, 0, sy), (0, 1, 0), (-sy, 0, cy))
    mz = ((cz, -sz, 0), (sz, cz, 0), (0, 0, 1))
    return matrix_mul(matrix_mul(mx, my), mz)


def parse_rotation_matrix(raw: object) -> Optional[Tuple[Tuple[float, float, float], Tuple[float, float, float], Tuple[float, float, float]]]:
    if not isinstance(raw, list) or len(raw) != 9:
        return None

    values = [float(value) for value in raw]
    return (
        (values[0], values[1], values[2]),
        (values[3], values[4], values[5]),
        (values[6], values[7], values[8]),
    )


def identity_matrix() -> Tuple[Tuple[float, float, float], Tuple[float, float, float], Tuple[float, float, float]]:
    return ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))


def matrix_is_identity(matrix: Tuple[Tuple[float, float, float], ...]) -> bool:
    expected = identity_matrix()
    for row in range(3):
        for col in range(3):
            if abs(matrix[row][col] - expected[row][col]) > 1e-6:
                return False
    return True


def part_rotation_matrix(part: Part) -> Tuple[Tuple[float, float, float], ...]:
    if part.rotation_matrix is not None:
        return part.rotation_matrix
    return euler_xyz_matrix(part.rot)


def matrix_mul(a: Tuple[Tuple[float, float, float], ...], b: Tuple[Tuple[float, float, float], ...]) -> Tuple[Tuple[float, float, float], ...]:
    out = []
    for row in range(3):
        out_row = []
        for col in range(3):
            out_row.append(
                a[row][0] * b[0][col] + a[row][1] * b[1][col] + a[row][2] * b[2][col]
            )
        out.append(tuple(out_row))
    return tuple(out)


def transform_point(local: Tuple[float, float, float], part: Part) -> Tuple[float, float, float]:
    matrix = part_rotation_matrix(part)
    x = matrix[0][0] * local[0] + matrix[0][1] * local[1] + matrix[0][2] * local[2] + part.pos[0]
    y = matrix[1][0] * local[0] + matrix[1][1] * local[1] + matrix[1][2] * local[2] + part.pos[1]
    z = matrix[2][0] * local[0] + matrix[2][1] * local[1] + matrix[2][2] * local[2] + part.pos[2]
    return (x, y, z)


def is_zero_rot(part: Part) -> bool:
    if part.rotation_matrix is not None:
        return matrix_is_identity(part.rotation_matrix)
    return all(abs(v) < 1e-6 for v in part.rot)


def point_inside_axis_aligned_part(point: Tuple[float, float, float], part: Part) -> bool:
    if not is_zero_rot(part):
        return False

    size = tuple(abs(v) for v in part.size)
    for axis in range(3):
        half = size[axis] / 2.0
        if point[axis] < part.pos[axis] - half - 1e-6 or point[axis] > part.pos[axis] + half + 1e-6:
            return False
    return True


def face_is_fully_hidden(part: Part, axis: int, sign: int, parts: List[Part]) -> bool:
    if not is_zero_rot(part):
        return False

    pos = part.pos
    size = tuple(abs(v) for v in part.size)
    axis1 = (axis + 1) % 3
    axis2 = (axis + 2) % 3
    plane = pos[axis] + sign * size[axis] / 2.0
    face_min1 = pos[axis1] - size[axis1] / 2.0
    face_max1 = pos[axis1] + size[axis1] / 2.0
    face_min2 = pos[axis2] - size[axis2] / 2.0
    face_max2 = pos[axis2] + size[axis2] / 2.0

    for other in parts:
        if other is part or not is_zero_rot(other):
            continue

        other_pos = other.pos
        other_size = tuple(abs(v) for v in other.size)
        other_min_axis = other_pos[axis] - other_size[axis] / 2.0
        other_max_axis = other_pos[axis] + other_size[axis] / 2.0

        if sign > 0 and abs(other_min_axis - plane) > 1e-6:
            continue
        if sign < 0 and abs(other_max_axis - plane) > 1e-6:
            continue

        other_min1 = other_pos[axis1] - other_size[axis1] / 2.0
        other_max1 = other_pos[axis1] + other_size[axis1] / 2.0
        other_min2 = other_pos[axis2] - other_size[axis2] / 2.0
        other_max2 = other_pos[axis2] + other_size[axis2] / 2.0

        if (
            abs(other_min1 - face_min1) < 1e-6
            and abs(other_max1 - face_max1) < 1e-6
            and abs(other_min2 - face_min2) < 1e-6
            and abs(other_max2 - face_max2) < 1e-6
        ):
            return True

    return False


def _cell_covered(cx: float, cy: float, rects: List[Tuple[float, float, float, float]]) -> bool:
    for x0, x1, y0, y1 in rects:
        if cx > x0 + 1e-6 and cx < x1 - 1e-6 and cy > y0 + 1e-6 and cy < y1 - 1e-6:
            return True
    return False


def merge_axis_aligned_quads(quads: List[Dict[str, object]]) -> List[Dict[str, object]]:
    groups: Dict[Tuple[int, int, float, str, Tuple[int, int, int]], List[Tuple[float, float, float, float]]] = {}
    for quad in quads:
        key = (
            int(quad["axis"]),
            int(quad["sign"]),
            round(float(quad["plane"]), 6),
            str(quad["meshGroup"]),
            tuple(int(v) for v in quad["rgb"]),
        )
        groups.setdefault(key, []).append(
            (
                float(quad["x0"]),
                float(quad["x1"]),
                float(quad["y0"]),
                float(quad["y1"]),
            )
        )

    merged: List[Dict[str, object]] = []
    for (axis, sign, plane, mesh_group, rgb), rects in groups.items():
        xs = sorted({value for rect in rects for value in rect[:2]})
        ys = sorted({value for rect in rects for value in rect[2:]})
        if len(xs) < 2 or len(ys) < 2:
            for x0, x1, y0, y1 in rects:
                merged.append(
                    {
                        "axis": axis,
                        "sign": sign,
                        "plane": plane,
                        "x0": x0,
                        "x1": x1,
                        "y0": y0,
                        "y1": y1,
                        "meshGroup": mesh_group,
                        "rgb": rgb,
                    }
                )
            continue

        occupied: List[List[bool]] = []
        for y_index in range(len(ys) - 1):
            row: List[bool] = []
            cy = (ys[y_index] + ys[y_index + 1]) / 2.0
            for x_index in range(len(xs) - 1):
                cx = (xs[x_index] + xs[x_index + 1]) / 2.0
                row.append(_cell_covered(cx, cy, rects))
            occupied.append(row)

        used = [[False for _ in range(len(xs) - 1)] for _ in range(len(ys) - 1)]
        group_merged: List[Dict[str, object]] = []
        for y_index in range(len(ys) - 1):
            for x_index in range(len(xs) - 1):
                if not occupied[y_index][x_index] or used[y_index][x_index]:
                    continue

                width = 1
                while x_index + width < len(xs) - 1 and occupied[y_index][x_index + width] and not used[y_index][x_index + width]:
                    width += 1

                height = 1
                while y_index + height < len(ys) - 1:
                    can_extend = True
                    for test_x in range(x_index, x_index + width):
                        if not occupied[y_index + height][test_x] or used[y_index + height][test_x]:
                            can_extend = False
                            break
                    if not can_extend:
                        break
                    height += 1

                for fill_y in range(y_index, y_index + height):
                    for fill_x in range(x_index, x_index + width):
                        used[fill_y][fill_x] = True

                group_merged.append(
                    {
                        "axis": axis,
                        "sign": sign,
                        "plane": plane,
                        "x0": xs[x_index],
                        "x1": xs[x_index + width],
                        "y0": ys[y_index],
                        "y1": ys[y_index + height],
                        "meshGroup": mesh_group,
                        "rgb": rgb,
                    }
                )

        if len(group_merged) > len(rects):
            for x0, x1, y0, y1 in rects:
                merged.append(
                    {
                        "axis": axis,
                        "sign": sign,
                        "plane": plane,
                        "x0": x0,
                        "x1": x1,
                        "y0": y0,
                        "y1": y1,
                        "meshGroup": mesh_group,
                        "rgb": rgb,
                    }
                )
        else:
            merged.extend(group_merged)

    return merged


def local_face_rect(size: Tuple[float, float, float], axis: int) -> Dict[str, float]:
    if axis == 0:
        return {"x0": -size[1] / 2.0, "x1": size[1] / 2.0, "y0": -size[2] / 2.0, "y1": size[2] / 2.0}
    if axis == 1:
        return {"x0": -size[0] / 2.0, "x1": size[0] / 2.0, "y0": -size[2] / 2.0, "y1": size[2] / 2.0}
    return {"x0": -size[0] / 2.0, "x1": size[0] / 2.0, "y0": -size[1] / 2.0, "y1": size[1] / 2.0}


def face_corners(size: Tuple[float, float, float], axis: int, sign: int, rect: Dict[str, float]) -> List[Tuple[float, float, float]]:
    sx, sy, sz = size[0] / 2.0, size[1] / 2.0, size[2] / 2.0
    if axis == 0 and sign > 0:
        return [(sx, rect["x0"], rect["y0"]), (sx, rect["x1"], rect["y0"]), (sx, rect["x1"], rect["y1"]), (sx, rect["x0"], rect["y1"])]
    if axis == 0:
        return [(-sx, rect["x1"], rect["y0"]), (-sx, rect["x0"], rect["y0"]), (-sx, rect["x0"], rect["y1"]), (-sx, rect["x1"], rect["y1"])]
    if axis == 1 and sign > 0:
        return [(rect["x0"], sy, rect["y0"]), (rect["x1"], sy, rect["y0"]), (rect["x1"], sy, rect["y1"]), (rect["x0"], sy, rect["y1"])]
    if axis == 1:
        return [(rect["x0"], -sy, rect["y1"]), (rect["x1"], -sy, rect["y1"]), (rect["x1"], -sy, rect["y0"]), (rect["x0"], -sy, rect["y0"])]
    if sign > 0:
        return [(rect["x0"], rect["y0"], sz), (rect["x1"], rect["y0"], sz), (rect["x1"], rect["y1"], sz), (rect["x0"], rect["y1"], sz)]
    return [(rect["x1"], rect["y0"], -sz), (rect["x0"], rect["y0"], -sz), (rect["x0"], rect["y1"], -sz), (rect["x1"], rect["y1"], -sz)]


def part_bounds(part: Part) -> Tuple[Tuple[float, float, float], Tuple[float, float, float]]:
    size = tuple(abs(value) for value in part.size)
    return (
        (part.pos[0] - size[0] / 2.0, part.pos[1] - size[1] / 2.0, part.pos[2] - size[2] / 2.0),
        (part.pos[0] + size[0] / 2.0, part.pos[1] + size[1] / 2.0, part.pos[2] + size[2] / 2.0),
    )


def build_axis_aligned_faces_fallback(parts: List[Part]) -> Tuple[List[Face], Dict[str, int]]:
    faces: List[Face] = []
    source_faces = 0
    culled_faces = 0
    axis_aligned_quads: List[Dict[str, object]] = []

    for part in parts:
        size = tuple(abs(v) for v in part.size)
        for axis, sign in ((0, 1), (0, -1), (1, 1), (1, -1), (2, 1), (2, -1)):
            source_faces += 1
            if face_is_fully_hidden(part, axis, sign, parts):
                culled_faces += 1
                continue
            axis1 = (axis + 1) % 3
            axis2 = (axis + 2) % 3
            axis_aligned_quads.append(
                {
                    "axis": axis,
                    "sign": sign,
                    "plane": part.pos[axis] + sign * size[axis] / 2.0,
                    "x0": part.pos[axis1] - size[axis1] / 2.0,
                    "x1": part.pos[axis1] + size[axis1] / 2.0,
                    "y0": part.pos[axis2] - size[axis2] / 2.0,
                    "y1": part.pos[axis2] + size[axis2] / 2.0,
                    "meshGroup": part.mesh_group,
                    "rgb": part.rgb,
                }
            )

    for quad in merge_axis_aligned_quads(axis_aligned_quads):
        axis = int(quad["axis"])
        sign = int(quad["sign"])
        plane = float(quad["plane"])
        axis1 = (axis + 1) % 3
        axis2 = (axis + 2) % 3

        def point(u: float, v: float) -> Tuple[float, float, float]:
            coords = [0.0, 0.0, 0.0]
            coords[axis] = plane
            coords[axis1] = u
            coords[axis2] = v
            return (coords[0], coords[1], coords[2])

        x0 = float(quad["x0"])
        x1 = float(quad["x1"])
        y0 = float(quad["y0"])
        y1 = float(quad["y1"])
        corners = [point(x0, y0), point(x1, y0), point(x1, y1), point(x0, y1)] if sign > 0 else [point(x1, y0), point(x0, y0), point(x0, y1), point(x1, y1)]
        faces.append(
            Face(
                corners=corners,
                mesh_group=str(quad["meshGroup"]),
                rgb=quad["rgb"],  # type: ignore[arg-type]
                u_span=abs(x1 - x0),
                v_span=abs(y1 - y0),
            )
        )

    shell_faces = len(axis_aligned_quads)
    output_faces = len(faces)
    return faces, {
        "sourceFaces": source_faces,
        "outputFaces": output_faces,
        "fullyCulledFaces": culled_faces,
        "mergedFaces": max(0, shell_faces - output_faces),
        "removedFaces": max(0, source_faces - output_faces),
        "shellFaces": shell_faces,
        "usedExactShell": 0,
    }


def face_owner_rgb(
    parts: List[Part],
    axis: int,
    sign: int,
    plane: float,
    u0: float,
    u1: float,
    v0: float,
    v1: float,
) -> Tuple[str, Tuple[int, int, int]]:
    axis1 = (axis + 1) % 3
    axis2 = (axis + 2) % 3
    for part in reversed(parts):
        bounds_min, bounds_max = part_bounds(part)
        boundary = bounds_max[axis] if sign > 0 else bounds_min[axis]
        if abs(boundary - plane) > 1e-6:
            continue
        if bounds_min[axis1] <= u0 + 1e-6 and bounds_max[axis1] >= u1 - 1e-6 and bounds_min[axis2] <= v0 + 1e-6 and bounds_max[axis2] >= v1 - 1e-6:
            return part.mesh_group, part.rgb
    if parts:
        return parts[-1].mesh_group, parts[-1].rgb
    return "Mesh", (255, 255, 255)


def has_safe_exact_shell_overlap(parts: List[Part]) -> bool:
    epsilon = 1e-6
    found_safe_overlap = False
    bounds = [part_bounds(part) for part in parts]

    for index, first in enumerate(parts):
        first_min, first_max = bounds[index]
        first_volume = max(epsilon, (first_max[0] - first_min[0]) * (first_max[1] - first_min[1]) * (first_max[2] - first_min[2]))
        for other_index in range(index + 1, len(parts)):
            second = parts[other_index]
            second_min, second_max = bounds[other_index]
            overlap_x = min(first_max[0], second_max[0]) - max(first_min[0], second_min[0])
            overlap_y = min(first_max[1], second_max[1]) - max(first_min[1], second_min[1])
            overlap_z = min(first_max[2], second_max[2]) - max(first_min[2], second_min[2])
            if overlap_x <= epsilon or overlap_y <= epsilon or overlap_z <= epsilon:
                continue

            second_volume = max(epsilon, (second_max[0] - second_min[0]) * (second_max[1] - second_min[1]) * (second_max[2] - second_min[2]))
            overlap_volume = overlap_x * overlap_y * overlap_z
            smaller_volume = min(first_volume, second_volume)
            coverage = overlap_volume / smaller_volume

            if coverage >= 0.999:
                found_safe_overlap = True
                continue

            return False

    return found_safe_overlap


def build_axis_aligned_union_faces(parts: List[Part]) -> Tuple[List[Face], Dict[str, int]]:
    if not parts:
        return [], {
            "sourceFaces": 0,
            "outputFaces": 0,
            "fullyCulledFaces": 0,
            "mergedFaces": 0,
            "removedFaces": 0,
            "shellFaces": 0,
            "usedExactShell": 1,
        }

    bounds = [part_bounds(part) for part in parts]
    xs = sorted({value for bounds_min, bounds_max in bounds for value in (bounds_min[0], bounds_max[0])})
    ys = sorted({value for bounds_min, bounds_max in bounds for value in (bounds_min[1], bounds_max[1])})
    zs = sorted({value for bounds_min, bounds_max in bounds for value in (bounds_min[2], bounds_max[2])})
    nx = len(xs) - 1
    ny = len(ys) - 1
    nz = len(zs) - 1

    if nx <= 0 or ny <= 0 or nz <= 0 or nx * ny * nz > MAX_AXIS_ALIGNED_CELLS:
        return build_axis_aligned_faces_fallback(parts)

    x_index = {value: index for index, value in enumerate(xs)}
    y_index = {value: index for index, value in enumerate(ys)}
    z_index = {value: index for index, value in enumerate(zs)}
    occupied = [False] * (nx * ny * nz)

    def cell_index(ix: int, iy: int, iz: int) -> int:
        return ix + nx * (iy + ny * iz)

    for bounds_min, bounds_max in bounds:
        ix0, ix1 = x_index[bounds_min[0]], x_index[bounds_max[0]]
        iy0, iy1 = y_index[bounds_min[1]], y_index[bounds_max[1]]
        iz0, iz1 = z_index[bounds_min[2]], z_index[bounds_max[2]]
        for ix in range(ix0, ix1):
            for iy in range(iy0, iy1):
                for iz in range(iz0, iz1):
                    occupied[cell_index(ix, iy, iz)] = True

    quads: List[Dict[str, object]] = []
    for ix in range(nx):
        for iy in range(ny):
            for iz in range(nz):
                if not occupied[cell_index(ix, iy, iz)]:
                    continue

                neighbor_checks = (
                    (0, 1, ix == nx - 1 or not occupied[cell_index(ix + 1, iy, iz)]),
                    (0, -1, ix == 0 or not occupied[cell_index(ix - 1, iy, iz)]),
                    (1, 1, iy == ny - 1 or not occupied[cell_index(ix, iy + 1, iz)]),
                    (1, -1, iy == 0 or not occupied[cell_index(ix, iy - 1, iz)]),
                    (2, 1, iz == nz - 1 or not occupied[cell_index(ix, iy, iz + 1)]),
                    (2, -1, iz == 0 or not occupied[cell_index(ix, iy, iz - 1)]),
                )

                for axis, sign, is_boundary in neighbor_checks:
                    if not is_boundary:
                        continue

                    if axis == 0:
                        plane = xs[ix + 1] if sign > 0 else xs[ix]
                        u0, u1 = ys[iy], ys[iy + 1]
                        v0, v1 = zs[iz], zs[iz + 1]
                    elif axis == 1:
                        plane = ys[iy + 1] if sign > 0 else ys[iy]
                        u0, u1 = xs[ix], xs[ix + 1]
                        v0, v1 = zs[iz], zs[iz + 1]
                    else:
                        plane = zs[iz + 1] if sign > 0 else zs[iz]
                        u0, u1 = xs[ix], xs[ix + 1]
                        v0, v1 = ys[iy], ys[iy + 1]

                    mesh_group, rgb = face_owner_rgb(parts, axis, sign, plane, u0, u1, v0, v1)
                    quads.append(
                        {
                            "axis": axis,
                            "sign": sign,
                            "plane": plane,
                            "x0": u0,
                            "x1": u1,
                            "y0": v0,
                            "y1": v1,
                            "meshGroup": mesh_group,
                            "rgb": rgb,
                        }
                    )

    shell_faces = len(quads)
    faces: List[Face] = []
    for quad in merge_axis_aligned_quads(quads):
        axis = int(quad["axis"])
        sign = int(quad["sign"])
        plane = float(quad["plane"])
        axis1 = (axis + 1) % 3
        axis2 = (axis + 2) % 3

        def point(u: float, v: float) -> Tuple[float, float, float]:
            coords = [0.0, 0.0, 0.0]
            coords[axis] = plane
            coords[axis1] = u
            coords[axis2] = v
            return (coords[0], coords[1], coords[2])

        x0 = float(quad["x0"])
        x1 = float(quad["x1"])
        y0 = float(quad["y0"])
        y1 = float(quad["y1"])
        corners = [point(x0, y0), point(x1, y0), point(x1, y1), point(x0, y1)] if sign > 0 else [point(x1, y0), point(x0, y0), point(x0, y1), point(x1, y1)]
        faces.append(
            Face(
                corners=corners,
                mesh_group=str(quad["meshGroup"]),
                rgb=quad["rgb"],  # type: ignore[arg-type]
                u_span=abs(x1 - x0),
                v_span=abs(y1 - y0),
            )
        )

    source_faces = len(parts) * 6
    output_faces = len(faces)
    return faces, {
        "sourceFaces": source_faces,
        "outputFaces": output_faces,
        "fullyCulledFaces": max(0, source_faces - shell_faces),
        "mergedFaces": max(0, shell_faces - output_faces),
        "removedFaces": max(0, source_faces - output_faces),
        "shellFaces": shell_faces,
        "usedExactShell": 1,
    }


def build_faces(parts: List[Part]) -> Tuple[List[Face], Dict[str, int]]:
    axis_aligned_parts = [part for part in parts if is_zero_rot(part)]
    rotated_parts = [part for part in parts if not is_zero_rot(part)]

    if has_safe_exact_shell_overlap(axis_aligned_parts):
        faces, stats = build_axis_aligned_union_faces(axis_aligned_parts)
    else:
        faces, stats = build_axis_aligned_faces_fallback(axis_aligned_parts)
    rotated_source_faces = 0

    for part in rotated_parts:
        size = tuple(abs(v) for v in part.size)
        for axis, sign in ((0, 1), (0, -1), (1, 1), (1, -1), (2, 1), (2, -1)):
            rotated_source_faces += 1
            rect = local_face_rect(size, axis)
            corners = face_corners(size, axis, sign, rect)
            world = [transform_point(corner, part) for corner in corners]
            faces.append(
                Face(
                    corners=world,
                    mesh_group=part.mesh_group,
                    rgb=part.rgb,
                    u_span=abs(rect["x1"] - rect["x0"]),
                    v_span=abs(rect["y1"] - rect["y0"]),
                )
            )

    stats["sourceFaces"] += rotated_source_faces
    stats["outputFaces"] = len(faces)
    stats["removedFaces"] = max(0, stats["sourceFaces"] - stats["outputFaces"])
    return faces, stats

def get_texture_png(texture_settings: TextureSettings) -> bytes:
    if texture_settings.opacity <= 0.01:
        return rgba_png(4, 4, bytes([255, 255, 255, 255] * 16))

    if STUD_TEXTURE_PATH.exists():
        if Image is not None:
            image = Image.open(STUD_TEXTURE_PATH).convert("RGBA")
            pixels = bytearray(image.tobytes())
            fade = clamp(texture_settings.opacity, 0.0, 5.0)
            for index in range(0, len(pixels), 4):
                for color_offset in range(3):
                    original = pixels[index + color_offset]
                    value = 255 - (255 - original) * fade
                    pixels[index + color_offset] = int(round(clamp(value, 0.0, 255.0)))
                pixels[index + 3] = 255
            return rgba_png(image.width, image.height, bytes(pixels))
        return STUD_TEXTURE_PATH.read_bytes()

    return rgba_png(1, 1, bytes([255, 255, 255, 255]))


def build_glb(name: str, faces: List[Face], texture_settings: TextureSettings) -> bytes:
    texture_png = get_texture_png(texture_settings)
    texture_hash = hashlib.sha1(texture_png).hexdigest()[:10]
    mesh_order: List[str] = []
    face_groups: Dict[str, List[Face]] = {}
    for face in faces:
        if face.mesh_group not in face_groups:
            mesh_order.append(face.mesh_group)
            face_groups[face.mesh_group] = []
        face_groups[face.mesh_group].append(face)

    binary = bytearray()
    buffer_views: List[Dict[str, object]] = []
    accessors: List[Dict[str, object]] = []
    meshes: List[Dict[str, object]] = []
    nodes: List[Dict[str, object]] = []

    def append_buffer_view(data: bytearray, target: Optional[int] = None) -> int:
        offset = len(binary)
        binary.extend(data)
        align4(binary)
        buffer_view: Dict[str, object] = {"buffer": 0, "byteOffset": offset, "byteLength": len(data)}
        if target is not None:
            buffer_view["target"] = target
        buffer_views.append(buffer_view)
        return len(buffer_views) - 1

    def append_accessor(buffer_view: int, component_type: int, count: int, accessor_type: str, min_value: Optional[List[float]] = None, max_value: Optional[List[float]] = None) -> int:
        accessor: Dict[str, object] = {
            "bufferView": buffer_view,
            "componentType": component_type,
            "count": count,
            "type": accessor_type,
        }
        if min_value is not None:
            accessor["min"] = min_value
        if max_value is not None:
            accessor["max"] = max_value
        accessors.append(accessor)
        return len(accessors) - 1

    for mesh_group in mesh_order:
        group_faces = face_groups[mesh_group]
        positions = bytearray()
        texcoords = bytearray()
        colors = bytearray()
        indices = bytearray()
        pos_min = [float("inf"), float("inf"), float("inf")]
        pos_max = [float("-inf"), float("-inf"), float("-inf")]
        vertex_index = 0

        for face in group_faces:
            face_uvs = (
                (0.0, 0.0),
                (max(0.001, face.u_span / texture_settings.scale), 0.0),
                (max(0.001, face.u_span / texture_settings.scale), max(0.001, face.v_span / texture_settings.scale)),
                (0.0, max(0.001, face.v_span / texture_settings.scale)),
            )

            for vertex, uv in zip(face.corners, face_uvs):
                positions.extend(struct.pack("<3f", *vertex))
                texcoords.extend(struct.pack("<2f", *uv))
                colors.extend(struct.pack("<4f", face.rgb[0] / 255.0, face.rgb[1] / 255.0, face.rgb[2] / 255.0, 1.0))
                for axis in range(3):
                    pos_min[axis] = min(pos_min[axis], vertex[axis])
                    pos_max[axis] = max(pos_max[axis], vertex[axis])

            indices.extend(struct.pack("<6I", vertex_index, vertex_index + 1, vertex_index + 2, vertex_index, vertex_index + 2, vertex_index + 3))
            vertex_index += 4

        vertex_count = len(group_faces) * 4
        index_count = len(group_faces) * 6
        position_accessor = append_accessor(
            append_buffer_view(positions, 34962),
            5126,
            vertex_count,
            "VEC3",
            pos_min,
            pos_max,
        )
        texcoord_accessor = append_accessor(append_buffer_view(texcoords, 34962), 5126, vertex_count, "VEC2")
        color_accessor = append_accessor(append_buffer_view(colors, 34962), 5126, vertex_count, "VEC4")
        index_accessor = append_accessor(append_buffer_view(indices, 34963), 5125, index_count, "SCALAR")

        mesh_index = len(meshes)
        mesh_name = mesh_group or name
        meshes.append(
            {
                "name": mesh_name,
                "primitives": [
                    {
                        "attributes": {"POSITION": position_accessor, "TEXCOORD_0": texcoord_accessor, "COLOR_0": color_accessor},
                        "indices": index_accessor,
                        "material": 0,
                    }
                ],
            }
        )
        nodes.append({"mesh": mesh_index, "name": mesh_name})

    image_offset = len(binary)
    binary.extend(texture_png)
    align4(binary)
    buffer_views.append({"buffer": 0, "byteOffset": image_offset, "byteLength": len(texture_png)})

    gltf = {
        "asset": {"version": "2.0", "generator": "roblox-mesh-optimizer"},
        "scene": 0,
        "scenes": [{"nodes": list(range(len(nodes)))}],
        "nodes": nodes,
        "meshes": meshes,
        "materials": [
            {
                "name": f"BakedColor_{texture_hash}",
                "doubleSided": True,
                "alphaMode": "BLEND",
                "pbrMetallicRoughness": {
                    "baseColorTexture": {"index": 0},
                    "metallicFactor": 0.0,
                    "roughnessFactor": 1.0,
                },
            }
        ],
        "textures": [{"sampler": 0, "source": 0}],
        "samplers": [{"magFilter": 9729, "minFilter": 9729, "wrapS": 10497, "wrapT": 10497}],
        "images": [{"bufferView": len(buffer_views) - 1, "mimeType": "image/png", "name": f"stud_texture_{texture_hash}"}],
        "buffers": [{"byteLength": len(binary)}],
        "bufferViews": buffer_views,
        "accessors": accessors,
    }

    json_blob = json.dumps(gltf, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    json_padding = (4 - (len(json_blob) % 4)) % 4
    json_blob += b" " * json_padding
    bin_blob = bytes(binary)
    total_length = 12 + 8 + len(json_blob) + 8 + len(bin_blob)

    header = struct.pack("<4sII", b"glTF", 2, total_length)
    json_chunk = struct.pack("<I4s", len(json_blob), b"JSON") + json_blob
    bin_chunk = struct.pack("<I4s", len(bin_blob), b"BIN\x00") + bin_blob
    return header + json_chunk + bin_chunk


def sanitize_filename(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in value.strip())
    return cleaned or "model"


def run_command(args: List[str], cwd: Path, step_name: str, timeout_seconds: int = 900) -> None:
    completed = subprocess.run(
        args,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
    )
    if completed.returncode == 0:
        return

    stderr = (completed.stderr or "").strip()
    stdout = (completed.stdout or "").strip()
    detail = stderr or stdout or f"exit code {completed.returncode}"
    if len(detail) > 4000:
        detail = detail[-4000:]
    raise RuntimeError(f"{step_name} failed: {detail}")


def optimize_glb_bytes(model_name: str, glb_bytes: bytes) -> Tuple[bytes, Dict[str, object]]:
    config = require_optimizer_config()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    jobs_dir = OUT_DIR / "jobs"
    jobs_dir.mkdir(parents=True, exist_ok=True)

    job_name = f"{sanitize_filename(model_name)}_{uuid.uuid4().hex[:10]}"
    job_dir = jobs_dir / job_name
    job_dir.mkdir(parents=True, exist_ok=True)

    source_path = job_dir / "source.glb"
    cleaned_path = job_dir / "cleaned.glb"
    transformed_path = job_dir / "transformed.glb"
    final_path = job_dir / "final.glb"
    source_path.write_bytes(glb_bytes)

    commands: List[str] = []

    blender_command = [
        str(config.blender_path),
        "--background",
        "--python",
        str(config.blender_script_path),
        "--",
        str(source_path),
        str(cleaned_path),
        str(config.addon_path),
    ]
    commands.append(" ".join(blender_command))
    run_command(blender_command, job_dir, "hidden geometry cleanup")

    pipeline_input = cleaned_path

    if config.use_gltf_transform:
        assert config.gltf_transform_path is not None
        gltf_transform_command = [
            str(config.gltf_transform_path),
            "optimize",
            str(cleaned_path),
            str(transformed_path),
            "--texture-compress",
            "webp",
        ]
        commands.append(" ".join(gltf_transform_command))
        run_command(gltf_transform_command, job_dir, "gltf-transform optimize")
        pipeline_input = transformed_path

    gltfpack_command = [str(config.gltfpack_path), "-i", str(pipeline_input), "-o", str(final_path), *config.gltfpack_args]
    commands.append(" ".join(gltfpack_command))
    run_command(gltfpack_command, job_dir, "gltfpack optimize")

    cleaned_bytes = cleaned_path.read_bytes()
    final_bytes = final_path.read_bytes()
    optimization = {
        "jobDir": str(job_dir),
        "sourceGlbBytes": len(glb_bytes),
        "cleanedGlbBytes": len(cleaned_bytes),
        "transformedGlbBytes": transformed_path.stat().st_size if transformed_path.exists() else None,
        "finalGlbBytes": len(final_bytes),
        "uploadGlbBytes": len(cleaned_bytes),
        "uploadArtifact": "cleaned.glb",
        "packedArtifact": "final.glb",
        "usedPackedGlbForUpload": False,
        "uploadReason": "Roblox import keeps the cleaned GLB size and texture intact; gltfpack output currently imports as a tiny MeshPart and drops the texture.",
        "usedGltfTransform": config.use_gltf_transform,
        "gltfpackArgs": list(config.gltfpack_args),
        "commands": commands,
    }
    return cleaned_bytes, optimization


def parse_parts(payload: Dict[str, object]) -> List[Part]:
    raw_parts = payload.get("parts")
    if not isinstance(raw_parts, list) or not raw_parts:
        raise ValueError("payload.parts must be a non-empty array")

    if any(not isinstance(raw, dict) or "meshGroup" not in raw for raw in raw_parts):
        raise ValueError(
            "The Studio plugin is out of date. Reload the latest StudMeshOptimizer plugin source so each direct child Model of the selected root is sent as one mesh group."
        )

    parts: List[Part] = []
    for index, raw in enumerate(raw_parts):
        if not isinstance(raw, dict):
            raise ValueError(f"part #{index + 1} must be an object")

        size = tuple(float(v) for v in raw["size"])
        pos = tuple(float(v) for v in raw["pos"])
        rot = tuple(float(v) for v in raw.get("rot", [0, 0, 0]))
        rotation_matrix = parse_rotation_matrix(raw.get("rotationMatrix"))
        rgb = tuple(int(clamp(float(v), 0, 255)) for v in raw["rgb"])
        parts.append(
            Part(
                name=str(raw.get("name") or f"Part_{index + 1}"),
                mesh_group=str(raw.get("meshGroup") or raw.get("meshGroupName") or f"Mesh_{index + 1}"),
                size=size,
                pos=pos,
                rot=rot,
                rotation_matrix=rotation_matrix,
                rgb=rgb,
            )
        )
    return parts


def make_operation_url(path: str) -> str:
    path = path.strip()
    if path.startswith("http://") or path.startswith("https://"):
        return path
    if path.startswith("/"):
        return f"https://apis.roblox.com{path}"
    if path.startswith("operations/"):
        return f"{OPEN_CLOUD_BASE}/{path}"
    if "/operations/" in path:
        return f"https://apis.roblox.com/{path.lstrip('/')}"
    return f"{OPEN_CLOUD_BASE}/{path}"


def http_json(url: str, method: str = "GET", headers: Optional[Dict[str, str]] = None, data: Optional[bytes] = None) -> Dict[str, object]:
    request = urllib.request.Request(url=url, method=method, headers=headers or {}, data=data)
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            raw = response.read()
            return json.loads(raw.decode("utf-8")) if raw else {}
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"{method} {url} failed with {error.code}: {detail}") from error


def get_open_cloud_api_key() -> str:
    if RUNTIME_OPEN_CLOUD_API_KEY:
        return RUNTIME_OPEN_CLOUD_API_KEY.strip()

    env_key = os.environ.get("ROBLOX_OPEN_CLOUD_API_KEY", "").strip()
    if env_key:
        return env_key

    if KEY_FILE_PATH.exists():
        return KEY_FILE_PATH.read_text(encoding="utf-8").strip()

    return FALLBACK_OPEN_CLOUD_API_KEY.strip()


def encode_multipart(request_json: Dict[str, object], filename: str, file_bytes: bytes, mime_type: str) -> Tuple[bytes, str]:
    boundary = f"----CodexBoundary{uuid.uuid4().hex}"
    lines = []
    lines.append(f"--{boundary}\r\n".encode("utf-8"))
    lines.append(b'Content-Disposition: form-data; name="request"\r\n')
    lines.append(b"Content-Type: application/json\r\n\r\n")
    lines.append(json.dumps(request_json, separators=(",", ":")).encode("utf-8"))
    lines.append(b"\r\n")
    lines.append(f"--{boundary}\r\n".encode("utf-8"))
    lines.append(f'Content-Disposition: form-data; name="fileContent"; filename="{filename}"\r\n'.encode("utf-8"))
    lines.append(f"Content-Type: {mime_type}\r\n\r\n".encode("utf-8"))
    lines.append(file_bytes)
    lines.append(b"\r\n")
    lines.append(f"--{boundary}--\r\n".encode("utf-8"))
    return b"".join(lines), boundary


def upload_model(glb_bytes: bytes, model_name: str, creator: Dict[str, object]) -> Dict[str, object]:
    api_key = get_open_cloud_api_key()
    if not api_key:
        raise RuntimeError("ROBLOX_OPEN_CLOUD_API_KEY is not set")

    creator_type = str(creator.get("type") or "").lower()
    creator_id = str(creator.get("id") or "").strip()
    if creator_type not in {"user", "group"} or not creator_id:
        raise RuntimeError("creator.type must be 'user' or 'group' and creator.id must be set")

    creator_payload = {"userId": creator_id} if creator_type == "user" else {"groupId": creator_id}
    request_json = {
        "assetType": "Model",
        "displayName": model_name[:50],
        "description": "Uploaded by local mesh optimizer backend",
        "creationContext": {"creator": creator_payload},
    }
    body, boundary = encode_multipart(request_json, f"{model_name}.glb", glb_bytes, "model/gltf-binary")
    response = http_json(
        f"{OPEN_CLOUD_BASE}/assets",
        method="POST",
        headers={
            "x-api-key": api_key,
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Accept": "application/json",
        },
        data=body,
    )

    operation_path = str(response.get("path") or response.get("operation") or "")
    if not operation_path:
        return response

    deadline = time.time() + 120
    last_state: Dict[str, object] = response
    while time.time() < deadline:
        time.sleep(2)
        last_state = http_json(
            make_operation_url(operation_path),
            headers={"x-api-key": api_key, "Accept": "application/json"},
        )
        if last_state.get("done") is True:
            break

    if last_state.get("done") is not True:
        raise RuntimeError("asset upload timed out")

    if "error" in last_state:
        raise RuntimeError(json.dumps(last_state["error"], ensure_ascii=True))

    response_payload = last_state.get("response")
    if isinstance(response_payload, dict):
        return response_payload
    return last_state


def optimize_model(payload: Dict[str, object]) -> Dict[str, object]:
    model_name = str(payload.get("modelName") or "OptimizedMesh")
    parts = parse_parts(payload)
    texture_settings = parse_texture_settings(payload)
    faces, stats = build_faces(parts)
    stats["meshGroups"] = len({part.mesh_group for part in parts})
    source_glb_bytes = build_glb(model_name, faces, texture_settings)
    final_glb_bytes, optimization = optimize_glb_bytes(model_name, source_glb_bytes)
    result: Dict[str, object] = {
        "ok": True,
        "modelName": model_name,
        "stats": stats,
        "texture": {
            "scale": texture_settings.scale,
            "opacity": texture_settings.opacity,
            "signature": hashlib.sha1(get_texture_png(texture_settings)).hexdigest()[:10],
        },
        "glbBytes": len(final_glb_bytes),
        "glbBase64": base64.b64encode(final_glb_bytes).decode("ascii"),
        "optimization": optimization,
    }

    if payload.get("upload"):
        upload_result = upload_model(final_glb_bytes, model_name, payload.get("creator") or {})
        result["upload"] = upload_result
        asset_id = None
        if isinstance(upload_result, dict):
            asset_id = upload_result.get("assetId") or upload_result.get("id")
        if asset_id is not None:
            result["assetId"] = int(asset_id)

    return result


class MeshOptimizerHandler(BaseHTTPRequestHandler):
    server_version = "RobloxMeshOptimizer/1.0"

    def _send_json(self, status_code: int, payload: Dict[str, object]) -> None:
        raw = json.dumps(payload, ensure_ascii=True).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        self.end_headers()
        self.wfile.write(raw)

    def do_OPTIONS(self) -> None:
        self._send_json(200, {"ok": True})

    def do_GET(self) -> None:
        if self.path == "/health":
            optimizer_status = get_optimizer_status()
            self._send_json(
                200,
                {
                    "ok": True,
                    "service": "roblox-mesh-optimizer",
                    "uploadConfigured": bool(get_open_cloud_api_key()),
                    "optimizerConfigured": bool(optimizer_status["configured"]),
                    "optimizer": optimizer_status,
                },
            )
            return
        self._send_json(404, {"ok": False, "error": "not found"})

    def do_POST(self) -> None:
        if self.path != "/optimize":
            self._send_json(404, {"ok": False, "error": "not found"})
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            result = optimize_model(payload)
            self._send_json(200, result)
        except Exception as error:
            self._send_json(400, {"ok": False, "error": str(error)})

    def log_message(self, fmt: str, *args: object) -> None:
        print(f"[{self.log_date_time_string()}] {fmt % args}")


def write_self_test() -> None:
    payload = {
        "modelName": "SelfTest",
        "upload": False,
        "parts": [
            {"name": "A", "size": [4, 4, 4], "pos": [0, 0, 0], "rot": [0, 0, 0], "rgb": [255, 0, 0]},
            {"name": "B", "size": [4, 4, 4], "pos": [4, 0, 0], "rot": [0, 0, 0], "rgb": [255, 0, 0]},
        ],
    }
    result = optimize_model(payload)
    output_dir = OUT_DIR
    output_dir.mkdir(parents=True, exist_ok=True)
    glb_bytes = base64.b64decode(result["glbBase64"])
    output_path = output_dir / "selftest.glb"
    output_path.write_bytes(glb_bytes)
    print(
        json.dumps(
            {
                "stats": result["stats"],
                "glbPath": str(output_path),
                "glbBytes": len(glb_bytes),
                "optimization": result.get("optimization"),
            },
            indent=2,
        )
    )


def serve(port: int) -> None:
    server = ThreadingHTTPServer((HOST, port), MeshOptimizerHandler)
    print(f"listening on http://{HOST}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def main() -> None:
    global RUNTIME_OPEN_CLOUD_API_KEY
    parser = argparse.ArgumentParser()
    parser.add_argument("--open-cloud-api-key")
    parser.add_argument("--port", type=int, default=PORT)
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    RUNTIME_OPEN_CLOUD_API_KEY = (args.open_cloud_api_key or "").strip() or None
    if args.self_test:
        write_self_test()
        return
    serve(args.port)


if __name__ == "__main__":
    main()
