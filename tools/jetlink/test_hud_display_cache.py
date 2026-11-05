"""PC invariants: caches are derived data, never a freshness authority."""
import base64
import ast
import json
import os
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

import hud_protocol as protocol
from cluster_battery import HeatmapLabels, heat_color, packet_from_metrics, panel_from_wire


@pytest.fixture
def reader(tmp_path, monkeypatch):
  clock = [10.]
  monkeypatch.setattr(protocol, 'HUD_PACKET', tmp_path / 'packet')
  monkeypatch.setattr(protocol, '_cached_snapshot', None)
  monkeypatch.setattr(protocol, '_snapshot_signature', None)
  monkeypatch.setattr(protocol, '_next_snapshot_read', 0.)
  monkeypatch.setattr(protocol, '_event_snapshot', None)
  monkeypatch.setattr(protocol, '_event_bytes', {})
  monkeypatch.setattr(protocol, 'time', NS(monotonic=lambda: clock[0]))
  def write(value, stamp=9.99):
    tmp = tmp_path / 'new'
    tmp.write_bytes(protocol.HEADER.pack(stamp) + json.dumps(value).encode())
    os.replace(tmp, protocol.HUD_PACKET)
  return clock, write


def test_one_json_parse_for_unchanged_atomic_packet(reader, monkeypatch):
  clock, write = reader
  write({'version': 1, 'params': {}})
  loads = protocol.json.loads
  calls = []
  monkeypatch.setattr(protocol.json, 'loads', lambda data: calls.append(1) or loads(data))
  first = protocol.read_snapshot()
  for i in range(20):
    clock[0] = 10. + .021 * i
    assert protocol.read_snapshot() is first
  assert len(calls) == 1
  # Expire even when the next file check is throttled.
  clock[0] = 10.489
  assert protocol.read_snapshot() is first
  clock[0] = 10.491
  assert protocol.read_snapshot() is None


def test_steering_advisory_expires_and_legacy_snapshot_clears_it(reader, monkeypatch):
  import hud

  clock, write = reader
  monkeypatch.setattr(hud, 'time', NS(monotonic=lambda: clock[0]))
  sm = hud.RemoteSubMaster([])
  record = {'version': 1, 'sent': 100.0, 'steering_warning': 2}
  write(record)
  sm.update()
  assert sm.steering_warning == 2

  clock[0] += .31
  sm.update()
  assert sm.steering_warning == 0
  write(record, clock[0])
  clock[0] += .03
  sm.update()
  assert sm.steering_warning == 2
  write({'version': 1, 'sent': 100.1}, clock[0])
  clock[0] += .03
  sm.update()
  assert sm.steering_warning == 0


@pytest.mark.parametrize('stamp', [float('nan'), float('inf'), 11., 9.])
def test_replacement_never_reuses_old_valid_cache(reader, stamp):
  clock, write = reader
  write({'version': 1, 'x': 1})
  assert protocol.read_snapshot()
  clock[0] += .021
  write({'version': 1, 'x': 2}, stamp)
  assert protocol.read_snapshot() is None
  clock[0] += .001
  assert protocol.read_snapshot() is None


@pytest.mark.parametrize('bad', [b'bad', b'[]', b'{"version":true}', b'x' * (protocol.MAX_HUD_BYTES + 1)])
def test_invalid_or_missing_packet_invalidates_cache(reader, bad):
  clock, write = reader
  write({'version': 1})
  assert protocol.read_snapshot()
  protocol.HUD_PACKET.write_bytes(protocol.HEADER.pack(10.) + bad)
  clock[0] += .021
  assert protocol.read_snapshot() is None
  assert protocol.read_snapshot() is None
  write({'version': 1}, clock[0])
  clock[0] += .021
  assert protocol.read_snapshot()
  protocol.HUD_PACKET.unlink()
  clock[0] += .021
  assert protocol.read_snapshot() is None


def test_equal_size_replacement_and_in_place_write_are_detected(reader):
  clock, write = reader
  write({'version': 1, 'x': 1})
  first = protocol.read_snapshot()
  write({'version': 1, 'x': 2})
  clock[0] += .021
  assert protocol.read_snapshot()[1]['x'] == 2
  assert protocol.read_snapshot() is not first
  protocol.HUD_PACKET.write_bytes(protocol.HEADER.pack(clock[0]) + b'{"version": 1, "x": 3}')
  clock[0] += .021
  assert protocol.read_snapshot()[1]['x'] == 3
  clock[0] = 9.
  assert protocol.read_snapshot() is None


def test_immutable_event_bytes_shared_but_replaced_on_content_or_snapshot_change(reader):
  _, write = reader
  def encoded(data):
    return base64.b64encode(data).decode()
  write({'version': 1, 'events': {'carState': encoded(b'first')}})
  value = protocol.read_snapshot()[1]
  a = protocol.snapshot_event_bytes(value, 'carState')
  assert a is protocol.snapshot_event_bytes(value, 'carState') and isinstance(a, bytes)
  value['events']['carState'] = encoded(b'second')
  assert protocol.snapshot_event_bytes(value, 'carState') == b'second'
  assert a == b'first'
  fresh = {'events': {'carState': encoded(b'third')}}
  assert protocol.snapshot_event_bytes(fresh, 'carState') == b'third'
  assert len(protocol._event_bytes) == 1


