"""UI-owned, bounded camera subscriptions; never recreate them just to change views."""
import time

from msgq.visionipc import VisionIpcClient, VisionStreamType

CONNECTION_RETRY_INTERVAL = 0.2


class CameraClientPool:
  def __init__(self, name: str):
    self._name = name
    self._clients: dict[VisionStreamType, VisionIpcClient] = {}
    self._reconnect: set[VisionStreamType] = set()
    self._discard_pending: set[VisionStreamType] = set()
    self._last_attempt: dict[VisionStreamType, float] = {}
    self._closed = False

  def select(self, stream: VisionStreamType) -> VisionIpcClient:
    if self._closed:
      raise RuntimeError("Camera client pool is closed")
    stream = VisionStreamType(stream)  # Bound the cache to the defined camera streams.
    if stream not in self._clients:
      self._clients[stream] = VisionIpcClient(self._name, stream, conflate=True)
    # An inactive subscriber may hold an old frame, even when still connected.
    # Drain its conflated queue once, then wait for a subsequent frame. No clock
    # comparison: camera BOOTTIME and the UI's MONOTONIC differ after suspend.
    self._discard_pending.add(stream)
    return self._clients[stream]

  def connected(self, stream: VisionStreamType) -> bool:
    client = self._clients[stream]
    return stream not in self._reconnect and client.is_connected() and bool(client.num_buffers)

  def connect(self, stream: VisionStreamType) -> bool:
    if self.connected(stream):
      return True
    now = time.monotonic()
    if now - self._last_attempt.get(stream, float('-inf')) < CONNECTION_RETRY_INTERVAL:
      return False
    self._last_attempt[stream] = now
    client = self._clients[stream]
    if not client.connect(False) or not client.num_buffers:
      return False
    self._reconnect.discard(stream)
    self._discard_pending.add(stream)
    return True

  def recv(self, stream: VisionStreamType):
    if not self.connected(stream):
      return None
    frame = self._clients[stream].recv(timeout_ms=0)
    if stream in self._discard_pending:
      self._discard_pending.discard(stream)
      return None
    return frame

  def reset(self) -> None:
    # Re-import buffers on the next onroad session, retaining the same sockets.
    self._reconnect.update(self._clients)
    self._discard_pending.update(self._clients)
    self._last_attempt.clear()

  def close(self) -> None:
    self._closed = True
    self._clients.clear()
    self._reconnect.clear()
    self._discard_pending.clear()
    self._last_attempt.clear()
