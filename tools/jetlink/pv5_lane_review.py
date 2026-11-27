"""Shadow-only classification of the signalled boundary in the actual MED road warp."""
import numpy as np
from PIL import Image

from openpilot.selfdrive.modeld.constants import ModelConstants

UNKNOWN = (-1, 0.)
# Sample the boundary from the first visible road immediately ahead. The far
# samples add context to the crop but cannot establish the marking by themselves.
DISTANCES = np.arange(8., 19.)
NEAR = DISTANCES <= 13
WIDTH, HEIGHT = 512, 256
NETWORK, PROTO = 416, 104


def road_luma(packed):
  """Jetlink's first four road planes use vertical parity before horizontal."""
  planes = np.frombuffer(packed, np.uint8).reshape(4, HEIGHT // 2, WIDTH // 2)
  image = np.empty((HEIGHT, WIDTH), np.uint8)
  for plane in range(4):
    image[plane % 2::2, plane // 2::2] = planes[plane]
  return image


def target_crop(side, line_yz, probability):
  """Project modelV2 laneLines[1 or 2] in calibrated FRD into the road MED warp."""
  if side not in (1, 2) or not .5 <= probability <= 1.:
    return None
  coords = np.asarray(line_yz)
  if coords.shape != (33, 2):
    return None
  x = ModelConstants.X_IDXS
  ys, zs = (np.interp(DISTANCES, x, column) for column in coords.T)
  if (not np.all(np.isfinite(ys)) or not np.all(np.isfinite(zs))
      or not np.all((ys[NEAR] * (-1 if side == 1 else 1)) > .25)):
    return None
  # Same fixed virtual intrinsics as get_warp_matrix(..., bigmodel_frame=False).
  # Lane z already includes road height relative to the camera.
  u, v = 256. + 910. * ys / DISTANCES, 47.6 + 910. * zs / DISTANCES
  visible = (u >= 0) & (u < WIDTH) & (v >= 0) & (v < HEIGHT)
  if np.count_nonzero(visible & NEAR) < 3:
    return None
  crop_x = int(np.clip(round(np.median(u[visible & NEAR]) - HEIGHT / 2), 0, WIDTH - HEIGHT))
  px = (u - crop_x + .5) * PROTO / HEIGHT - .5
  py = (v + .5) * PROTO / HEIGHT - .5
  valid = visible & (px >= 3) & (px < PROTO - 3) & (py >= 1) & (py < PROTO - 1)
  if np.count_nonzero(valid & NEAR) < 3:
    return None
  return crop_x, np.round(px[valid]).astype(int), np.round(py[valid]).astype(int), NEAR[valid]


def select_marking(outputs, px, py, near):
  pred = np.asarray(outputs['output0'])
  proto = np.asarray(outputs['output1'])
  if (pred.shape != (1, 42, 3549) or proto.shape != (1, 32, PROTO, PROTO)
      or not np.isfinite(pred).all() or not np.isfinite(proto).all()):
    return UNKNOWN
  pred = pred[0]
  scores = pred[4:10].max(axis=0)
  anchors = np.flatnonzero(scores >= .25)
  if not 0 < len(anchors) <= 128:
    return UNKNOWN
  # Evaluate the Xiaoge class masks only in a narrow corridor along the actual
  # boundary. Duplicate same-class anchors corroborate a type but never add votes.
  offsets = np.arange(-10, 11)
  cols = px[:, None] + offsets
  inside = (cols >= 0) & (cols < PROTO)
  cols = np.clip(cols, 0, PROTO - 1)
  features = proto[0, :, py[:, None], cols]
  contenders = set()
  supported = {}
  for anchor in anchors:
    cx, cy, width, height = pred[:4, anchor] * (PROTO / NETWORK)
    if width <= 0 or height <= 0:
      continue
    in_box = ((cols >= cx - width / 2) & (cols <= cx + width / 2)
              & (py[:, None] >= cy - height / 2) & (py[:, None] <= cy + height / 2))
    if not np.any(in_box[near]):
      continue
    mask = ((features @ pred[10:, anchor]) > 0.) & in_box & inside
    on_target = mask[:, np.abs(offsets) <= 3].any(axis=1)
    # A filled roadway patch or a sideways stripe can intersect every projected
    # sample without tracing the target boundary. It cannot establish its type.
    broad = mask.sum(axis=1) > 12
    if np.any(on_target & near & broad):
      contenders.add(-1)
    hit = on_target & ~broad
    near_rows = np.unique(py[hit & near])
    if not len(near_rows):
      continue
    # Xiaoge classes 0,2,5 are solid and 1 is dashed; 3,4 are ignored.
    # A single mask can carry competing class scores as well as overlapping
    # masks of different classes. A near-field conflict makes either ambiguous.
    class_scores = pred[4:10, anchor]
    types = (1, 0, 1, -1, -1, 1)
    kind = types[int(class_scores.argmax())]
    contenders.update(types[i] for i in np.flatnonzero(class_scores >= .25))
    if len(near_rows) >= 3 and np.ptp(near_rows) >= 5 and scores[anchor] >= .65:
      supported[kind] = max(supported.get(kind, 0.), float(scores[anchor]))
  if len(contenders) == 1:
    kind = next(iter(contenders))
    if kind >= 0 and kind in supported and supported[kind] <= 1.:
      return kind, supported[kind]
  return UNKNOWN


def classify(road_y, side, line_yz, probability, forward):
  target = target_crop(side, line_yz, probability)
  if target is None or road_y.shape != (HEIGHT, WIDTH) or road_y.dtype != np.uint8:
    return UNKNOWN
  x, px, py, near = target
  patch = road_y[:, x:x + HEIGHT]
  resized = Image.fromarray(patch).resize((NETWORK, NETWORK), Image.Resampling.BILINEAR)
  # Xiaoge takes normalized grayscale replicated across its three inputs.
  gray = np.asarray(resized, dtype=np.float32) * (1. / 255.)
  blob = np.broadcast_to(gray, (1, 3, NETWORK, NETWORK)).copy()
  return select_marking(forward(blob), px, py, near)
