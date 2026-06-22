from __future__ import annotations

import json
import math
import threading
import time
import traceback
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import numpy as np
import requests
import trimesh
from PIL import Image

from config import append_log


EPSILON = 1e-4
UPLOAD_URL = "https://apis.roblox.com/assets/v1/assets"
OPERATION_URL = "https://apis.roblox.com/assets/v1/{path}"
TEXTURE_SIGNATURE = "dudeax/Roblox-HD-Studs 4x4 AO Diffuse HD"


class BackendError(Exception):
    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def _normalize(vector: np.ndarray) -> np.ndarray:
    length = float(np.linalg.norm(vector))
    if length <= EPSILON:
        return vector
    return vector / length


def _rect_subtract(source: tuple[float, float, float, float], cut: tuple[float, float, float, float]) -> list[tuple[float, float, float, float]]:
    sx0, sy0, sx1, sy1 = source
    cx0, cy0, cx1, cy1 = cut
    ix0 = max(sx0, cx0)
    iy0 = max(sy0, cy0)
    ix1 = min(sx1, cx1)
    iy1 = min(sy1, cy1)
    if ix1 <= ix0 + EPSILON or iy1 <= iy0 + EPSILON:
        return [source]

    pieces: list[tuple[float, float, float, float]] = []
    if sy0 < iy0 - EPSILON:
        pieces.append((sx0, sy0, sx1, iy0))
    if iy1 < sy1 - EPSILON:
        pieces.append((sx0, iy1, sx1, sy1))
    if sx0 < ix0 - EPSILON:
        pieces.append((sx0, iy0, ix0, iy1))
    if ix1 < sx1 - EPSILON:
        pieces.append((ix1, iy0, sx1, iy1))
    return pieces


@dataclass
class Face:
    normal: np.ndarray
    u_axis: np.ndarray
    v_axis: np.ndarray
    plane_point: np.ndarray
    u_range: tuple[float, float]
    v_range: tuple[float, float]
    color: tuple[int, int, int]

    @property
    def plane_offset(self) -> float:
        return float(np.dot(self.normal, self.plane_point))


@dataclass
class VisiblePatch:
    face: Face
    rect: tuple[float, float, float, float]

    @property
    def width(self) -> float:
        return self.rect[2] - self.rect[0]

    @property
    def height(self) -> float:
        return self.rect[3] - self.rect[1]

    def corners(self) -> list[np.ndarray]:
        u0, v0, u1, v1 = self.rect
        origin = self.face.plane_point
        return [
            origin + self.face.u_axis * u0 + self.face.v_axis * v0,
            origin + self.face.u_axis * u1 + self.face.v_axis * v0,
            origin + self.face.u_axis * u1 + self.face.v_axis * v1,
            origin + self.face.u_axis * u0 + self.face.v_axis * v1,
        ]


