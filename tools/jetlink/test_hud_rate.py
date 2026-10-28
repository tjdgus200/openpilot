"""PC checks of cadence/admission/framing; no physical USB timing claims."""
import base64
import json
import socket
import struct
import threading
import time
from types import SimpleNamespace as NS

import pytest

from hud_rate import FrameRate, IdleBudget, RATES, rate, request_ready, selection


@pytest.mark.parametrize('value', [None, True, False, 0, 1, 11, 31, 'nan', float('inf'), b''])
def test_invalid_rates_keep_ten(value):
  assert rate(value) == 10


def test_web_indices_map_to_exact_five_rates():
  assert [RATES[selection(value)] for value in range(5)] == [10,15,20,25,30]
  assert all(selection(value) == 0 for value in (-1,5,30,None,True,'bad'))


def test_only_new_receiver_negotiates_higher_snapshot_rates():
  from hud_wifi import publisher, USB_CAPABILITY
  from hud_rate import CAPABILITY
  calls = []
  def legacy(**kwargs):
    calls.append(kwargs)
    return 'publisher'
  peer = {'carrot_host':'jetson', USB_CAPABILITY:True,'carrot_hud_v1':True,
          'carrot_navi_v1':True,'carrot_navi_pump_v1':True,CAPABILITY:True}
  assert publisher(None,peer,legacy) == 'publisher'
  assert calls == [{'navi':True,'nav_map':True,'rate_control':True}]


@pytest.mark.parametrize('fps', RATES)
def test_real_snapshot_builder_cadence_is_not_limited_to_model_twenty_hz(monkeypatch, fps):
  import hud_protocol as protocol
  import openpilot.common.jetlink_status as status
  clock = [100.]
  car = NS(gearShifter='park', vEgo=0.)
  class SM:
    updated = dict.fromkeys(protocol.SERVICES, False)
    valid = alive = {'carState': True}
    recv_time = {'carState': 100.}
    logMonoTime = {'carState': 100_000_000_000}
    def update(self, _):
      self.recv_time['carState'] = clock[0]
    def __getitem__(self, _):
      return car
  builder = object.__new__(protocol.SnapshotBuilder)
  builder.sm = SM()
  builder.next_send, builder.next_params = 0., 200.
  builder.cached = {}
  builder.settings = {'IsOnroad': 'MQ=='}
  builder.host_release = builder.nav_map = None
  builder.target_fps, builder.requested_fps, builder.rate_control = 10, fps, True
  builder.network = NS(address=lambda: '')
  builder.camera_previews = dict
  builder.battery_snapshot = lambda _: None
  monkeypatch.setattr(protocol.time, 'monotonic', lambda: clock[0])
  monkeypatch.setattr(status, 'badge', lambda: None)
  stamps = []
  # Drive the actual producer at its next deadline, without a catch-up burst.
  for _ in range(fps * 3):
    packet = json.loads(builder.packet())
    assert packet['events'] == {}
    assert int(base64.b64decode(packet['params']['JetsonHudFps'])) == RATES.index(fps)
    stamps.append(clock[0])
    clock[0] = builder.next_send
  assert stamps[-1] - stamps[0] == pytest.approx(3 - 1/fps)
  clock[0] += 2.
  assert builder.packet() is not None
  assert builder.packet() is None  # No queued/replayed snapshots after a stall.
  assert builder.next_send == pytest.approx(clock[0] + 1/fps)
  builder.requested_fps = 30
  car.gearShifter = 'drive'
  builder.target_fps = 10
  clock[0] = builder.next_send
  assert json.loads(builder.packet())['params']['JetsonHudFps'] == 'NA=='


