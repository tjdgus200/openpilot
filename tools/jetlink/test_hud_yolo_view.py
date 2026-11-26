from types import SimpleNamespace

import numpy as np
import pytest

import hud_yolo_view as view
from hud_yolo_protocol import HEIGHT, MAX_AGE_NS, WIDTH, Snapshot


def test_rgb_matches_native_plane_order_and_limited_range():
  # One 2x2 block with distinct luma and chroma, replicated across the frame.
  values = (16, 90, 180, 235, 195, 48)
  image = b''.join(bytes([v]) * (WIDTH * HEIGHT // 4) for v in values)
  rgb = view.detector_rgb(image)
  def expected(y):
    base = 298 * (y - 16) + 128
    return [int(np.clip((base + delta) >> 8, 0, 255)) for delta in
            (409 * (48 - 128), -100 * (195 - 128) - 208 * (48 - 128), 516 * (195 - 128))]
  np.testing.assert_array_equal(rgb[:2, :2], [[expected(16), expected(180)], [expected(90), expected(235)]])


@pytest.mark.parametrize('enabled,onroad,stamp,caption,present', [
  (False, True, 1, 'YOLO inference is off', False),
  (True, False, 1, 'Waiting for a fresh YOLO frame', False),
  (None, True, 1, 'Waiting for a fresh YOLO frame', False),
  (True, True, 1, 'Waiting for a fresh YOLO frame', False),  # expired during upload
  (True, True, MAX_AGE_NS + 1, '0 detections', True),
])
def test_cached_pixels_are_hidden_on_disable_staleness_and_session_loss(monkeypatch, enabled, onroad, stamp, caption, present):
  panel = view.YoloView()
  reads, textured, texts = [], [], []
  def read():
    reads.append(True)
    return Snapshot(1, stamp, (), b'')
  panel.reader = SimpleNamespace(read=read)
  panel.texture = object()
  panel._upload = lambda _: None
  theme = SimpleNamespace(panel_bg=(), faint=(), text=(), muted=())
  ui = SimpleNamespace(language='en', _current_theme=lambda: theme, _rounded_rect=lambda *args: None,
                       _draw_text=lambda text, *args, **kwargs: texts.append(text))
  monkeypatch.setattr(view.rl, 'draw_texture_pro', lambda *args: textured.append(True))
  monkeypatch.setattr(view.time, 'monotonic_ns', lambda: MAX_AGE_NS + 1)
  panel.draw(ui, 0, 792, enabled, onroad)
  assert bool(textured) is present
  assert caption in texts
  assert bool(reads) is bool(enabled and onroad)
