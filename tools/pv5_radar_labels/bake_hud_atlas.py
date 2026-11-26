"""Bake the camera-overlay sprites from the same meshes the 3D HUD draws."""
from math import ceil, floor
from pathlib import Path
import sys
from tempfile import NamedTemporaryFile

from PIL import Image, ImageFilter
import pyray as rl

CLUSTER = Path(__file__).resolve().parents[2] / 'openpilot/selfdrive/carrot/cluster'
sys.path.insert(0, str(CLUSTER))
from cluster_track_models import ATLAS_PIVOT, ATLAS_TILE, ATLAS_VIEWS, STYLES, load_track_models, unload_track_models


def render_sprite(model, yaw, camera, target, side):
  rl.begin_texture_mode(target)
  rl.clear_background(rl.BLANK)
  rl.begin_mode_3d(camera)
  rl.draw_model_ex(model, rl.Vector3(0, 0, 0), rl.Vector3(0, 0, 1),
                   yaw, rl.Vector3(1, 1, 1), rl.WHITE)
  rl.end_mode_3d()
  rl.end_texture_mode()

  raw = rl.load_image_from_texture(target.texture)
  try:
    rl.image_flip_vertical(raw)
    rl.image_format(raw, rl.PixelFormat.PIXELFORMAT_UNCOMPRESSED_R8G8B8A8)
    sprite = Image.frombytes('RGBA', (side, side), bytes(rl.ffi.buffer(raw.data, side * side * 4)))
  finally:
    rl.unload_image(raw)

  outline = Image.new('RGBA', sprite.size, (8, 18, 28, 255))
  outline.putalpha(sprite.getchannel('A').filter(ImageFilter.MaxFilter(7)))
  outline.alpha_composite(sprite)
  return outline


def bake():
  side = 384
  atlas = Image.new('RGBA', (ATLAS_TILE * len(STYLES), ATLAS_TILE * ATLAS_VIEWS))
  rl.set_config_flags(rl.FLAG_WINDOW_HIDDEN)
  rl.init_window(side, side, 'HUD atlas')
  models = {}
  target = None
  try:
    models = load_track_models(CLUSTER / 'assets/models/track_classes.json', normalized=True)
    target = rl.load_render_texture(side, side)
    camera = rl.Camera3D(rl.Vector3(0, -3.1, 1.60), rl.Vector3(0, 0, .38),
                         rl.Vector3(0, 0, 1), 1.65, rl.CameraProjection.CAMERA_ORTHOGRAPHIC)
    origin = rl.get_world_to_screen_ex(rl.Vector3(0, 0, 0), camera, side, side)
    pivot_x, pivot_y = (fraction * ATLAS_TILE for fraction in ATLAS_PIVOT)
    inset = 4
    size = ATLAS_TILE - 2 * inset
    for column, style in enumerate(STYLES):
      ident = style[0]
      sprites = []
      bounds = []
      for row in range(ATLAS_VIEWS):
        yaw = row * 360. / ATLAS_VIEWS
        sprite = render_sprite(models[ident], yaw, camera, target, side)
        box = sprite.getbbox()
        if box is None or box[0] == 0 or box[1] == 0 or box[2] == side or box[3] == side:
          raise ValueError(f'Empty or clipped HUD sprite for class {ident} at {yaw:g} degrees')
        sprites.append(sprite)
        bounds.append(box)

      # Fit all views at one scale and keep the projected ground origin at the fixed atlas pivot.
      left_px, right_px = pivot_x - inset, ATLAS_TILE - inset - pivot_x
      top_px, bottom_px = pivot_y - inset, ATLAS_TILE - inset - pivot_y
      scale = min(left_px / max(1., origin.x - min(box[0] for box in bounds)),
                  right_px / max(1., max(box[2] for box in bounds) - origin.x),
                  top_px / max(1., origin.y - min(box[1] for box in bounds)),
                  bottom_px / max(1., max(box[3] for box in bounds) - origin.y))
      left, top = origin.x - left_px / scale, origin.y - top_px / scale
      right, bottom = origin.x + right_px / scale, origin.y + bottom_px / scale
      crop_left, crop_top = floor(left), floor(top)
      crop = (crop_left, crop_top, ceil(right), ceil(bottom))
      resize_box = (left - crop_left, top - crop_top, right - crop_left, bottom - crop_top)
      for row, sprite in enumerate(sprites):
        sprite = sprite.crop(crop).resize((size, size), Image.Resampling.LANCZOS, box=resize_box)
        atlas.alpha_composite(sprite, (column * ATLAS_TILE + inset, row * ATLAS_TILE + inset))

    output = CLUSTER / 'assets/images/track_classes.png'
    temporary_path = None
    try:
      with NamedTemporaryFile(dir=output.parent, delete=False) as temporary:
        temporary_path = Path(temporary.name)
        atlas.save(temporary, format='PNG', optimize=True)
      temporary_path.replace(output)
    finally:
      if temporary_path is not None:
        temporary_path.unlink(missing_ok=True)
  finally:
    if target is not None:
      rl.unload_render_texture(target)
    unload_track_models(models)
    rl.close_window()


if __name__ == '__main__':
  bake()