def test_rate_metadata_is_private_and_reconnect_drops_old_or_oversized_packets(tmp_path, monkeypatch):
  import hud_protocol as p
  monkeypatch.setattr(p.time, 'monotonic', lambda: 100.)
  publisher = object.__new__(p.Publisher)
  publisher.path, publisher.last_sent, publisher.rate_control = tmp_path/'packet', 0., True
  publisher.path.write_bytes(p.HEADER.pack(100.) + p.PUBLISHER_RATE.pack(p.RATE_MAGIC, 30) + b'{"version":1}')
  assert publisher.packet() == b'{"version":1}'
  assert publisher.target_fps == 30
  assert publisher.packet() is None
  publisher.last_sent = 0.
  publisher.path.write_bytes(p.HEADER.pack(99.) + p.PUBLISHER_RATE.pack(p.RATE_MAGIC, 30) + b'old')
  assert publisher.packet() is None
  publisher.path.write_bytes(p.HEADER.pack(100.) + p.PUBLISHER_RATE.pack(p.RATE_MAGIC, 30) + b'x'*(96*1024+1))
  assert publisher.packet() is None
  publisher.path.write_bytes(p.HEADER.pack(100.) + b'legacy')
  assert publisher.packet() == b'legacy'


def test_idle_admission_reserves_next_request_and_backs_off_after_slow_usb():
  budget = IdleBudget()
  assert budget.allowed(1.)
  budget.model_started(1.)
  assert budget.allowed(1.030)
  assert not budget.allowed(1.045)
  budget.observe(1.031, {'display_hud_send_ms': 3})
  assert not budget.allowed(2.)
  assert budget.allowed(4.)
  budget.model_started(4.)
  budget.completed(4.047, .047)
  assert not budget.allowed(4.08)
  budget.model_started(7.)
  budget.model_started(7.033)
  assert budget.period == pytest.approx(.033)
  assert not budget.allowed(7.062)


def test_ipc_ready_check_does_not_consume_or_interrupt_partial_packets():
  from openpilot.selfdrive.modeld.jetlink.link import PacketReader
  a, b = socket.socketpair()
  try:
    a.settimeout(.5)
    assert not request_ready(a, 0.)
    b.sendall(struct.pack('<I', 5)[:2])
    assert request_ready(a, 0.)
    def remainder():
      time.sleep(.04)
      b.sendall(struct.pack('<I', 5)[2:] + b'frame')
    worker = threading.Thread(target=remainder)
    worker.start()
    assert bytes(PacketReader(32).receive(a)) == b'frame'
    worker.join(timeout=1.)
    assert not worker.is_alive()
    assert a.gettimeout() == .5
  finally:
    a.close()
    b.close()


def test_counter_does_not_store_frames():
  meter = FrameRate()
  for n in range(31):
    meter.frame(n/30)
  assert meter.sample(1.) == 30.
  assert set(vars(meter)) == {'started', 'count', 'value'}


def test_fps_comparisons_never_reuse_old_codec_trial(tmp_path):
  from hud_quality_compare import Comparison
  path = tmp_path / 'rates.json'
  path.write_text(json.dumps({'version':1,'session':'same','rows':[], 'kind':'codec'}))
  comparison = Comparison(4,4,path=path,kind='fps-q92',clock=lambda:0.)
  assert comparison.cached_session is None
  assert comparison.update(None)['kind'] == 'fps-q92'


@pytest.mark.parametrize('fps', RATES)
def test_bsd_dwell_time_does_not_speed_up_at_higher_fps(fps):
  from hud_bsd import WarningPulse
  pulse = WarningPulse()
  frames = [pulse.update((True,True), (True,False), now=n/fps) for n in range(fps*4)]
  assert all(frame[1] == 255 for frame in frames)
  starts = sum(frame[0] == 255 and (n == 0 or frames[n-1][0] == 0) for n, frame in enumerate(frames))
  assert starts == 10  # Four seconds at 2.5Hz, independent of rendering rate.


def test_heatmap_resolves_photo_values_without_normalizing_small_spreads_to_red():
  from cluster_battery import heat_color
  for name, low, high, large in [('module_temps_c',21.,22.,29.), ('cell_voltages_v',3.96,3.98,4.08)]:
    a,b,c = (heat_color(v, low, name) for v in (low,high,large))
    assert a != b != c
    assert a == (55,205,115)
    assert b == pytest.approx((245,215,65),abs=1)
    assert c == (240,65,65)
    colors = [heat_color(low+delta,low,name) for delta in (.0001,.001,.005,.01)]
    assert all(color != a for color in colors)
