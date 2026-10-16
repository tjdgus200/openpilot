"""AIO framing/ownership/failure tests; regular-file test checks real Linux ABI."""
import ctypes
import errno
import os
from pathlib import Path
import sys
import threading
import time

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'third_party/jetlink'))
from jetlink import protocol as P
from jetlink.transport.base import LinkError
from jetlink.transport.ffs import FfsTransport
from jetlink.transport.ffs_aio import AioWriter, AioFfsTransport, AioCleanupError, DEPTH, REQUEST_BYTES
from jetlink.transport.aio_writer_native import Aio, Layout, address


def eventually(predicate):
  end = time.monotonic() + 2
  while not predicate():
    assert time.monotonic() < end
    time.sleep(.001)


class FakeAio:
  address, layout = staticmethod(address), staticmethod(Layout)

  def __init__(self, fd, depth):
    self.sequence = 1
    self.events, self.bytes, self.submissions = [], [], []
    self.lock = threading.Lock()
    self.gate = threading.Event()
    self.gate.set()
    self.max_submit = depth
    self.failure = None
    self.closed = False

  def submit(self, plan, bases, start, count):
    count = min(count, self.max_submit)
    if self.failure:
      raise self.failure
    self.submissions.append((start, count))
    with self.lock:
      for i in range(start, start + count):
        data = b''.join(ctypes.string_at(bases[int(plan.span[j])] + int(plan.offset[j]), int(plan.length[j]))
                        for j in range(plan.first[i], plan.first[i + 1]))
        self.bytes.append(data)
        self.events.append((self.sequence, len(data), 0))
        self.sequence += 1
    return count

  def reap(self, minimum, timeout):
    self.gate.wait(timeout)
    if not self.gate.is_set():
      return []
    with self.lock:
      events, self.events = self.events[::-1], []  # completions need not be in order
    if not events:
      time.sleep(min(timeout, .001))
    return events

  def close(self): self.closed = True


@pytest.fixture
def writer(monkeypatch):
  monkeypatch.setattr('jetlink.transport.ffs_aio.background_thread', lambda: None)
  aborted = []
  result = AioWriter(9, lambda: aborted.append(True), factory=FakeAio)
  yield result, aborted
  result.aio.gate.set()
  eventually(lambda: not result.pending)
  result.close()


def test_partial_submit_and_out_of_order_reap_preserve_bytes_and_copy_lifetime(writer):
  w, aborted = writer
  w.aio.max_submit = 3
  source = bytearray(i % 251 for i in range(600000))
  expected = bytes(source)
  assert w.write([memoryview(source)], time.monotonic() + 2) == len(source)
  source[:] = b'\xff' * len(source)
  eventually(lambda: not w.pending)
  assert b''.join(w.aio.bytes) == expected
  assert len(w.aio.submissions) > 1 and not aborted


@pytest.mark.parametrize('secondary', [0, 16384])
def test_functionfs_modern_and_c3_legacy_success_completion(writer, secondary):
  w, aborted = writer
  w.aio.gate.clear()
  w.write([memoryview(b'x' * 16384)], time.monotonic() + 1)
  token, count, _ = w.aio.events[0]
  assert count == 16384
  w.aio.events[0] = (token, count, secondary)
  w.aio.gate.set()
  eventually(lambda: not w.pending)
  w.check()
  assert not aborted


@pytest.mark.parametrize('kind', ['negative', 'legacy_negative', 'short', 'legacy_short', 'secondary', 'unknown'])
def test_bad_completion_poison_link_without_retransmission(writer, kind):
  w, aborted = writer
  w.aio.gate.clear()
  w.write([memoryview(b'payload')], time.monotonic() + 1)
  token, count, _ = w.aio.events[0]
  w.aio.events[0] = {'negative': (token, -errno.EIO, 0), 'legacy_negative': (token, -errno.EIO, -errno.EIO),
                     'short': (token, count - 1, 0), 'legacy_short': (token, count - 1, count - 1),
                     'secondary': (token, count, 1), 'unknown': (token + 99, count, 0)}[kind]
  if kind == 'unknown':
    w.aio.events.append((token, count, 0))  # real completion still must be reaped
  w.aio.gate.set()
  eventually(lambda: bool(aborted))
  with pytest.raises(LinkError):
    w.write([memoryview(b'new')], time.monotonic() + 1)
  assert len(w.aio.submissions) == 1


