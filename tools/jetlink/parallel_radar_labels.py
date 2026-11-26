"""One optional wide-frame detector in the Jetlink process' CUDA primary context."""
import ctypes as C
import logging
import os
from pathlib import Path
import queue
import socket
import stat
import struct
import subprocess
import threading
import time

from hud_yolo_protocol import MAX_AGE_NS, MAX_BOXES, WIDE_BYTES, packet_path, write_snapshot
from openpilot.common.pv5_radar_label_context import OFFSETS

HEADER = struct.Struct('<4sIQ16sI')
PREVIEW_INTERVAL_NS = 100_000_000
WAKE = object()
PROCESS_SLOT = threading.Lock()
log = logging.getLogger('jetlink.radar_labels')


def assets():
  return (Path(os.environ.get('PV5_RADAR_LABEL_LIB', '/opt/carrot-jetlink/libpv5_radar_labels.so')),
          Path(os.environ.get('PV5_RADAR_LABEL_PLAN', '/opt/carrot-jetlink/yolov5n.plan')))


class Box(C.Structure):
  _fields_ = [('class_id', C.c_uint32), ('confidence', C.c_float), ('xyxy', C.c_float * 4)]


class RustDetector:
  def __init__(self, lib_path, plan_path):
    self.lib = C.CDLL(os.fspath(lib_path))
    self.lib.pv5_wide_open.argtypes = [C.c_char_p]
    self.lib.pv5_wide_open.restype = C.c_void_p
    self.lib.pv5_wide_detect.argtypes = [C.c_void_p, C.c_char_p, C.c_size_t, C.POINTER(Box), C.c_size_t]
    self.lib.pv5_wide_detect.restype = C.c_int
    self.lib.pv5_wide_close.argtypes = [C.c_void_p]
    self.lib.pv5_wide_close.restype = None
    self.rows = (Box * MAX_BOXES)()
    self.handle = self.lib.pv5_wide_open(os.fsencode(plan_path))
    if not self.handle:
      raise RuntimeError('failed to load wide YOLO TensorRT plan')

  def detect(self, wide):
    count = self.lib.pv5_wide_detect(self.handle, wide, len(wide), self.rows, MAX_BOXES)
    if not 0 <= count <= MAX_BOXES:
      raise RuntimeError('wide YOLO inference failed')
    return count, C.string_at(self.rows, count * C.sizeof(Box))

  def close(self):
    if self.handle:
      self.lib.pv5_wide_close(self.handle)
      self.handle = None


class Sample:
  def __init__(self, seq, frame, stamp, image, preview_key):
    self.seq, self.frame, self.stamp, self.image = seq, frame, stamp, image
    self.preview_key = preview_key
    self.replied = threading.Event()
    self.valid = False


