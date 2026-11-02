"""Draw local raw diagnostic pages only while fresh PV5 Park proof holds."""
import time
import pyray as rl

from openpilot.selfdrive.carrot.obd_debug import Display, TARGETS, parked
from openpilot.system.ui.lib.text_measure import measure_text_cached


class ObdDebugOverlay:
  def __init__(self):
    self.display = Display()

  def draw(self, rect, font, state):
    now = time.monotonic()
    cp = getattr(state, 'CP', None)
    allowed = (getattr(state, 'started', False) and cp is not None and not cp.notCar
               and cp.carFingerprint == 'KIA_PV5' and parked(state.sm, now))
    rows = self.display.update(now, allowed)
    if rows is None or rect.width < 1200 or rect.height < 750:
      return False
    # Use the entire C3/C4 onroad content rectangle, with opaque background.
    # Parent alert/driver-monitor renderers still run after this HUD returns.
    scale = min(rect.width / 1920., rect.height / 1080.)
    width, height = rect.width / scale, rect.height / scale
    x, y = rect.x, rect.y
    panel = rl.Rectangle(x, y, rect.width, rect.height)
    rl.draw_rectangle_rounded(panel, 0, 1, rl.Color(0, 0, 0, 255))
    white, yellow, gray = rl.Color(235, 235, 235, 255), rl.Color(255, 210, 40, 255), rl.Color(155, 155, 155, 255)
    def text(value, px, py, size, color=white):
      rl.draw_text_ex(font, value, rl.Vector2(x + (px - x) * scale, y + (py - y) * scale), size * scale, 0, color)
    text('OBD RAW / PARK TEST / UNKNOWN SIGNALS', x + 32, y + 24, 42, yellow)
    received = 'DATA' if getattr(state, 'bleobd_receiving', False) else 'WAITING'
    text(f'BLEOBD: {received}', x + width - 390, y + 34, 30, white)
    text('D00 = first byte after 62 + DID; yellow = changed in last 3s', x + 32, y + 83, 26, gray)
    col_width = (width - 64) / 2
    row_height = (height - 230) / 3
    for page, (key, tx, _rx, _cmd, length) in enumerate(TARGETS):
      px = x + 32 + (page // 3) * col_width
      py = y + 145 + (page % 3) * row_height
      row = rows.get(key, {})
      age = now - row.get('at', now)
      ready = row.get('state') == 'ok' and 0 <= age <= 30
      label = f'{age:.1f}s' if ready else ('STALE' if row.get('state') == 'ok' else row.get('state', 'waiting').upper())
      text(f'ID:{key} TX:{tx}  {label}', px, py, 32, white if ready else gray)
      if not ready:
        text('DATA: --', px, py + 48, 32, gray)
        continue
      data = bytes.fromhex(row['data'])
      for offset in range(0, length - 3, 12):
        sy = py + 48 + offset // 12 * 38
        text(f'D{offset:02d}:', px, sy, 28, gray)
        runs = []
        for index, value in enumerate(data[offset:offset + 12], offset):
          changed = self.display.changed.get((key, index))
          color = yellow if changed is not None and 0 <= now - changed <= 3 else white
          if not runs or runs[-1][0] != color:
            runs.append((color, []))
          runs[-1][1].append(f'{value:02X}')
        # Usually one text draw per row rather than one per byte. Only changed
        # runs need a separate draw, with measured proportional-font spacing.
        prefix = ''
        for color, values in runs:
          advance = measure_text_cached(font, prefix, 32).x if prefix else 0
          segment = ' '.join(values)
          text(segment, px + 88 + advance, sy, 32, color)
          prefix += segment + ' '
    remaining = max(0, int(self.display.lease['until'] - now))
    text('HEX bytes / unknown meanings / normal battery polling paused', x + 32, y + height - 82, 26, gray)
    text(f'{remaining}s left / leaving Park or enabling control hides this screen', x + 32, y + height - 46, 26, gray)
    return True