class MeshPipeline:
    def __init__(self, texture_path: Path, normal_texture_path: Path) -> None:
        self.texture_tile = Image.open(texture_path).convert("RGBA")
        self.normal_tile = Image.open(normal_texture_path).convert("RGBA")
        self.texture_studs_per_tile = 4.0
        self._material_tile_cache: dict[tuple[tuple[int, int, int], float], tuple[Image.Image, Image.Image]] = {}

    def build(self, payload: dict[str, Any], output_dir: Path) -> tuple[Path, dict[str, Any]]:
        model_name = payload.get("modelName", "")
        append_log(f"Mesh build start: model={model_name!r} parts={len(payload.get('parts', []))}")
        build_started_at = time.perf_counter()

        faces, face_seconds = self._timed("build_faces", lambda: self._build_faces(payload["parts"]))
        source_faces = len(faces)
        append_log(f"Mesh build_faces done: faces={source_faces} seconds={face_seconds:.3f}")

        (visible_patches, culled_faces), cull_seconds = self._timed("cull_hidden_faces", lambda: self._cull_hidden_faces(faces))
        append_log(
            f"Mesh cull_hidden_faces done: visiblePatches={len(visible_patches)} "
            f"culledFaces={culled_faces} seconds={cull_seconds:.3f}"
        )

        (texture_image, normal_image, uv_boxes), atlas_seconds = self._timed(
            "build_atlas",
            lambda: self._build_atlas(visible_patches, payload.get("texture", {})),
        )
        append_log(
            f"Mesh build_atlas done: atlas={texture_image.width}x{texture_image.height} "
            f"uvBoxes={len(uv_boxes)} seconds={atlas_seconds:.3f}"
        )

        glb_path = output_dir / "optimized.glb"
        _, write_seconds = self._timed(
            "write_glb",
            lambda: self._write_glb(glb_path, visible_patches, uv_boxes, texture_image, normal_image),
        )
        append_log(f"Mesh write_glb done: path={glb_path} seconds={write_seconds:.3f}")
        append_log(f"Built GLB at {glb_path} ({glb_path.stat().st_size} bytes)")
        append_log(f"Mesh build complete: totalSeconds={time.perf_counter() - build_started_at:.3f}")

        stats = {
            "sourceFaces": source_faces,
            "outputFaces": len(visible_patches),
            "fullyCulledFaces": culled_faces,
            "mergedFaces": 0,
        }
        texture = {
            "scale": payload.get("texture", {}).get("scale", 1.25),
            "opacity": payload.get("texture", {}).get("opacity", 0.9),
            "signature": TEXTURE_SIGNATURE,
        }
        return glb_path, {"stats": stats, "texture": texture}

    def _timed(self, label: str, callback: Callable[[], Any]) -> tuple[Any, float]:
        append_log(f"Mesh {label} start")
        started_at = time.perf_counter()
        result = callback()
        return result, time.perf_counter() - started_at

    def _build_faces(self, parts: list[dict[str, Any]]) -> list[Face]:
        faces: list[Face] = []
        for part in parts:
            color = tuple(int(channel) for channel in part.get("rgb", (255, 255, 255)))
            size = np.array(part["size"], dtype=float)
            position = np.array(part["pos"], dtype=float)
            basis = part.get("basis")
            if basis:
                rotation = np.array([basis["x"], basis["y"], basis["z"]], dtype=float).T
            else:
                rotation = self._rotation_matrix(part.get("rot", (0.0, 0.0, 0.0)))

            half = size / 2.0
            local_faces = [
                (np.array([1.0, 0.0, 0.0]), np.array([0.0, 0.0, -1.0]), np.array([0.0, 1.0, 0.0]), np.array([half[0], 0.0, 0.0]), (-half[2], half[2]), (-half[1], half[1])),
                (np.array([-1.0, 0.0, 0.0]), np.array([0.0, 0.0, 1.0]), np.array([0.0, 1.0, 0.0]), np.array([-half[0], 0.0, 0.0]), (-half[2], half[2]), (-half[1], half[1])),
                (np.array([0.0, 1.0, 0.0]), np.array([1.0, 0.0, 0.0]), np.array([0.0, 0.0, -1.0]), np.array([0.0, half[1], 0.0]), (-half[0], half[0]), (-half[2], half[2])),
                (np.array([0.0, -1.0, 0.0]), np.array([1.0, 0.0, 0.0]), np.array([0.0, 0.0, 1.0]), np.array([0.0, -half[1], 0.0]), (-half[0], half[0]), (-half[2], half[2])),
                (np.array([0.0, 0.0, 1.0]), np.array([1.0, 0.0, 0.0]), np.array([0.0, 1.0, 0.0]), np.array([0.0, 0.0, half[2]]), (-half[0], half[0]), (-half[1], half[1])),
                (np.array([0.0, 0.0, -1.0]), np.array([-1.0, 0.0, 0.0]), np.array([0.0, 1.0, 0.0]), np.array([0.0, 0.0, -half[2]]), (-half[0], half[0]), (-half[1], half[1])),
            ]
            for normal, u_axis, v_axis, center, u_range, v_range in local_faces:
                faces.append(
                    Face(
                        normal=_normalize(rotation @ normal),
                        u_axis=_normalize(rotation @ u_axis),
                        v_axis=_normalize(rotation @ v_axis),
                        plane_point=position + rotation @ center,
                        u_range=u_range,
                        v_range=v_range,
                        color=color,
                    )
                )
        return faces

    def _rotation_matrix(self, rot_deg: list[float] | tuple[float, float, float]) -> np.ndarray:
        rx, ry, rz = [math.radians(float(value)) for value in rot_deg]
        cx, sx = math.cos(rx), math.sin(rx)
        cy, sy = math.cos(ry), math.sin(ry)
        cz, sz = math.cos(rz), math.sin(rz)
        rx_matrix = np.array([[1.0, 0.0, 0.0], [0.0, cx, -sx], [0.0, sx, cx]])
        ry_matrix = np.array([[cy, 0.0, sy], [0.0, 1.0, 0.0], [-sy, 0.0, cy]])
        rz_matrix = np.array([[cz, -sz, 0.0], [sz, cz, 0.0], [0.0, 0.0, 1.0]])
        return rz_matrix @ ry_matrix @ rx_matrix

    def _can_cull_against(self, face: Face, other: Face) -> bool:
        if abs(float(np.dot(face.normal, other.plane_point)) - face.plane_offset) > EPSILON:
            return False
        if np.dot(face.normal, other.normal) > -0.999:
            return False
        if abs(abs(np.dot(face.u_axis, other.u_axis)) - 1.0) > 1e-3:
            return False
        if abs(abs(np.dot(face.v_axis, other.v_axis)) - 1.0) > 1e-3:
            return False
        return True

    def _cull_hidden_faces(self, faces: list[Face]) -> tuple[list[VisiblePatch], int]:
        patches: list[VisiblePatch] = []
        culled_faces = 0
        for index, face in enumerate(faces):
            hidden = False
            for other_index, other in enumerate(faces):
                if index == other_index:
                    continue
                if not self._can_cull_against(face, other):
                    continue
                if (
                    abs(face.u_range[0] - other.u_range[0]) <= EPSILON
                    and abs(face.u_range[1] - other.u_range[1]) <= EPSILON
                    and abs(face.v_range[0] - other.v_range[0]) <= EPSILON
                    and abs(face.v_range[1] - other.v_range[1]) <= EPSILON
                    and np.linalg.norm(face.plane_point - other.plane_point) <= EPSILON
                ):
                    hidden = True
                    break
            if hidden:
                culled_faces += 1
                continue
            patches.append(
                VisiblePatch(
                    face=face,
                    rect=(face.u_range[0], face.v_range[0], face.u_range[1], face.v_range[1]),
                )
            )
        return patches, culled_faces

    def _tint_patch(self, tile: Image.Image, color: tuple[int, int, int], width: int, height: int, opacity: float) -> Image.Image:
        canvas = Image.new("RGBA", (width, height))
        for x in range(0, width, tile.width):
            for y in range(0, height, tile.height):
                canvas.alpha_composite(tile, (x, y))

        base = np.asarray(canvas).astype(np.float32) / 255.0
        tint = np.array(color, dtype=np.float32) / 255.0
        grayscale = base[..., :3].mean(axis=2)
        normalized = np.clip((grayscale - grayscale.min()) / max(1e-6, grayscale.max() - grayscale.min()), 0.0, 1.0)
        stud_mask = np.power(normalized, 1.6)
        shadow_strength = min(0.55, 0.2 + opacity * 0.18)
        highlight_strength = min(0.25, 0.08 + opacity * 0.06)
        shading = 1.0 - shadow_strength * (1.0 - stud_mask) + highlight_strength * stud_mask
        shaded = np.clip(tint[None, None, :] * shading[..., None], 0.0, 1.0)
        alpha = np.ones((height, width, 1), dtype=np.float32)
        combined = np.concatenate((shaded, alpha), axis=2)
        return Image.fromarray((combined * 255.0).astype(np.uint8), "RGBA")

    def _make_material_tile(self, color: tuple[int, int, int], opacity: float) -> tuple[Image.Image, Image.Image]:
        cache_key = (color, round(opacity, 4))
        cached = self._material_tile_cache.get(cache_key)
        if cached is not None:
            return cached

        pixels_per_stud = 256.0
        tile_pixels = max(256, int(round(self.texture_studs_per_tile * pixels_per_stud)))
        color_tile = self.texture_tile.resize((tile_pixels, tile_pixels), Image.Resampling.LANCZOS)
        normal_tile = self.normal_tile.resize((tile_pixels, tile_pixels), Image.Resampling.LANCZOS)
        built = (self._tint_patch(color_tile, color, tile_pixels, tile_pixels, opacity), normal_tile)
        self._material_tile_cache[cache_key] = built
        return built

    def _repeat_patch(self, tile: Image.Image, width: int, height: int, offset_x: int = 0, offset_y: int = 0) -> Image.Image:
        canvas = Image.new("RGBA", (width, height))
        start_x = -((offset_x % tile.width + tile.width) % tile.width)
        start_y = -((offset_y % tile.height + tile.height) % tile.height)
        x = start_x
        while x < width:
            y = start_y
            while y < height:
                canvas.alpha_composite(tile, (x, y))
                y += tile.height
            x += tile.width
        return canvas

    def _layout_atlas_items(
        self,
        prepared: list[dict[str, Any]],
        padding: int,
        max_width: int,
    ) -> tuple[list[tuple[int, int, int, int]], int, int]:
        x = padding
        y = padding
        row_height = 0
        atlas_width = padding
        placements: list[tuple[int, int, int, int]] = []

        for item in prepared:
            width = int(item["width"])
            height = int(item["height"])
            if x + width + padding > max_width:
                x = padding
                y += row_height + padding
                row_height = 0
            placements.append((x, y, width, height))
            x += width + padding
            row_height = max(row_height, height)
            atlas_width = max(atlas_width, x)

        atlas_height = y + row_height + padding
        return placements, atlas_width, atlas_height

    def _build_atlas(self, patches: list[VisiblePatch], texture_settings: dict[str, Any]) -> tuple[Image.Image, Image.Image, list[tuple[float, float, float, float]]]:
        scale = max(float(texture_settings.get("scale", 1.25)), 0.1)
        opacity = float(texture_settings.get("opacity", 0.9))
        pixels_per_stud = 192.0
        prepared: list[dict[str, Any]] = []
        append_log(f"Atlas prep start: patches={len(patches)} scale={scale:.3f} opacity={opacity:.3f}")
        for patch in patches:
            color_tile, normal_tile = self._make_material_tile(patch.face.color, opacity)
            tile_width = color_tile.width
            tile_height = color_tile.height
            width = max(16, min(2048, int(round((patch.width / scale) * pixels_per_stud))))
            height = max(16, min(2048, int(round((patch.height / scale) * pixels_per_stud))))
            plane_u_origin = int(round(patch.face.u_range[0] / max(0.01, self.texture_studs_per_tile * scale) * tile_width))
            plane_v_origin = int(round(patch.face.v_range[0] / max(0.01, self.texture_studs_per_tile * scale) * tile_height))
            prepared.append(
                {
                    "patch": patch,
                    "color_tile": color_tile,
                    "normal_tile": normal_tile,
                    "width": width,
                    "height": height,
                    "offset_x": plane_u_origin,
                    "offset_y": plane_v_origin,
                }
            )

        padding = 4
        max_width = 4096
        max_dimension = 4096
        placements, atlas_width, atlas_height = self._layout_atlas_items(prepared, padding, max_width)
        append_log(f"Atlas layout done: atlas={atlas_width}x{atlas_height} placements={len(placements)}")

        resize_pass = 0
        while prepared and (atlas_width > max_dimension or atlas_height > max_dimension):
            resize_pass += 1
            shrink = min(max_dimension / atlas_width, max_dimension / atlas_height)
            append_log(
                f"Atlas pre-scale pass {resize_pass}: from={atlas_width}x{atlas_height} "
                f"shrink={shrink:.4f}"
            )
            for item in prepared:
                item["width"] = max(1, int(round(int(item["width"]) * shrink)))
                item["height"] = max(1, int(round(int(item["height"]) * shrink)))
            placements, atlas_width, atlas_height = self._layout_atlas_items(prepared, padding, max_width)
            append_log(f"Atlas layout after pass {resize_pass}: atlas={atlas_width}x{atlas_height}")

        atlas = Image.new("RGBA", (atlas_width, atlas_height), (255, 255, 255, 255))
        normal_atlas = Image.new("RGBA", (atlas_width, atlas_height), (128, 128, 255, 255))
        uv_boxes: list[tuple[float, float, float, float]] = []
        for index, (item, (px, py, width, height)) in enumerate(zip(prepared, placements, strict=True), start=1):
            image = self._repeat_patch(item["color_tile"], width, height, item["offset_x"], item["offset_y"])
            normal_image = self._repeat_patch(item["normal_tile"], width, height, item["offset_x"], item["offset_y"])
            atlas.alpha_composite(image, (px, py))
            normal_atlas.alpha_composite(normal_image, (px, py))
            u0 = px / atlas_width
            v0 = 1.0 - (py + height) / atlas_height
            u1 = (px + width) / atlas_width
            v1 = 1.0 - py / atlas_height
            uv_boxes.append((u0, v0, u1, v1))
            if index == 1 or index == len(prepared) or index % 50 == 0:
                append_log(f"Atlas composite progress: {index}/{len(prepared)}")

        return atlas, normal_atlas, uv_boxes

    def _write_glb(
        self,
        output_path: Path,
        patches: list[VisiblePatch],
        uv_boxes: list[tuple[float, float, float, float]],
        texture_image: Image.Image,
        normal_image: Image.Image,
    ) -> None:
        vertices: list[list[float]] = []
        faces: list[list[int]] = []
        uvs: list[list[float]] = []

        for patch, uv_box in zip(patches, uv_boxes, strict=True):
            corners = patch.corners()
            u0, v0, u1, v1 = uv_box
            start = len(vertices)
            vertices.extend(corner.tolist() for corner in corners)
            uvs.extend([[u0, v0], [u1, v0], [u1, v1], [u0, v1]])
            triangle_a = [start, start + 1, start + 2]
            triangle_b = [start, start + 2, start + 3]
            normal = np.cross(corners[1] - corners[0], corners[2] - corners[0])
            if float(np.dot(normal, patch.face.normal)) < 0.0:
                triangle_a = [start, start + 2, start + 1]
                triangle_b = [start, start + 3, start + 2]
            faces.append(triangle_a)
            faces.append(triangle_b)

        material = trimesh.visual.material.PBRMaterial(
            baseColorTexture=texture_image,
            normalTexture=normal_image,
            metallicFactor=0.0,
            roughnessFactor=1.0,
            doubleSided=True,
        )

        mesh = trimesh.Trimesh(
            vertices=np.array(vertices, dtype=np.float32),
            faces=np.array(faces, dtype=np.int64),
            visual=trimesh.visual.texture.TextureVisuals(
                uv=np.array(uvs, dtype=np.float32),
                material=material,
            ),
            process=False,
        )
        output_path.write_bytes(trimesh.exchange.gltf.export_glb(mesh))