class ParallelRadarLabels:
  def __init__(self, detector_factory, sink, *, release=None, preview_path=None):
    self.detector_factory, self.sink, self.release = detector_factory, sink, release
    self.preview_path = preview_path
    self.requested = True
    self.preview_key = self.published_key = None
    self.preview_attempt_ns = 0
    self.ready = threading.Event()
    self.stopping = threading.Event()
    self.slot = threading.Lock()
    self.mailbox = queue.Queue(maxsize=1)
    self.sample = None
    self.last_detection = None
    self.timing_since = 0.
    self.thread = threading.Thread(target=self._worker, name='pv5-wide-yolo', daemon=True)
    self.thread.start()

  @classmethod
  def open(cls, device):
    preview_path = packet_path()
    directory = preview_path.parent
    worker_path = os.environ.get('PV5_RADAR_LABEL_WORKER')
    if worker_path:
      directory.mkdir(mode=0o700, exist_ok=True)
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
      raise PermissionError('private labels directory required')
    if not PROCESS_SLOT.acquire(blocking=False):
      return None  # A previous session's detector is still retiring.
    sock = None
    worker = None
    try:
      sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
      sock.setblocking(False)
      target = os.fspath(directory / 'detections.sock')

      def detector():
        nonlocal worker
        from jetlink.server.backends.trt import cudart
        cudart.set_device(device)
        if worker_path:
          try:
            worker = subprocess.Popen([worker_path, directory], stdin=subprocess.DEVNULL)
          except OSError:
            pass  # Raw camera detections can still be displayed.
        return RustDetector(*assets())

      def send(header, rows):
        sock.sendmsg([header, rows], [], socket.MSG_DONTWAIT, target)

      def release():
        try:
          if worker is not None:
            worker.terminate()
            try:
              worker.wait(timeout=1)
            except subprocess.TimeoutExpired:
              worker.kill()
              worker.wait()
        finally:
          sock.close()
          PROCESS_SLOT.release()

      return cls(detector, send, release=release, preview_path=preview_path)
    except BaseException:
      if sock is not None:
        sock.close()
      PROCESS_SLOT.release()
      raise

  def offer(self, seq, frame, stamp, warped):
    if (not self.requested or not self.ready.is_set() or self.stopping.is_set() or tuple(warped.shape) != (2, 6, 128, 256)
        or not 0 <= time.monotonic_ns() - stamp < 50_000_000 or not self.slot.acquire(blocking=False)):
      return
    try:
      # The USB transport may reuse its storage immediately after the main reply.
      # Own one copy of the actual second camera slice and no driving-engine buffers.
      image = bytes(memoryview(warped[1]).cast('B'))
      if len(image) != WIDE_BYTES:
        raise ValueError('unexpected wide frame size')
      sample = Sample(seq, frame, stamp, image, self.preview_key)
      self.sample = sample
      self.mailbox.put_nowait(sample)
    except queue.Full:
      self.sample = None
      self.slot.release()
    except Exception:
      self.slot.release()
      self.close()

  def set_request(self, enabled, preview):
    if self.requested and not enabled:
      self.invalidate()
    self.requested = enabled
    self._set_preview(enabled and preview)

  def _set_preview(self, enabled):
    if enabled and self.preview_path is not None:
      if self.preview_key is None:
        self.preview_key = object()
    elif self.preview_key is not None:
      self.preview_key = None
      # Wake the existing worker if no frame is pending. All preview file I/O,
      # including deletion, stays outside the driving response path.
      try:
        self.mailbox.put_nowait(WAKE)
      except queue.Full:
        pass

  def finish(self, seq, valid):
    sample = self.sample
    if sample is not None and sample.seq == seq:
      sample.valid = valid
      sample.replied.set()

  def invalidate(self):
    self._set_preview(False)
    self.timing_since = time.perf_counter()
    self.last_detection = None
    sample, self.sample = self.sample, None
    if sample is not None:
      sample.valid = False
      sample.replied.set()

  def close(self):
    self.stopping.set()
    self.invalidate()
    try:
      self.mailbox.put_nowait(None)
    except queue.Full:
      pass
    # Thread owns the CUDA state until its own stream has completed. A session
    # close never joins a slow auxiliary inference on the protocol thread.

  def _clear_preview(self):
    if self.published_key is not None and self.published_key is not self.preview_key:
      try:
        self.preview_path.unlink(missing_ok=True)
      except OSError:
        pass
      self.published_key = None

  def _publish_preview(self, sample, count, rows, now_ns):
    if (sample.preview_key is None or sample.preview_key is not self.preview_key
        or now_ns - self.preview_attempt_ns < PREVIEW_INTERVAL_NS):
      return
    self.preview_attempt_ns = now_ns
    try:
      write_snapshot(self.preview_path, sample.frame, sample.stamp, count, rows, sample.image)
      self.published_key = sample.preview_key
    except (OSError, ValueError, struct.error, MemoryError):
      pass  # Preview I/O must not disable detection or radar captions.

  def _worker(self):
    detector = None
    try:
      detector = self.detector_factory()
      self.ready.set()
      while not self.stopping.is_set():
        self._clear_preview()
        sample = self.mailbox.get()
        if sample is None:
          return
        if sample is WAKE:
          continue
        try:
          if self.sample is not sample or self.stopping.is_set() or time.monotonic_ns() - sample.stamp >= 50_000_000:
            continue
          started = time.perf_counter()
          count, rows = detector.detect(sample.image)
          finished = time.perf_counter()
          # A failed main inference or a reset must not seed HUD labels.
          deadline = sample.stamp + MAX_AGE_NS
          sample.replied.wait(max(0, (deadline - time.monotonic_ns()) * 1e-9))
          now_ns = time.monotonic_ns()
          if sample.valid and not self.stopping.is_set() and now_ns < deadline:
            self.last_detection = (started, finished)
            self._publish_preview(sample, count, rows, now_ns)
            fingerprint = bytes(sample.image[i] for i in OFFSETS)
            header = HEADER.pack(b'RLD2', sample.frame, sample.stamp, fingerprint, count)
            try:
              self.sink(header, rows)
            except OSError:
              pass  # Busy or absent association worker drops this one display sample.
        finally:
          self.slot.release()
    except Exception as error:
      log.warning('Wide HUD detector disabled (%s)', type(error).__name__)
    finally:
      self.ready.clear()
      self.last_detection = None
      self.stopping.set()
      self.preview_key = None
      self._clear_preview()
      try:
        if detector is not None:
          detector.close()
      finally:
        if self.release is not None:
          self.release()
