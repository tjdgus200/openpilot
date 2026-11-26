"""Local latest-only preview of the exact warped camera frame sent to YOLO."""

import os
from pathlib import Path
import stat
import struct
import time
from typing import NamedTuple

WIDTH, HEIGHT = 512, 256
WIDE_BYTES = WIDTH * HEIGHT * 3 // 2
MAX_BOXES = 100
MAX_AGE_NS = 250_000_000
HEADER = struct.Struct('<4sIQI')
ROW = struct.Struct('<I5f')
MAGIC = b'YOL1'
CLASSES = frozenset((0, 1, 2, 3, 5, 7))  # COCO classes emitted by the pinned detector.
MAX_PACKET = HEADER.size + MAX_BOXES * ROW.size + WIDE_BYTES
READ_FLAGS = os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NONBLOCK', 0) | getattr(os, 'O_NOFOLLOW', 0)


class Snapshot(NamedTuple):
  frame_id: int
  stamp_ns: int
  rows: tuple[tuple[int, float, float, float, float, float], ...]
  image: bytes


def packet_path():
  return Path(os.environ.get('PV5_RADAR_LABEL_DIR', '/run/pv5-radar-labels')) / 'hud-yolo.packet'


def write_snapshot(path, frame, stamp_ns, count, rows, image):
  if not (0 <= count <= MAX_BOXES and len(rows) == count * ROW.size and len(image) == WIDE_BYTES):
    raise ValueError('Invalid YOLO preview payload')
  # The existing detector worker owns the checked private runtime directory
  # and this single staging file. The driving request path does no preview I/O.
  temporary = path.with_suffix('.tmp')
  # Create a new regular inode in the private directory with restrictive mode.
  temporary.unlink(missing_ok=True)
  file = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_BINARY', 0), 0o600)
  try:
    with os.fdopen(file, 'wb') as stream:
      stream.write(HEADER.pack(MAGIC, frame, stamp_ns, count))
      stream.write(rows)
      stream.write(image)
    os.replace(temporary, path)
  except BaseException:
    temporary.unlink(missing_ok=True)
    raise


def _decode_snapshot(file, size, now_ns):
  magic, frame, stamp_ns, count = HEADER.unpack(file.read(HEADER.size))
  if (magic != MAGIC or count > MAX_BOXES or size != HEADER.size + count * ROW.size + WIDE_BYTES
      or not 0 <= now_ns - stamp_ns < MAX_AGE_NS):
    return None
  row_bytes = file.read(count * ROW.size)
  if len(row_bytes) != count * ROW.size:
    return None
  rows = tuple(ROW.iter_unpack(row_bytes))
  if not all(class_id in CLASSES and 0. <= confidence <= 1. and
             0. <= x0 < x1 <= WIDTH and 0. <= y0 < y1 <= HEIGHT
             for class_id, confidence, x0, y0, x1, y1 in rows):
    return None
  image = file.read(WIDE_BYTES + 1)
  if len(image) == WIDE_BYTES:
    return Snapshot(frame, stamp_ns, rows, image)
  return None


class SnapshotReader:
  def __init__(self, path=None):
    self.path = packet_path() if path is None else path
    self.signature = self.snapshot = None

  def read(self, now_ns=None):
    try:
      with os.fdopen(os.open(self.path, READ_FLAGS), 'rb') as file:
        info = os.fstat(file.fileno())
        now_ns = time.monotonic_ns() if now_ns is None else now_ns
        if stat.S_ISREG(info.st_mode) and HEADER.size + WIDE_BYTES <= info.st_size <= MAX_PACKET:
          # Read the actual opened inode, including when rename races this read.
          signature = (info.st_ino, info.st_mtime_ns, info.st_size)
          if signature != self.signature:
            self.snapshot = _decode_snapshot(file, info.st_size, now_ns)
            self.signature = signature
          if self.snapshot is not None and 0 <= now_ns - self.snapshot.stamp_ns < MAX_AGE_NS:
            return self.snapshot
    except (OSError, struct.error):
      pass
    self.signature = self.snapshot = None
    return None


def read_snapshot(path=None, now_ns=None):
  return SnapshotReader(path).read(now_ns)