def test_heatmap_plan_exact_colors_text_incremental_and_expiry():
  cache = HeatmapLabels()
  for name, count, baseline in [('cell_voltages_v', 108, 3.96), ('module_temps_c', 15, 21)]:
    values = [None] * count
    for n in range(count):
      values[n] = baseline + (.02 if name == 'cell_voltages_v' else 1) * (n % 3)
      spread, cells = cache.get(name, values)
      reference = min(v for v in values if v is not None)
      assert spread == max(v for v in values if v is not None) - reference
      for i, (color, ink, number, text) in enumerate(cells):
        value = values[i]
        assert color == heat_color(value, reference, name)
        assert ink == ((16, 22, 28) if value is not None else (220, 220, 225))
        if name == 'cell_voltages_v':
          assert number == f'{i+1:03d}'
          assert text == (f'{value:.2f}' if value is not None else '--')
        else:
          assert text == (f'T{i+1:02d}  {value:.0f}°C' if value is not None else f'T{i+1:02d}  --')
      assert cache.get(name, list(values))[1] is cells
    packet = packet_from_metrics({name: (values, 100.)}, 100.)
    assert panel_from_wire(packet, 60.).value(name)
    expired = panel_from_wire(packet, 60.001).value(name)
    assert expired is None
    assert all(c[0] == (85, 90, 100) and c[-1].endswith('--') for c in cache.get(name, expired)[1])
  assert len(cache._plans) == 2


def test_shared_bytes_never_share_mutable_cereal_builders(monkeypatch):
  from openpilot.cereal import log
  import hud
  event = log.Event.new_message(carState={'vEgo': 12.})
  value = {'sent': 10., 'valid': {'carState': True}, 'alive': {'carState': True},
           'received': {'carState': 10.}, 'mono': {'carState': 100},
           'events': {'carState': base64.b64encode(event.to_bytes()).decode()}}
  monkeypatch.setattr(hud, 'read_snapshot', lambda: (10., value))
  first, second = hud.RemoteSubMaster(['carState']), hud.RemoteSubMaster(['carState'])
  first.update()
  second.update()
  first['carState'].vEgo = 99.
  assert second['carState'].vEgo == 12.
  monkeypatch.setattr(hud, 'read_snapshot', lambda: None)
  second.update()
  assert not second.alive['carState'] and not second.valid['carState']


def test_cached_heatmap_emits_same_graphics_commands_as_uncached_reference():
  # A reference loop independent of HeatmapLabels checks every draw, label,
  # color and position in the production renderer. No GPU/image claim.
  source = Path(__file__).resolve().parents[2] / 'openpilot/selfdrive/carrot/cluster/cluster_renderer.py'
  tree = ast.parse(source.read_text(encoding='utf-8'))
  method = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == '_draw_charging_detail')
  calls = []
  env = {'ClusterUiState': object, 'BatteryPanelData': object, 'HeatmapLabels': HeatmapLabels,
         'CAMERA_BACKGROUND_W': 1400, 'DESIGN_HEIGHT': 480,
         'rl_color': lambda color: color, 'rl': NS(draw_rectangle=lambda *a: calls.append(('rect', a)))}
  exec(compile(ast.fix_missing_locations(ast.Module(body=[method], type_ignores=[])), str(source), 'exec'), env)
  for language in ('en', 'ko'):
    renderer = NS(language=language, _driving_panel_offset_design_x=lambda: 0,
                  _current_theme=lambda: NS(panel_bg=1, faint=2, text=3, muted=4),
                  _rounded_rect=lambda *a: calls.append(('panel', a)),
                  _draw_text=lambda *a: calls.append(('text', a)), _ellipsize_text=lambda text, *a: text)
    for offset in (0., .02):
      fields = {'module_temps_c': ([21]*14 + [22], 100.),
                'cell_voltages_v': ([3.96+offset]*37 + [None]*71, 100.)}
      for elapsed in (0., 59., 61.):
        data = panel_from_wire(packet_from_metrics(fields, 100.), elapsed)
        calls.clear()
        env['_draw_charging_detail'](renderer, NS(battery=data))
        # Repeated plans must preserve the complete graphics trace.
        expected = list(calls)
        calls.clear()
        renderer._heatmap_labels = HeatmapLabels()
        env['_draw_charging_detail'](renderer, NS(battery=data))
        assert calls == expected
        rectangles = [args for kind, args in calls if kind == 'rect']
        assert len(rectangles) == 123
        for name, start, count in [('module_temps_c', 0, 15), ('cell_voltages_v', 15, 108)]:
          values = data.value(name)
          present = [v for v in values if v is not None] if values else []
          reference = min(present) if present else None
          assert [rect[-1] for rect in rectangles[start:start+count]] == [
            heat_color(values[i] if values else None, reference, name) for i in range(count)]
