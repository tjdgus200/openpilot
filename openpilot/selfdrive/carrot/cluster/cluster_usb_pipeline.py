from __future__ import annotations

from collections import deque
from collections.abc import Iterable
import threading
import time

from cluster_usb_display import TuringUsbDisplay


_worker_lock = threading.Lock()
_worker_owner = None


class AsyncJpegUsbPipeline:
    def __init__(self, usb_display: TuringUsbDisplay) -> None:
        self.usb_display = usb_display
        self.profile_enabled = bool(getattr(usb_display, "profile_enabled", False))
        self._condition = threading.Condition()
        self._pending_rgba: tuple[bytes, int, int] | None = None
        self._closing = False
        self._error: BaseException | None = None
        self._samples: deque[tuple[str, float]] = deque(maxlen=256)
        self._thread = threading.Thread(target=self._run, name="cluster-usb-jpeg", daemon=True)
        self._buffers = [None, None]
        self._active_slot = None
        self._pending_stamp = None
        self._active_deadline = None
        self._unsafe_shutdown = False

    @staticmethod
    def ensure_idle() -> None:
        with _worker_lock:
            if _worker_owner is not None and (_worker_owner.is_running or _worker_owner._unsafe_shutdown):
                raise RuntimeError('previous JPEG USB owner not retired; restart HUD process before reopening')

    @property
    def is_running(self) -> bool:
        return self._thread.is_alive()

    def start(self) -> None:
        global _worker_owner
        with _worker_lock:
            if _worker_owner is not None and (_worker_owner.is_running or _worker_owner._unsafe_shutdown):
                raise RuntimeError('previous JPEG USB worker is still active; refusing to reopen')
            _worker_owner = self
            try:
                self._thread.start()
            except BaseException:
                _worker_owner = None
                raise

    def submit_rgba(self, rgba: bytes, width: int, height: int) -> None:
        self.check_error()
        with self._condition:
            if self._closing:
                raise RuntimeError('JPEG pipeline is closed')
            self._pending_rgba = (rgba, width, height)
            self._pending_stamp = None
            self._condition.notify()

    def submit_rgba_buffer(self, rgba, width: int, height: int) -> None:
        """One ownership copy into two reusable slots; pending always means latest.

        The caller's raylib view dies on context exit. Never pass that view to
        the worker, resize an encoding slot, or queue a backlog of stale frames.
        """
        self.check_error()
        if (type(width) is not int or type(height) is not int or
                not 2 <= width <= 1920 or not 2 <= height <= 1920 or
                width % 2 or height % 2 or width * height > 1920 * 720):
            raise ValueError('invalid JPEG staging dimensions')
        source = memoryview(rgba).cast('B')
        size = width * height * 4
        if source.nbytes != size:
            raise ValueError('invalid JPEG staging size')
        with self._condition:
            if self._closing:
                raise RuntimeError('JPEG pipeline is closed')
            slot = 1 if self._active_slot == 0 else 0
            if self._buffers[slot] is None or len(self._buffers[slot]) != size:
                self._buffers[slot] = bytearray(size)
            memoryview(self._buffers[slot])[:] = source
            self._pending_rgba = self._buffers[slot], width, height
            self._pending_stamp = time.monotonic()
            self._condition.notify()

    def wait_for_capacity(self, timeout: float | None = None) -> bool:
        deadline = None if timeout is None else time.perf_counter() + max(0.0, timeout)
        with self._condition:
            while self._pending_rgba is not None and not self._closing and self._error is None:
                if deadline is None:
                    self._condition.wait()
                    continue
                remaining = deadline - time.perf_counter()
                if remaining <= 0.0:
                    return False
                self._condition.wait(timeout=remaining)
            if self._error is not None:
                raise RuntimeError("asynchronous USB JPEG pipeline failed") from self._error
            return self._pending_rgba is None

    def profile_samples(self) -> tuple[tuple[str, float], ...]:
        if not self.profile_enabled:
            return ()
        with self._condition:
            samples = tuple(self._samples)
            self._samples.clear()
        return samples

    def check_error(self) -> None:
        with self._condition:
            error = self._error
        if error is not None:
            raise RuntimeError("asynchronous USB JPEG pipeline failed") from error

    def close(self) -> None:
        global _worker_owner
        with self._condition:
            self._closing = True
            # Retire unsubmitted display data; an already-running USB write
            # retains its existing driver timeout and owns the display until done.
            if self._pending_stamp is not None:
                self._pending_rgba = None
                self._pending_stamp = None
            self._condition.notify()
        if self._thread.ident is not None:
            self._thread.join(timeout=5.0)
        if self.is_running:
            self._unsafe_shutdown = True
            raise RuntimeError('JPEG USB worker did not stop; refusing unsafe display cleanup')
        self._buffers = [None, None]
        with _worker_lock:
            if _worker_owner is self:
                _worker_owner = None

    def _add_sample(self, name: str, start_time: float) -> None:
        if not self.profile_enabled:
            return
        milliseconds = (time.perf_counter() - start_time) * 1000.0
        with self._condition:
            self._samples.append((name, milliseconds))

    def _add_samples(self, samples: Iterable[tuple[str, float]]) -> None:
        if not self.profile_enabled or not samples:
            return
        with self._condition:
            self._samples.extend(samples)

    def _take_pending(self) -> tuple[bytes, int, int] | None:
        with self._condition:
            while self._pending_rgba is None and not self._closing:
                self._condition.wait(timeout=0.1)
            if self._pending_rgba is None:
                return None
            pending = self._pending_rgba
            self._active_slot = next((i for i, buffer in enumerate(self._buffers) if buffer is pending[0]), None)
            self._active_deadline = self._pending_stamp + .15 if self._pending_stamp is not None else None
            self._pending_rgba = None
            self._pending_stamp = None
            self._condition.notify_all()
            return pending

    def _run(self) -> None:
        while True:
            pending = self._take_pending()
            if pending is None:
                return
            rgba, width, height = pending
            try:
                if self._active_deadline is not None and time.monotonic() >= self._active_deadline:
                    continue
                if self.profile_enabled:
                    self.usb_display.clear_profile_samples()
                profile_stage = time.perf_counter() if self.profile_enabled else 0.0
                jpeg = self.usb_display.encode_jpeg(rgba, width, height)
                self._add_sample("usb_async.encode_jpeg", profile_stage)

                with self._condition:
                    self._active_slot = None  # Encoder owns JPEG bytes now.
                    if self._active_deadline is not None and (self._closing or time.monotonic() >= self._active_deadline):
                        continue

                profile_stage = time.perf_counter() if self.profile_enabled else 0.0
                self.usb_display.send_jpeg(jpeg)
                self._add_sample("usb_async.send_jpeg", profile_stage)
                if self.profile_enabled:
                    self._add_samples(self.usb_display.profile_samples())
                if self.profile_enabled:
                    self.usb_display.clear_profile_samples()
            except BaseException as exc:
                with self._condition:
                    self._error = exc
                    self._pending_rgba = None
                    self._pending_stamp = None
                    self._condition.notify_all()
                return
            finally:
                with self._condition:
                    self._active_slot = None
                    self._condition.notify_all()
