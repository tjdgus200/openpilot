"""Content equivalence, live gates and bounded optional HUD allocation on a PC."""
import base64
import copy
import json
import random
import sys
from types import SimpleNamespace as NS

import pytest

import hud_protocol as protocol
from openpilot.common import jetlink_status as status
from openpilot.selfdrive.carrot import carrot_navi_control as navi


def reference_encode(record):
  # Pre-optimization wire behavior: test oracle deliberately encodes each stage.
  def encode():
    return json.dumps(record, separators=(',', ':')).encode()
  data = encode()
  if len(data) > 96 * 1024:
    record.pop('indicator_display', None)
    record.pop('signal_display', None)
    data = encode()
  if len(data) > 96 * 1024:
    record['cameras'] = {}
    data = encode()
  if len(data) > 96 * 1024:
    record.pop('nav_map_preview', None)
    data = encode()
  if len(data) > 96 * 1024:
    record['events'] = {k:v for k,v in record['events'].items() if k != 'navRoute'}
    data = encode()
  if len(data) > 96 * 1024:
    raise ValueError('HUD snapshot exceeds bounded display allocation')
  return data


def outcome(encoder, value):
  try:
    return encoder(value), None
  except ValueError as error:
    return None, str(error)


def test_optional_wire_and_mutation_equivalence_across_size_boundaries():
  rng = random.Random(1971)
  fixtures = []
  for _ in range(220):
    value = {'events': {'carState': 'A' * rng.randrange(150, 110000),
                        'navRoute': 'R' * rng.randrange(35000)},
             'params': {'unicode': '한글"\\\n'}, 'sent': 101.25,
             'received': {'carState': 101.}, 'alive': {'carState': True},
             'valid': {'carState': True}}
    for key in ('indicator_display', 'signal_display', 'cameras', 'nav_map_preview'):
      if rng.randrange(4):
        value[key] = {key: '한' * rng.randrange(5000)}
    fixtures.append(value)
  # Equal limit must pass, one byte over must follow the same pruning order.
  for key in ('indicator_display', 'signal_display', 'cameras', 'nav_map_preview', 'navRoute'):
    for delta in (-1, 0, 1):
      value = {'events': {'carState': 'essential', 'navRoute': 'route'},
               'cameras': {'road': 'camera'}, 'nav_map_preview': 'map',
               'indicator_display': 'indicator', 'signal_display': 'signal', 'pad': ''}
      value['pad'] = 'x' * (96 * 1024 + delta - len(json.dumps(value, separators=(',', ':')).encode()))
      if key == 'navRoute':
        value['events'].pop(key)
      else:
        value.pop(key)
      value['pad'] += 'x' * (96 * 1024 + delta - len(json.dumps(value, separators=(',', ':')).encode()))
      assert len(json.dumps(value, separators=(',', ':')).encode()) == 96 * 1024 + delta
      fixtures.append(value)
  for value in fixtures:
    expected, actual = copy.deepcopy(value), copy.deepcopy(value)
    assert outcome(protocol.encode_snapshot, actual) == outcome(reference_encode, expected)
    assert actual == expected


def test_optional_eviction_never_mutates_cached_service_events():
  events = {'carState': 'essential', 'navRoute': 'x' * (100 * 1024)}
  record = {'events': events}
  assert json.loads(protocol.encode_snapshot(record)) == {'events': {'carState': 'essential'}, 'cameras': {}}
  assert len(events['navRoute']) == 100 * 1024
  assert record['events'] is not events


def test_small_packet_encodes_once_and_large_packet_encodes_whole_record_at_most_three_times(monkeypatch):
  original, whole_calls = protocol.json.dumps, []
  record = {'events': {'carState': 'essential'}, 'cameras': {}}
  def dumps(value, **kwargs):
    if value is record:
      whole_calls.append(1)
    return original(value, **kwargs)
  monkeypatch.setattr(protocol.json, 'dumps', dumps)
  protocol.encode_snapshot(record)
  assert len(whole_calls) == 1
  whole_calls.clear()
  record.update(indicator_display='i' * 20000, signal_display='s' * 20000,
                cameras={'road': 'c' * 40000}, nav_map_preview='m' * 60000)
  protocol.encode_snapshot(record)
  assert len(whole_calls) == 3


