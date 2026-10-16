"""Opt-in asynchronous FunctionFS writes with Carrot's existing wire framing.

The io_submit implementation is ported from zoompilot/jetlink d26001b. It is
FunctionFS-specific: that driver copies source bytes before io_submit returns.
One submitter and one reaper use disjoint native arrays; close joins the reaper
before destroying the context. No frame skipping, old-result reuse or v3 switch.
"""
import errno
import logging
import os
import threading
import time

from jetlink.transport.base import LinkError
from jetlink.transport.ffs import FfsTransport, WRITE_TIMEOUT
from jetlink.transport.priority import background_thread

REQUEST_BYTES = 32 * 1024
DEPTH = 32  # at most 1 MiB of kernel-owned TX buffers


class AioCleanupError(LinkError):
  pass


class AioWriter:
  def __init__(self, fd, abort, factory=None):
    if factory is None:
      from jetlink.transport.aio_writer_native import Aio
      factory = Aio
    self.aio = factory(fd, DEPTH)
    self.abort = abort
    self.cv = threading.Condition()
    self.pending = {}  # token -> (expected bytes, absolute deadline)
    self.error = None
    self.stopping = False
    self.thread = threading.Thread(target=self._run, name='jetlink-aio-write', daemon=True)
    self.thread.start()

  def check(self):
    if self.error is not None:
      raise LinkError(self.error)
    if self.stopping:
      raise LinkError('AIO writer closing')

  def _fail(self, error):
    with self.cv:
      first = self.error is None
      if first:
        self.error = f'FunctionFS AIO write failed: {error}'
      self.cv.notify_all()
    if first:
      self.abort()  # disables endpoint; never resubmit a partially sent message

  def write(self, bufs, deadline):
    self.check()
    lengths = tuple(b.nbytes for b in bufs)
    plan = self.aio.layout(lengths, REQUEST_BYTES)
    bases = [self.aio.address(b)[0] for b in bufs]
    start = 0
    try:
      with self.cv:
        while start < plan.count:
          self.check()
          left = deadline - time.monotonic()
          if left <= 0:
            raise LinkError('send deadline expired')
          room = DEPTH - len(self.pending)
          if not room:
            self.cv.wait(left)
            continue
          count = min(room, plan.count - start)
          token = self.aio.sequence
          try:
            queued = self.aio.submit(plan, bases, start, count)
          except OSError as exc:
            if exc.errno == errno.EAGAIN and self.pending:
              self.cv.wait(min(left, .01))
              continue
            raise
          if not 0 < queued <= count:
            raise LinkError('invalid AIO submission count')
          total = sum(lengths)
          for i in range(queued):
            expected = min(REQUEST_BYTES, total - (start + i) * REQUEST_BYTES)
            self.pending[token + i] = (expected, deadline)
          start += queued
          self.cv.notify_all()
      return sum(lengths)
    except Exception as exc:
      self._fail(exc)
      raise LinkError(self.error) from exc

  def _run(self):
    background_thread()
    while True:
      with self.cv:
        while not self.pending and not self.stopping:
          self.cv.wait()
        if self.stopping and not self.pending:
          return
      try:
        events = self.aio.reap(1, .01)
        failure = None
        with self.cv:
          for token, result, result2 in events:
            request = self.pending.pop(token, None)
            # C3's 4.9 FunctionFS calls ki_complete(iocb, ret, ret).
            # New kernels leave res2 zero. Accept only those two successful
            # forms; negative, short, mismatched and unknown completions fail.
            if request is None or result != request[0] or result2 not in (0, result):
              failure = f'bad completion token={token}, result={result}, result2={result2}'
          if self.pending and time.monotonic() >= min(d for _, d in self.pending.values()):
            failure = 'completion deadline expired'
          self.cv.notify_all()
        if failure is not None:
          self._fail(failure)
      except Exception as exc:
        self._fail(exc)
        return  # pending requests remain: close refuses unsafe io_destroy

  def close(self):
    # Caller has disabled the endpoint first. io_destroy with outstanding
    # requests can sleep uninterruptibly, so never use it to cancel writes.
    with self.cv:
      self.stopping = True
      self.cv.notify_all()
    self.thread.join(1.)
    if self.thread.is_alive() or self.pending:
      raise AioCleanupError('AIO writes still owned by kernel; refusing rebind')
    if self.aio is not None:
      try:
        self.aio.close()
      except Exception as exc:
        raise AioCleanupError('Native AIO cleanup failed; refusing rebind') from exc
      self.aio = None


class AioFfsTransport(FfsTransport):
  def _prepare(self, mount, gadget):
    super()._prepare(mount, gadget)
    self._aio_write_enabled = os.getenv('CARROT_JETLINK_AIO_WRITE', '0') == '1'
    self._aio_writer = None

  def _write(self, bufs):
    if not self._aio_write_enabled:
      return super()._write(bufs)
    self._ensure_epfiles()
    self._write_budget = self._write_timeout(WRITE_TIMEOUT)
    if self._aio_writer is None:
      self._aio_writer = AioWriter(self.ep_in, self._abort_write)
      logging.getLogger('jetlink').info('Experimental AIO TX enabled: 32 KiB requests, 1 MiB bound')
    if not self._write_guard.arm(self._write_budget):
      raise LinkError('AIO submit watchdog already expired or closed')
    try:
      return self._aio_writer.write(bufs, time.monotonic() + self._write_budget)
    finally:
      if not self._write_guard.disarm():
        raise LinkError('AIO submit exceeded deadline; link abandoned')

  def recv(self, timeout=None):
    writer = getattr(self, '_aio_writer', None)
    if writer is not None:
      writer.check()
    result = super().recv(timeout)
    if writer is not None:
      writer.check()
    return result

  def _close_fds(self, names, reader):
    # Both close() and release_endpoints() disable the endpoint before here.
    if 'ep_in' in names and self._aio_writer is not None:
      try:
        self._aio_writer.close()
      except AioCleanupError:
        super()._close_fds(tuple(n for n in names if n != 'ep_in'), reader)
        raise
      self._aio_writer = None
    super()._close_fds(names, reader)
