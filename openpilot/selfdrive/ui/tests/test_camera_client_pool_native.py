"""Real PC VisionIPC buffers/sockets; only the display/GL backend is doubled."""
import gc
import os
import struct
import uuid
from pathlib import Path

import pytest

vipc = pytest.importorskip('msgq.visionipc')
from openpilot.selfdrive.ui.tests.test_camera_client_pool import load_view, ROAD, WIDE


class Bench:
  def __init__(self, prefix):
    self.prefix, self.name = prefix, 'camera_pool_test'
    self.folder = Path('/dev/shm') / ('msgq_' + prefix)
    self.created = 0
    self.clients = []
    self.server = None
    self.view = None
    self.make_server()

  def make_server(self, width=16):
    self.server = vipc.VisionIpcServer(self.name)
    self.width = width
    self.data = bytes(width * 16 * 3 // 2)
    for stream in (ROAD, WIDE): self.server.create_buffers(stream, 3, width, 16)
    self.server.start_listener()

  def client(self, name, stream, conflate):
    self.created += 1
    return vipc.VisionIpcClient(self.name, stream, conflate)

  def send(self, number):
    for stream in (ROAD, WIDE): self.server.send(stream, self.data, frame_id=number)

  def counts(self):
    return [struct.unpack('<Q', (self.folder / f'visionipc_{self.name}_{int(s)}').read_bytes()[:8])[0] for s in (ROAD, WIDE)]

  def models(self):
    for stream in (ROAD, WIDE):
      for _ in range(3):
        client = vipc.VisionIpcClient(self.name, stream, True)
        assert client.connect(False)
        self.clients.append(client)

  def read_models(self, expected):
    missing = 0
    for client in self.clients:
      frame = client.recv(timeout_ms=0)
      if frame is None: missing += 1
      else: assert client.frame_id == expected
    return missing

  def close(self):
    if self.view:
      self.view.close()
      self.view = None
    self.clients.clear()
    self.server = None
    gc.collect()


@pytest.fixture
def ipc(monkeypatch):
  prefix = 'pv5_pool_test_' + uuid.uuid4().hex
  folder = Path('/dev/shm') / ('msgq_' + prefix)
  folder.mkdir()
  monkeypatch.setenv('OPENPILOT_PREFIX', prefix)
  bench = Bench(prefix)
  yield bench
  bench.close()
  # Only this fixture's two known queue files and exact private socket.
  for stream in (ROAD, WIDE):
    (folder / f'visionipc_{bench.name}_{int(stream)}').unlink(missing_ok=True)
  folder.rmdir()
  (Path('/tmp') / (prefix + '_visionipc_' + bench.name)).unlink(missing_ok=True)


def compare_switches(bench, source, count=1000):
  scene = load_view(source, bench.client, [10.], False)
  view = bench.view = scene.view
  assert view._ensure_connection()
  bench.models()
  missing = 0
  for i in range(count):
    stream = WIDE if i % 2 == 0 else ROAD
    number = 2 * i + 1
    bench.send(number)  # The model has a pending packet while UI subscribes/switches.
    view.switch_stream(stream)
    missing += bench.read_models(number)
    view._handle_switch()
    bench.send(number + 1)
    missing += bench.read_models(number + 1)
    view._handle_switch()
    assert view.stream_type == stream
  return dict(switches=count, ui_clients_created=bench.created, model_reads=count * 2 * 6,
              missing_model_reads=missing, final_readers_per_stream=bench.counts())


@pytest.mark.parametrize('source', ['onroad/cameraview.py', 'mici/onroad/cameraview.py'])
def test_native_repeated_switches_do_not_evict_model_readers(ipc, source):
  result = compare_switches(ipc, source)
  assert result['missing_model_reads'] == 0
  assert result['ui_clients_created'] == 2
  assert result['final_readers_per_stream'] == [4, 4]


@pytest.mark.parametrize('source', ['onroad/cameraview.py', 'mici/onroad/cameraview.py'])
def test_native_server_restart_and_offroad_keep_sockets_but_replace_buffers(ipc, source):
  scene = load_view(source, ipc.client, [10.], False)
  view = ipc.view = scene.view
  assert view._ensure_connection()
  ipc.send(1)
  assert view._clients.recv(ROAD) is None
  ipc.send(2)
  frame = view._clients.recv(ROAD)
  assert frame is not None and frame.width == 16
  view.frame = frame
  frame = None
  old_client = view.client
  ipc.server = None
  ipc.make_server(width=32)
  # Queue reset can discard the first publication. A subsequent one exposes new server_id.
  for number in range(3, 7):
    ipc.send(number)
    view._clients.recv(ROAD)
    if not old_client.is_connected(): break
  assert not old_client.is_connected()
  scene.clock[0] += .21
  assert view._ensure_connection()
  ipc.send(7)
  assert view._clients.recv(ROAD) is None
  ipc.send(8)
  fresh = view._clients.recv(ROAD)
  assert fresh.width == 32 and old_client.frame_id == 8
  fresh = None
  view.frame = None
  view._offroad_transition()
  assert view._ensure_connection()
  ipc.send(9)
  assert view._clients.recv(ROAD) is None
  ipc.send(10)
  assert view._clients.recv(ROAD).width == 32
  assert ipc.created == 1 and view.client is old_client


def test_native_inactive_queue_wrap_does_not_reset_models(ipc):
  from openpilot.selfdrive.ui.onroad.camera_client_pool import CameraClientPool
  pool = CameraClientPool(ipc.name)
  for stream in (ROAD, WIDE):
    pool.select(stream)
    assert pool.connect(stream)
  ipc.models()
  # Exceed 1 MiB queue capacity while UI is idle. Only the idle reader may reset.
  for number in range(1, 40001):
    ipc.send(number)
    assert ipc.read_models(number) == 0
  for stream in (ROAD, WIDE):
    pool.select(stream)
    assert pool.recv(stream) is None
  ipc.send(40001)
  assert ipc.read_models(40001) == 0
  for stream in (ROAD, WIDE): assert pool.recv(stream) is not None
  assert ipc.counts() == [4, 4]
  pool.close()
