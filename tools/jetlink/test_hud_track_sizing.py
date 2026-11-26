import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


CLUSTER = Path(__file__).resolve().parents[2] / 'openpilot/selfdrive/carrot/cluster'
sys.path.insert(0, str(CLUSTER))
import cluster_renderer as hud
from cluster_config import CLUSTER_RADAR_INFO_NONE, EGO_FORWARD_M, RADAR_TO_CAMERA_M, VEHICLE_LENGTH_M
from cluster_scene import RadarPointMarker, Vec3, vehicle_box


@pytest.fixture
def camera(monkeypatch):
  uploads = []
  monkeypatch.setattr(hud.rl, 'load_texture_from_image', lambda image: uploads.append(image.width) or SimpleNamespace(id=1))
  monkeypatch.setattr(hud.rl, 'set_texture_filter', lambda *_: None)
  renderer = object.__new__(hud.ClusterUiRenderer)
  renderer.camera_overlay_z_offset_m = 0.
  renderer._track_texture, renderer._track_tiles = renderer._load_camera_track_atlas(CLUSTER / 'assets/images/track_classes.png')
  assert uploads == [384]
  assert len(renderer._track_tiles) == 6 and all(len(views) == 16 for views in renderer._track_tiles.values())

  dest = hud.rl.Rectangle(0., 0., 1124., 480.)
  projection = hud.CameraOverlayProjection(dest, dest, dest, 1124., 480., 600., 1., 0., 0.,
                                           ((0., -1., 0.), (0., 0., -1.), (1., 0., 0.)), 1.4)
  return renderer, projection


@pytest.mark.parametrize('raw', (False, True))
def test_classified_camera_track_keeps_projected_perspective_past_60_m(camera, monkeypatch, raw):
  renderer, projection = camera
  renderer.hud_track_classes = {34: 2}
  draws = []
  monkeypatch.setattr(hud.rl, 'draw_texture_pro', lambda _texture, src, dst, *_: draws.append((src, dst)))
  monkeypatch.setattr(hud.rl, 'draw_rectangle_rounded_lines_ex', lambda *_: pytest.fail('classified box fell back'))
  monkeypatch.setattr(hud.rl, 'draw_rectangle_rec', lambda *_: pytest.fail('classified point fell back'))
  tile = renderer._track_tiles[2][0]
  for distance in (8., 30., 60., 90.):
    center = Vec3(0., EGO_FORWARD_M + distance, 0.)
    if raw:
      point = RadarPointMarker(center, 0.2, (255, 255, 255), '', distance, 0., front_track_id=34)
      renderer._draw_camera_overlay_radar_point(point, projection, 0., CLUSTER_RADAR_INFO_NONE)
      base = Vec3(0., center.y + RADAR_TO_CAMERA_M, center.z)
    else:
      vehicle = vehicle_box(0., center.y, 0., 3.6, (255, 255, 255), False, front_track_id=34)
      renderer._draw_camera_overlay_vehicle_frame(vehicle, projection, 0., CLUSTER_RADAR_INFO_NONE)
      base = Vec3(0., vehicle.center.y + RADAR_TO_CAMERA_M + VEHICLE_LENGTH_M, hud.CAMERA_OVERLAY_VEHICLE_ROAD_HEIGHT_M)
    source, dst = draws[-1]
    assert source.x == tile.source.x and source.y == tile.source.y
    assert dst.width == pytest.approx(dst.height)
    scale = dst.width / source.width
    assert (dst.x + tile.pivot[0] * scale, dst.y + tile.pivot[1] * scale) == pytest.approx(
      renderer._camera_overlay_screen_xy(base, projection), abs=0.001)
  assert len(draws) == 4
  widths = [dst.width * (tile.bounds[2] - tile.bounds[0]) / source.width for source, dst in draws]
  assert widths[0] > widths[1] > widths[2] > widths[3] > 0.
  assert widths[2] / widths[3] > 1.35


def test_camera_atlas_fit_uses_visible_bounds_and_keeps_viewport_inset(camera, monkeypatch):
  renderer, projection = camera
  draws = []
  monkeypatch.setattr(hud.rl, 'draw_texture_pro', lambda _texture, src, dst, *_: draws.append((src, dst)))
  frame = hud.rl.Rectangle(0., 0., 70., 65.)
  for tile in renderer._track_tiles[2]:
    ground = (29., 440.)
    span = renderer._draw_camera_track_icon(tile, ground, frame, projection.dest, hud.rl.WHITE)
    source, dst = draws[-1]
    assert span is not None
    scale = dst.width / source.width
    assert dst.height / source.height == pytest.approx(scale)
    assert dst.x + tile.pivot[0] * scale == pytest.approx(ground[0], abs=0.001)
    assert dst.y + tile.pivot[1] * scale == pytest.approx(ground[1], abs=0.001)
    left, top, right, bottom = tile.bounds
    assert (right - left) * scale <= frame.width + 0.001
    assert ground[0] + left * scale >= hud.CAMERA_OVERLAY_FRAME_EDGE_PAD_PX - 0.001
    assert span == pytest.approx((ground[1] + top * scale, ground[1] + bottom * scale), abs=0.001)
    assert span[1] <= projection.dest.height - hud.CAMERA_OVERLAY_FRAME_EDGE_PAD_PX
