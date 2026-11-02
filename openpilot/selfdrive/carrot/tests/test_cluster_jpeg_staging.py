"""Real threads and borrowed-buffer mutation; no physical USB/GPU claims."""
import threading
from types import SimpleNamespace as NS

import pytest

import cluster_usb_pipeline as module


def test_two_slot_ownership_latest_only_and_same_bytes():
  entered, release, sent = threading.Event(), threading.Event(), threading.Event()
  encoded, output = [], []
  def encode(rgba, width, height):
    before = bytes(rgba)
    if not encoded:
      entered.set()
      assert release.wait(2)
      assert bytes(rgba) == before  # Producer must never overwrite an active lease.
    encoded.append(before)
    return b'jpeg:' + before
  def send(data):
    output.append(data)
    if len(output) == 2:
      sent.set()
  pipe = module.AsyncJpegUsbPipeline(NS(encode_jpeg=encode, send_jpeg=send))
  pipe.start()
  try:
    borrowed = bytearray([1] * 16)
    pipe.submit_rgba_buffer(borrowed, 2, 2)
    assert entered.wait(2)
    borrowed[:] = bytes([99] * 16)  # Simulates raylib freeing/reusing its image.
    for i in range(2, 99):
      pipe.submit_rgba_buffer(bytes([i] * 16), 2, 2)
    assert sum(len(b) for b in pipe._buffers if b is not None) == 32
    release.set()
    assert sent.wait(2)
    assert encoded == [bytes([1] * 16), bytes([98] * 16)]
    assert output == [b'jpeg:' + pixels for pixels in encoded]
  finally:
    release.set()
    pipe.close()
  assert pipe._buffers == [None, None] and not pipe.is_running


def test_stale_queued_or_slow_encoded_frame_is_not_sent(monkeypatch):
  clock = [1.]
  monkeypatch.setattr(module, 'time', NS(monotonic=lambda: clock[0]))
  output = []
  def encode(*args):
    clock[0] += .151
    return b'jpeg'
  pipe = module.AsyncJpegUsbPipeline(NS(encode_jpeg=encode, send_jpeg=output.append))
  pipe.submit_rgba_buffer(bytes(16), 2, 2)
  # Process one queued frame without any wall-clock timeout/deadlock.
  take = pipe._take_pending
  calls = [0]
  def once():
    calls[0] += 1
    return take() if calls[0] == 1 else None
  pipe._take_pending = once
  pipe._run()
  assert not output and pipe._active_slot is None
  pipe.submit_rgba_buffer(bytes(16), 2, 2)
  clock[0] += .151
  calls[0] = 0
  pipe._run()
  assert not output


@pytest.mark.parametrize('width,height,size', [(0,2,0), (3,2,24), (1920,1920,16), (2,2,15)])
def test_size_limits_rejected_before_allocation(width, height, size):
  pipe = module.AsyncJpegUsbPipeline(NS())
  with pytest.raises(ValueError):
    pipe.submit_rgba_buffer(bytes(size), width, height)
  assert pipe._buffers == [None, None]


def test_error_releases_slots_and_rejects_more_work():
  pipe = module.AsyncJpegUsbPipeline(NS(encode_jpeg=lambda *args: (_ for _ in ()).throw(ValueError('encoder failed'))))
  pipe.start()
  pipe.submit_rgba_buffer(bytes(16), 2, 2)
  pipe._thread.join(2)
  with pytest.raises(RuntimeError):
    pipe.submit_rgba_buffer(bytes(16), 2, 2)
  assert pipe._active_slot is None and pipe._pending_rgba is None
  pipe.close()
  with pytest.raises(RuntimeError):
    pipe.submit_rgba(bytes(16), 2, 2)


def test_inflight_usb_owner_cannot_be_reopened(monkeypatch):
  entered, release = threading.Event(), threading.Event()
  def send(data):
    entered.set()
    assert release.wait(2)
  pipe = module.AsyncJpegUsbPipeline(NS(encode_jpeg=lambda *args: b'jpeg', send_jpeg=send))
  pipe.start()
  pipe.submit_rgba_buffer(bytes(16), 2, 2)
  assert entered.wait(2)
  try:
    with pytest.raises(RuntimeError):
      module.AsyncJpegUsbPipeline.ensure_idle()
    # Exercise the actual timeout branch without waiting five seconds.
    with monkeypatch.context() as patch:
      patch.setattr(pipe._thread, 'join', lambda **kwargs: None)
      with pytest.raises(RuntimeError, match='unsafe display cleanup'):
        pipe.close()
    release.set()
    pipe._thread.join(2)
    with pytest.raises(RuntimeError):
      module.AsyncJpegUsbPipeline.ensure_idle()  # Requires process restart after unsafe stop.
  finally:
    release.set()
    pipe.close()
  module.AsyncJpegUsbPipeline.ensure_idle()


def test_resize_does_not_change_active_slot():
  pipe = module.AsyncJpegUsbPipeline(NS())
  pipe.submit_rgba_buffer(bytes([1] * 16), 2, 2)
  active = pipe._take_pending()[0]
  pipe.submit_rgba_buffer(bytes([2] * 32), 4, 2)
  assert bytes(active) == bytes([1] * 16)
  assert pipe._buffers[pipe._active_slot] is active
  assert pipe._pending_rgba[0] == bytes([2] * 32)