@pytest.mark.parametrize('state,age,handshake,preparing,expected', [
  ('ready', 2.999, True, True, True), ('ready', 3., True, True, False),
  ('connecting', 7.999, True, True, True), ('connecting', 8., True, True, False),
  ('connecting', 3., False, True, False), ('updating', 34.999, True, True, True),
  ('updating', 35., True, True, False), ('updating', 3., True, False, False),
  ('ready', -.001, True, True, False),
])
def test_shared_status_keeps_each_consumers_original_lease(state, age, handshake, preparing, expected):
  value = {'updated': 100. - age, 'state': state, 'host_update': {'state': 'downloading'}}
  assert bool(status.fresh_value(value, 100., handshake=handshake, preparing=preparing)) == expected


def test_packet_status_reads_two_files_once_but_never_renews_health_or_model(monkeypatch):
  now, reads = [100.], []
  link = {'updated': 100., 'state': 'ready', 'peer': {'carrot_host': 'jetson'},
          'telemetry_updated': 100., 'telemetry': {
            'carrot_hud_connected': True, 'carrot_health': {'age_s': 0., 'severity': 'ok'}}}
  model = {'updated': 100., 'active': True}
  def read(path):
    reads.append(path)
    return copy.deepcopy(link if path == status.LINK_STATUS else model)
  monkeypatch.setattr(status, '_read', read)
  monkeypatch.setattr(status.time, 'monotonic', lambda: now[0])
  packet_status = status.read_status()
  assert status.badge(packet_status) == ('jetSON', 'active')
  assert status.diagnostics(packet_status)['fresh']
  assert len(reads) == 2
  now[0] += 3.
  assert status.badge(packet_status) == ('jetSON ERROR', 'error')
  assert not status.diagnostics(packet_status)['fresh']
  assert len(reads) == 2
  link.update(updated=now[0], state='retrying', error='transport failure')
  assert status.diagnostics(status.read_status())['reason'] == 'transport failure'
  assert len(reads) == 4


def test_shared_status_camera_admission_rechecks_strict_lease_and_closes_worker(monkeypatch):
  now, created, closed = [100.], [], []
  def camera():
    created.append(1)
    return NS(latest={'road': 'image'}, close=lambda: closed.append(1))
  monkeypatch.setitem(sys.modules, 'hud_camera', NS(CameraPublisher=camera))
  monkeypatch.setattr(protocol.time, 'monotonic', lambda: now[0])
  builder = object.__new__(protocol.SnapshotBuilder)
  builder.camera = None
  builder.params = NS(get_int=lambda _: 0, get_bool=lambda _: True)
  link = {'updated': 100., 'state': 'ready', 'peer': {'carrot_host': 'jetson'},
          'telemetry_updated': 100., 'telemetry': {'carrot_hud_connected': True}}
  snapshot = link, {}
  assert builder.camera_previews(snapshot) == {'road': 'image'} and created == [1]
  now[0] = 103.
  assert builder.camera_previews(snapshot) == {} and closed == [1]
  # The badge's extended handshake/preparation lease must never admit a camera.
  link.update(state='connecting')
  assert status.fresh_value(link, now[0], handshake=True)
  assert builder.camera_previews(snapshot) == {} and created == [1]
  link.update(updated=now[0], telemetry_updated=now[0])
  assert builder.camera_previews(snapshot) == {'road': 'image'} and created == [1, 1]
  link['telemetry']['carrot_hud_connected'] = False
  assert builder.camera_previews(snapshot) == {} and closed == [1, 1]


def message():
  return {'schemaVersion': 1, 'connected': True, 'sessionId': 'first',
          'speed': {'meta': {'present': True, 'sequence': 1}, 'roadLimitValid': True, 'roadLimitKph': 80}}


class SubMaster:
  def __init__(self):
    self.data = message()
    self.alive, self.valid, self.updated = ({'carrotNavi': True} for _ in range(3))
    self.recv_frame, self.logMonoTime = {'carrotNavi': 1}, {'carrotNavi': 100}
  def __getitem__(self, name):
    assert name == 'carrotNavi'
    return self.data


def test_navi_parsed_once_per_received_generation_and_owned_immutable(monkeypatch):
  original, calls = navi.parse_carrot_navi_control, []
  def parse(value):
    calls.append(1)
    return original(value)
  monkeypatch.setattr(navi, 'parse_carrot_navi_control', parse)
  reader, sm = navi.CarrotNaviReader(), SubMaster()
  first = reader.read(sm)
  sm.updated['carrotNavi'] = False
  assert reader.read(sm) is first and len(calls) == 1
  sm.data['speed']['roadLimitKph'] = 30
  assert first.speed.road_limit_kph == 80  # No borrowed mutable/cereal views.
  sm.recv_frame['carrotNavi'] += 1
  assert reader.read(sm).speed.road_limit_kph == 30 and len(calls) == 2
  sm.logMonoTime['carrotNavi'] += 1
  reader.read(sm)
  assert len(calls) == 3
  reader.read(SubMaster())
  assert len(calls) == 4  # A different subscriber cannot reuse the generation.


