"""Static colored meshes used by the Jetson HUD. Model coordinates are Z-up."""
import json
from pathlib import Path

import numpy as np
import pyray as rl


ATLAS_VIEWS = 16
ATLAS_TILE = 64
ATLAS_PIVOT = (0.5, 0.66)
# Nominal HUD footprints, in metres (width, length, height). These are display
# dimensions, not estimated vehicle dimensions. The car matches the HUD box.
TRACK_SIZE_M = {0: (.65, .50, 1.72), 1: (.62, 1.8, 1.12), 2: (1.82, 4.35, 1.35),
                3: (.82, 2.15, 1.45), 5: (2.4, 7.0, 2.75), 7: (2.2, 5.8, 2.65)}
# COCO ID and asset name (+Y forward).
STYLES = ((0, "person"), (1, "bicycle"), (2, "car"), (3, "motorcycle"), (5, "bus"), (7, "truck"))


def _upload_buffer(values, ctype):
    values = np.ascontiguousarray(values)
    dest = rl.mem_alloc(values.nbytes)
    if dest == rl.ffi.NULL:
        raise MemoryError("Track model allocation failed")
    rl.ffi.memmove(dest, rl.ffi.from_buffer(values), values.nbytes)
    return rl.ffi.cast(ctype, dest)


def _upload_model(entry, palette, dimensions):
    vertices = np.asarray(entry["vertices"], dtype=np.float32)
    faces = np.asarray(entry["faces"], dtype=np.int32)
    if (vertices.ndim != 2 or vertices.shape[1] != 3 or not np.isfinite(vertices).all()
            or faces.ndim != 2 or faces.shape[1] != 4 or not 0 < len(faces) <= 4096
            or faces.min() < 0 or faces[:, :3].max() >= len(vertices) or faces[:, 3].max() >= len(palette)):
        raise ValueError("Invalid HUD track mesh")
    extent = np.ptp(vertices, axis=0)
    if np.any(extent < 1e-6):
        raise ValueError("Empty HUD track model dimension")
    vertices *= np.asarray(dimensions, dtype=np.float32) / extent
    triangles = vertices[faces[:, :3]]
    normals = np.cross(triangles[:, 1] - triangles[:, 0], triangles[:, 2] - triangles[:, 0])
    normals /= np.maximum(np.linalg.norm(normals, axis=1, keepdims=True), 1e-8)
    light = np.clip(normals @ (-.36, -.48, .80), 0., 1.)
    rgb = palette[faces[:, 3]] * (.72 + .28 * light[:, None])
    colors = np.column_stack((rgb, np.full(len(rgb), 255))).astype(np.uint8)

    mesh = rl.Mesh()
    mesh.triangleCount = len(faces)
    mesh.vertexCount = 3 * len(faces)
    try:
        mesh.vertices = _upload_buffer(triangles, "float *")
        mesh.colors = _upload_buffer(np.repeat(colors, 3, axis=0), "unsigned char *")
        rl.upload_mesh(rl.ffi.addressof(mesh), False)
        model = rl.load_model_from_mesh(mesh)
    except Exception:
        rl.unload_mesh(mesh)
        raise
    if not rl.is_model_valid(model):
        rl.unload_model(model)
        raise ValueError("Unusable HUD track mesh")
    return model


def load_track_models(path: Path, *, normalized=False):
    asset = json.loads(path.read_text(encoding="utf-8"))
    palette = np.asarray(asset["palette"], dtype=np.float32)
    models = {}
    try:
        for ident, name in STYLES:
            entry = asset["models"][name]
            dimensions = TRACK_SIZE_M[ident]
            # Baking uses identical proportions in a unit-sized orthographic scene.
            if normalized:
                dimensions = tuple(side / max(dimensions) for side in dimensions)
            models[ident] = _upload_model(entry, palette, dimensions)
    except Exception:
        unload_track_models(models)
        raise
    return models


def unload_track_models(models):
    for model in models.values():
        rl.unload_model(model)
