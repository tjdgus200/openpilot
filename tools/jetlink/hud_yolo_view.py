"""Draw the detached Jetson detector's exact wide input and post-NMS boxes."""
import math
import time

import numpy as np
import pyray as rl

from hud_yolo_protocol import HEIGHT, MAX_AGE_NS, WIDTH, SnapshotReader


# COCO classes admitted by native/kernels.cuh, independent of radar association.
CLASSES = {
  0: ('사람', 'Person', (255, 178, 95)), 1: ('자전거', 'Bicycle', (213, 171, 255)),
  2: ('승용차', 'Car', (91, 231, 202)), 3: ('오토바이', 'Motorcycle', (255, 157, 198)),
  5: ('버스', 'Bus', (114, 197, 255)), 7: ('트럭', 'Truck', (250, 217, 113)),
}


def detector_rgb(image):
  """Unpack the wide tensor using the native detector's BT.601 pixel mapping."""
  planes = np.frombuffer(image, dtype=np.uint8).reshape(6, HEIGHT // 2, WIDTH // 2)
  luma = np.empty((HEIGHT, WIDTH), dtype=np.int32)
  for i in range(4):
    luma[i % 2::2, i // 2::2] = planes[i]
  luma -= 16
  luma *= 298
  luma += 128  # Round like floor(pixel + 0.5) at the native detector input.
  blocks = luma.reshape(HEIGHT // 2, 2, WIDTH // 2, 2)
  u, v = planes[4:].astype(np.int32) - 128
  rgb = np.empty((HEIGHT, WIDTH, 3), dtype=np.uint8)
  for channel, chroma in enumerate((409 * v, -100 * u - 208 * v, 516 * u)):
    value = (blocks + chroma[:, None, :, None]) >> 8
    rgb[:, :, channel] = np.clip(value, 0, 255).reshape(HEIGHT, WIDTH)
  return rgb


class YoloView:
  def __init__(self):
    self.reader = SnapshotReader()
    self.texture = self.key = None

  def close(self):
    if self.texture is not None:
      rl.unload_texture(self.texture)
    self.texture = self.key = None

  def _upload(self, sample):
    key = (sample.frame_id, sample.stamp_ns)
    if key == self.key:
      return
    pixels = detector_rgb(sample.image)
    data = rl.ffi.cast('void *', rl.ffi.from_buffer(pixels))
    if self.texture is None:
      self.texture = rl.load_texture_from_image(
        rl.Image(data, WIDTH, HEIGHT, 1, rl.PixelFormat.PIXELFORMAT_UNCOMPRESSED_R8G8B8))
      if not self.texture.id:
        self.texture = None
        return
      rl.set_texture_filter(self.texture, rl.TextureFilter.TEXTURE_FILTER_BILINEAR)
    else:
      rl.update_texture(self.texture, data)
    self.key = key

  def draw(self, ui, x, panel_width, enabled, onroad):
    theme, ko = ui._current_theme(), ui.language == 'ko'
    ui._rounded_rect(x, 1, panel_width, 478, 10, theme.panel_bg, theme.faint, 1)
    ui._draw_text('광각 YOLO' if ko else 'Wide YOLO', x + 18, 26, 26, theme.text)
    sample = self.reader.read() if enabled and onroad else None
    if sample is not None:
      try:
        self._upload(sample)
      except (MemoryError, RuntimeError, OSError):
        sample = None
    if sample is not None and not 0 <= time.monotonic_ns() - sample.stamp_ns < MAX_AGE_NS:
      sample = None
    # Leave room for the shared Jetson timing and resource rows, in both layouts.
    width = min(panel_width - 24, 742)
    left, top = x + (panel_width - width) / 2, 71
    height = width * HEIGHT / WIDTH
    if sample is None or self.texture is None:
      if enabled is False:
        status = 'YOLO 추론 꺼짐' if ko else 'YOLO inference is off'
      else:
        status = '새 YOLO 프레임 대기' if ko else 'Waiting for a fresh YOLO frame'
      ui._draw_text(status, x + panel_width / 2, top + height / 2, 25, theme.muted, anchor='center')
      return
    count = len(sample.rows)
    caption = f'검출 {count}개' if ko else f'{count} detection' + ('' if count == 1 else 's')
    ui._draw_text(caption, x + 18, 52, 18, theme.muted)
    rect = rl.Rectangle(left, top, width, height)
    rl.draw_texture_pro(self.texture, rl.Rectangle(0, 0, WIDTH, HEIGHT), rect, rl.Vector2(0, 0), 0, rl.WHITE)
    scale = width / WIDTH
    for class_id, confidence, x0, y0, x1, y1 in reversed(sample.rows):
      name_ko, name_en, rgb = CLASSES.get(class_id, (str(class_id), str(class_id), (234, 237, 240)))
      box = rl.Rectangle(left + x0 * scale, top + y0 * scale, (x1 - x0) * scale, (y1 - y0) * scale)
      rl.draw_rectangle_lines_ex(box, 2, rl.Color(*rgb, 255))
      label = f'{name_ko if ko else name_en} {confidence:.0%}'
      label_w = math.ceil(ui._measure_text(label, 17)[0]) + 10
      label_x = min(left + width - label_w, box.x)
      label_y = max(top, box.y - 22)
      rl.draw_rectangle(round(label_x), round(label_y), label_w, 22, rl.Color(12, 21, 27, 235))
      ui._draw_text(label, label_x + 5, label_y + 11, 17, rgb)