@pytest.mark.parametrize('gate', ['alive', 'valid'])
def test_navi_disconnect_is_immediate_and_reconnect_reparses_same_generation(gate):
  reader, sm = navi.CarrotNaviReader(), SubMaster()
  assert reader.read(sm).session_id == 'first'
  getattr(sm, gate)['carrotNavi'] = False
  assert reader.read(sm) is None
  sm.data['sessionId'] = 'reconnected'
  getattr(sm, gate)['carrotNavi'] = True
  assert reader.read(sm).session_id == 'reconnected'
  sm.data['connected'] = False
  sm.recv_frame['carrotNavi'] += 1
  assert reader.read(sm) is None


def test_navi_generation_missing_uses_uncached_legacy_path():
  reader, sm = navi.CarrotNaviReader(), SubMaster()
  del sm.recv_frame
  assert reader.read(sm).session_id == 'first'
  sm.data['sessionId'] = 'changed'
  assert reader.read(sm).session_id == 'changed'
  assert reader.read(NS(alive={'carrotNavi': False}, valid={'carrotNavi': True})) is None


@pytest.mark.parametrize('fps', [10, 20, 30])
def test_cruise_camera_model_events_survive_packet_pressure_and_state_transitions(monkeypatch, fps):
  """Real cereal envelopes through packet(); synthetic vehicle and camera input."""
  from openpilot.cereal import log
  from test_hud_fps_transition import builder_at_realistic_receive_time
  builder, clock, selected, _ = builder_at_realistic_receive_time(monkeypatch)
  selected[0] = protocol.RATES.index(fps)
  # Exercise the actual status reader rather than the older fixture's badge double.
  monkeypatch.undo()
  monkeypatch.setattr(protocol.time, 'monotonic', lambda: clock[0])
  reads = []
  def read(path):
    reads.append(path)
    return {}
  monkeypatch.setattr(status, '_read', read)
  class SM:
    def __init__(self):
      self.updated = dict.fromkeys(protocol.SERVICES, False)
      self.valid, self.alive, self.recv_time, self.logMonoTime = {}, {}, {}, {}
      self.events = {}
    def update(self, _):
      clock[0] += .002
    def __getitem__(self, name):
      return getattr(self.events[name], name)
  builder.sm = sm = SM()
  camera_valid = True
  for frame, (enabled, gear, speed, brake) in enumerate([
      (False, 'park', 0., False), (False, 'drive', 0., False),
      (True, 'drive', 12., False), (False, 'drive', 10., True),
      (True, 'drive', 10., False), (False, 'drive', 0., True),
      (False, 'reverse', 0., False), (False, 'park', 0., False)]):
    camera_valid = frame != 4
    payloads = {
      'carState': {'gearShifter': gear, 'vEgo': speed, 'brakePressed': brake,
                   'cruiseState': {'enabled': enabled}, 'leftBlindspot': frame == 2},
      'carControl': {'enabled': enabled}, 'modelV2': {'frameId': frame},
      'roadCameraState': {'frameId': frame}, 'wideRoadCameraState': {'frameId': frame},
    }
    for name, value in payloads.items():
      valid = camera_valid if name.endswith('CameraState') else True
      sm.events[name] = log.Event.new_message(logMonoTime=frame * 50000000 + 1, valid=valid, **{name: value})
      sm.updated[name], sm.valid[name], sm.alive[name] = True, valid, valid
      sm.recv_time[name], sm.logMonoTime[name] = clock[0], frame * 50000000 + 1
    builder.cached['navRoute'] = 'R' * 120000  # Optional overflow must preserve core events.
    before = len(reads)
    record = json.loads(builder.packet())
    assert len(reads) - before == 2
    assert 'navRoute' not in record['events']
    assert builder.target_fps == fps
    for name in payloads:
      raw = base64.b64decode(record['events'][name])
      assert raw == sm.events[name].to_bytes()
      with log.Event.from_bytes(raw) as decoded:
        assert decoded.valid == sm.valid[name]
        assert decoded.logMonoTime == sm.logMonoTime[name]
    assert record['valid']['roadCameraState'] == camera_valid
    clock[0] = builder.next_send
