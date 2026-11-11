import pyray as rl


_ATTENTION_EVENTS = frozenset(
  f'{name}{stage}'
  for name in ('driverDistracted', 'driverUnresponsive')
  for stage in (1, 2, 3)
)
_ENTER_SECONDS = .36
CRITICAL_BACKGROUND = (183, 0, 24, 255)


def attention_panel_rect(rect: rl.Rectangle, avoid_speed_panel: bool) -> rl.Rectangle:
  """Keep PV5's noncritical attention panel clear of the lower-left speed display."""
  if not avoid_speed_panel:
    return rect
  inset = min(600., rect.width * .32)
  return rl.Rectangle(rect.x + inset, rect.y, rect.width - inset, rect.height)


class DriverAttentionIcon:
  def __init__(self):
    self._started = 0.
    self.reset()

  def reset(self) -> None:
    self._event = ''

  def scale(self, alert_type: str, now: float) -> float:
    event = alert_type.partition('/')[0]
    if event not in _ATTENTION_EVENTS:
      self.reset()
      return 0.
    if event != self._event:
      self._event, self._started = event, now
    elapsed = min(_ENTER_SECONDS, max(0., now - self._started))
    remaining = 1. - elapsed / _ENTER_SECONDS
    return 1. - .18 * remaining ** 3


def draw_driver_attention_icon(x: float, y: float, height: float) -> None:
  """Draw an opaque warning triangle with a font-independent exclamation mark."""
  rl.draw_triangle(rl.Vector2(x, y - height * .5),
                   rl.Vector2(x - height * .58, y + height * .5),
                   rl.Vector2(x + height * .58, y + height * .5),
                   rl.Color(255, 225, 91, 255))
  ink = rl.Color(37, 27, 14, 255)
  stem = rl.Rectangle(x - height * .065, y - height * .15, height * .13, height * .37)
  rl.draw_rectangle_rounded(stem, .65, 6, ink)
  rl.draw_circle_v(rl.Vector2(x, y + height * .35), height * .064, ink)
