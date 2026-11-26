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

from openpilot.common.pv5_radar_label_context import OFFSETS

WIDE_BYTES = 6 * 128 * 256
MAX_BOXES = 100
HEADER = struct.Struct('<4sIQ16sI')
MAX_AGE_NS = 250_000_000
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
  def __init__(self, seq, frame, stamp, image):
    self.seq, self.frame, self.stamp, self.image = seq, frame, stamp, image
    self.replied = threading.Event()
    self.valid = False


class ParallelRadarLabels:
  def __init__(self, detector_factory, sink, *, release=None):
    self.detector_factory, self.sink, self.release = detector_factory, sink, release
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
    directory = Path(os.environ.get('PV5_RADAR_LABEL_DIR', '/run/pv5-radar-labels'))
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
          worker = subprocess.Popen([worker_path, directory], stdin=subprocess.DEVNULL)
        return RustDetector(*assets())

      def send(header, rows):
        try:
          sock.sendmsg([header, rows], [], socket.MSG_DONTWAIT, target)
        except OSError:
          if worker is not None and worker.poll() is not None:
            raise RuntimeError('The local radar association worker stopped')
          raise

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

      return cls(detector, send, release=release)
    except BaseException:
      if sock is not None:
        sock.close()
      PROCESS_SLOT.release()
      raise

  def offer(self, seq, frame, stamp, warped):
    if (not self.ready.is_set() or self.stopping.is_set() or tuple(warped.shape) != (2, 6, 128, 256)
        or not 0 <= time.monotonic_ns() - stamp < 50_000_000 or not self.slot.acquire(blocking=False)):
      return
    try:
      # The USB transport may reuse its storage immediately after the main reply.
      # Own one copy of the actual second camera slice and no driving-engine buffers.
      image = bytes(memoryview(warped[1]).cast('B'))
      if len(image) != WIDE_BYTES:
        raise ValueError('unexpected wide frame size')
      sample = Sample(seq, frame, stamp, image)
      self.sample = sample
      self.mailbox.put_nowait(sample)
    except Exception:
      self.slot.release()
      self.close()

  def finish(self, seq, valid):
    sample = self.sample
    if sample is not None and sample.seq == seq:
      sample.valid = valid
      sample.replied.set()

  def invalidate(self):
    self.timing_since = time.perf_counter()
    self.last_detection = None
    if self.sample is not None:
      self.sample.valid = False
      self.sample.replied.set()

  def close(self):
    self.stopping.set()
    self.invalidate()
    try:
      self.mailbox.put_nowait(None)
    except queue.Full:
      pass
    # Thread owns the CUDA state until its own stream has completed. A session
    # close never joins a slow auxiliary inference on the protocol thread.

  def _worker(self):
    detector = None
    try:
      detector = self.detector_factory()
      self.ready.set()
      while not self.stopping.is_set():
        sample = self.mailbox.get()
        if sample is None:
          return
        try:
          if self.stopping.is_set() or time.monotonic_ns() - sample.stamp >= 50_000_000:
            continue
          started = time.perf_counter()
          count, rows = detector.detect(sample.image)
          finished = time.perf_counter()
          # A failed main inference or a reset must not seed HUD labels.
          deadline = sample.stamp + MAX_AGE_NS
          sample.replied.wait(max(0, (deadline - time.monotonic_ns()) * 1e-9))
          if sample.valid and not self.stopping.is_set() and time.monotonic_ns() < deadline:
            self.last_detection = (started, finished)
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
      try:
        if detector is not None:
          detector.close()
      finally:
        if self.release is not None:
          self.release()