class BackendServer:
    def __init__(self, api_key_getter, texture_path: Path, normal_texture_path: Path, port_getter) -> None:
        self.api_key_getter = api_key_getter
        self.port_getter = port_getter
        self.pipeline = MeshPipeline(texture_path, normal_texture_path)
        self.httpd: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None
        self.jobs: dict[str, dict[str, Any]] = {}
        self.jobs_lock = threading.Lock()

    def start(self) -> None:
        if self.thread and self.thread.is_alive():
            return

        server = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                if self.path != "/health":
                    if self.path.startswith("/jobs/"):
                        job_id = self.path.removeprefix("/jobs/").strip()
                        try:
                            self._send_json(server.get_job(job_id))
                        except BackendError as exc:
                            self._send_json({"error": exc.message}, exc.status_code)
                        return
                    self._send_json({"error": "Not found"}, 404)
                    return
                self._send_json(
                    {
                        "ok": True,
                        "uploadConfigured": bool(server.api_key_getter().strip()),
                        "port": server.port_getter(),
                    }
                )

            def do_POST(self) -> None:
                if self.path != "/optimize":
                    self._send_json({"error": "Not found"}, 404)
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    payload = json.loads(self.rfile.read(length).decode("utf-8"))
                    response = server.start_job(payload)
                    self._send_json(response)
                except BackendError as exc:
                    self._send_json({"error": exc.message}, exc.status_code)
                except json.JSONDecodeError:
                    self._send_json({"error": "Request body must be valid JSON."}, 400)
                except Exception as exc:
                    append_log(f"Unexpected backend error: {exc!r}")
                    append_log(traceback.format_exc())
                    self._send_json({"error": str(exc)}, 500)

            def log_message(self, *_args) -> None:
                return

            def _send_json(self, payload: dict[str, Any], status_code: int = 200) -> None:
                body = json.dumps(payload).encode("utf-8")
                self.send_response(status_code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", self.port_getter()), Handler)
        append_log(f"Starting backend server on port {self.port_getter()}")
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        time.sleep(0.2)

    def stop(self) -> None:
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()
        if self.thread:
            self.thread.join(timeout=2.0)
        self.httpd = None
        self.thread = None

    def start_job(self, payload: dict[str, Any]) -> dict[str, Any]:
        job_id = uuid.uuid4().hex
        with self.jobs_lock:
            self.jobs[job_id] = {
                "ok": True,
                "jobId": job_id,
                "state": "queued",
            }
        append_log(f"Job queued: {job_id} model={payload.get('modelName', '')!r} parts={len(payload.get('parts', []))}")

        worker = threading.Thread(target=self._run_job, args=(job_id, payload), daemon=True)
        worker.start()
        return {
            "ok": True,
            "jobId": job_id,
            "state": "queued",
        }

    def get_job(self, job_id: str) -> dict[str, Any]:
        with self.jobs_lock:
            job = self.jobs.get(job_id)
            if not job:
                raise BackendError("Unknown jobId.", 404)
            return dict(job)

    def _set_job(self, job_id: str, data: dict[str, Any]) -> None:
        with self.jobs_lock:
            current = self.jobs.get(job_id, {})
            current.update(data)
            self.jobs[job_id] = current

    def _run_job(self, job_id: str, payload: dict[str, Any]) -> None:
        self._set_job(job_id, {"state": "running"})
        append_log(f"Job running: {job_id}")
        try:
            result = self.handle_optimize(payload)
            result["state"] = "done"
            self._set_job(job_id, result)
            append_log(f"Job done: {job_id} assetId={result.get('assetId')}")
        except BackendError as exc:
            self._set_job(job_id, {"ok": False, "state": "failed", "error": exc.message})
            append_log(f"Job failed: {job_id} error={exc.message}")
        except Exception as exc:
            append_log(f"Unexpected job error: {exc!r}")
            append_log(traceback.format_exc())
            self._set_job(job_id, {"ok": False, "state": "failed", "error": str(exc)})
            append_log(f"Job failed: {job_id} error={exc}")

    def handle_optimize(self, payload: dict[str, Any]) -> dict[str, Any]:
        api_key = self.api_key_getter().strip()
        if not api_key:
            raise BackendError("Open Cloud API key is not configured.")

        creator = payload.get("creator") or {}
        creator_type = str(creator.get("type", "")).lower()
        creator_id = str(creator.get("id", "")).strip()
        append_log(
            f"Incoming optimize payload: creator_type={creator_type!r} creator_id={creator_id!r} "
            f"model={payload.get('modelName', '')!r} parts={len(payload.get('parts', []))}"
        )
        if creator_type not in {"user", "group"} or not creator_id:
            raise BackendError("Payload creator must include a valid type and id.")

        build_dir = Path(__file__).resolve().parent / "build"
        build_dir.mkdir(parents=True, exist_ok=True)
        glb_path, meta = self.pipeline.build(payload, build_dir)
        asset_id = self._upload_asset(api_key, payload, glb_path, creator_type, creator_id)
        return {
            "ok": True,
            "assetId": asset_id,
            "stats": meta["stats"],
            "texture": meta["texture"],
        }

    def _upload_asset(self, api_key: str, payload: dict[str, Any], glb_path: Path, creator_type: str, creator_id: str) -> int:
        request_payload = {
            "assetType": "Model",
            "displayName": f'{payload.get("modelName", "OptimizedModel")}_Optimized',
            "description": "Generated by Mesh To Part local backend",
            "creationContext": {"creator": {f"{creator_type}Id": creator_id}},
        }
        append_log(f"Uploading {glb_path.name} ({glb_path.stat().st_size} bytes) to Roblox")
        try:
            with glb_path.open("rb") as handle:
                response = requests.post(
                    UPLOAD_URL,
                    headers={
                        "x-api-key": api_key,
                        "Connection": "close",
                    },
                    files={
                        "request": (None, json.dumps(request_payload), "application/json"),
                        "fileContent": (glb_path.name, handle, "model/gltf-binary"),
                    },
                    timeout=120,
                )
        except requests.RequestException as exc:
            append_log(f"Upload request failed: {exc!r}")
            raise BackendError(f"Upload request to Roblox failed: {exc}", 502)
        if response.status_code >= 400:
            raise BackendError(self._extract_error(response), 502)

        body = response.json()
        operation_path = str(body.get("path", "")).lstrip("/")
        if not operation_path:
            asset_id = body.get("assetId")
            if asset_id:
                return int(asset_id)
            raise BackendError("Roblox upload succeeded but no operation path was returned.", 502)

        for attempt in range(10):
            if attempt:
                time.sleep(min(8, 2 ** (attempt - 1)))
            operation = requests.get(
                OPERATION_URL.format(path=operation_path),
                headers={"x-api-key": api_key},
                timeout=60,
            )
            if operation.status_code >= 400:
                raise BackendError(self._extract_error(operation), 502)
            data = operation.json()
            if not data.get("done"):
                continue
            error = data.get("error") or data.get("status")
            if error:
                raise BackendError(json.dumps(error), 502)
            response_body = data.get("response") or {}
            asset_id = response_body.get("assetId")
            if asset_id:
                return int(asset_id)
            raise BackendError("Roblox operation finished without an assetId.", 502)
        raise BackendError("Timed out while waiting for Roblox to finish the asset upload.", 504)

    def _extract_error(self, response: requests.Response) -> str:
        try:
            body = response.json()
        except ValueError:
            return response.text or response.reason
        if isinstance(body, dict):
            for key in ("message", "error", "detail", "errors"):
                if key in body:
                    return json.dumps(body[key]) if not isinstance(body[key], str) else body[key]
        return json.dumps(body)