def test_deadline_aborts_once_even_when_no_later_send_or_receive(writer):
  w, aborted = writer
  w.aio.gate.clear()
  w.write([memoryview(b'x')], time.monotonic() + .02)
  eventually(lambda: bool(aborted))
  with pytest.raises(LinkError, match='deadline'):
    w.check()
  time.sleep(.03)
  assert len(aborted) == 1


def test_queue_is_bounded_and_close_never_destroys_live_kernel_requests(writer):
  w, _ = writer
  w.aio.gate.clear()
  w.write([memoryview(b'x' * (REQUEST_BYTES * DEPTH))], time.monotonic() + 2)
  assert len(w.pending) == DEPTH
  with pytest.raises(AioCleanupError, match='refusing rebind'):
    w.close()
  assert not w.aio.closed
  w.aio.gate.set()


def test_submission_failure_cannot_fall_back_to_synchronous_resend(writer):
  w, aborted = writer
  w.aio.failure = OSError(errno.ENOMEM, 'fragmented')
  with pytest.raises(LinkError, match='fragmented'):
    w.write([memoryview(b'x')], time.monotonic() + 1)
  assert aborted == [True] and not w.aio.bytes


def test_failed_native_cleanup_blocks_rebind_and_retains_context(writer, monkeypatch):
  w, _ = writer
  close = w.aio.close
  def failed():
    raise OSError(errno.EIO, 'destroy failed')
  monkeypatch.setattr(w.aio, 'close', failed)
  with pytest.raises(AioCleanupError, match='refusing rebind'):
    w.close()
  assert w.aio is not None
  monkeypatch.setattr(w.aio, 'close', close)


@pytest.mark.parametrize('version', [2, 3])
@pytest.mark.parametrize('size', [0, 992, 16352, 32780, 475000, 1048700])
def test_async_splitting_preserves_existing_protocol_and_padding(monkeypatch, version, size):
  monkeypatch.setattr('jetlink.transport.ffs.background_thread', lambda: None)
  expected = []
  plain = FfsTransport.__new__(FfsTransport)
  plain._prepare('/unused', None)
  plain.protocol_version = version
  plain._write = lambda bufs: (expected.extend(bytes(b) for b in bufs) or sum(b.nbytes for b in bufs))
  try:
    plain.send(P.Msg.INFER_REQ, 17, [bytes([37]) * size])
    # Use the actual base sender of the new transport, with immediate kernel
    # copies simulated. It must produce byte-for-byte identical v2/v3 frames.
    monkeypatch.setenv('CARROT_JETLINK_AIO_WRITE', '1')
    t = AioFfsTransport.__new__(AioFfsTransport)
    t._prepare('/unused', None)
    t.protocol_version = version
    t._ensure_epfiles = lambda: None
    fake = FakeAio(1, DEPTH)
    monkeypatch.setattr('jetlink.transport.ffs_aio.background_thread', lambda: None)
    t._aio_writer = AioWriter(1, lambda: None, factory=lambda *a: fake)
    try:
      t.send(P.Msg.INFER_REQ, 17, [bytes([37]) * size], timeout=2)
      eventually(lambda: not t._aio_writer.pending)
      assert b''.join(fake.bytes) == b''.join(expected)
      assert all(len(b) <= REQUEST_BYTES for b in fake.bytes)
    finally:
      t._aio_writer.close()
      t._aio_writer = None
      t.close()
  finally:
    plain.close()


def test_default_switch_uses_existing_transport(monkeypatch):
  monkeypatch.delenv('CARROT_JETLINK_AIO_WRITE', raising=False)
  t = AioFfsTransport.__new__(AioFfsTransport)
  t._prepare('/unused', None)
  monkeypatch.setattr(FfsTransport, '_write', lambda self, bufs: 91)
  try:
    assert not t._aio_write_enabled and t._write([]) == 91
  finally:
    t.close()


@pytest.mark.skipif(sys.platform != 'linux', reason='Linux native AIO ABI')
def test_native_aio_regular_file_submission_and_completion_identity(tmp_path):
  fd = os.open(tmp_path / 'native', os.O_RDWR | os.O_CREAT, 0o600)
  aio = Aio(fd, 4)
  data = bytearray(b'abcdef' * 100)
  try:
    plan = aio.layout((len(data),), REQUEST_BYTES)
    assert aio.submit(plan, [aio.address(data)[0]], 0, 1) == 1
    assert aio.reap(1, 1) == [(1, len(data), 0)]
    assert os.pread(fd, len(data), 0) == data
  finally:
    aio.close()
    os.close(fd)
