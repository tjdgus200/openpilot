import json
from types import SimpleNamespace as NS

import capture_bms_visible_can as capture


def event(*frames, valid=True):
  return NS(valid=valid, can=[NS(src=b, address=a, dat=d) for b, a, d in frames])


def test_non_candidate_ids_are_visible_on_each_receive_bus_without_claiming_bms():
  c = capture.Capture()
  c.consume(event((0, 0x123, b'\x01'), (1, 0x123, b'\x02\x03'), (2, 0x456, b'\x04')), 0)
  d = json.loads(json.dumps(c.result(60, False)))
  assert not d['candidate_counts'] and not d['samples'] and not d['temperature_verified']
  assert [(i['bus'], i['address'], i['samples'][0]['data_hex']) for i in d['id_inventory']] == [
    (0, '0x123', '01'), (1, '0x123', '0203'), (2, '0x456', '04'),
  ]


def test_transmit_echoes_and_invalid_events_never_become_receive_evidence():
  c = capture.Capture()
  c.consume(event((128, 0x7EC, b'echo'), (129, 0x123, b'echo')), 0)
  c.consume(event((1, 0x7EC, b'invalid'), valid=False), 1)
  d = c.result(60, False)
  assert d['invalid_events_excluded'] == 1
  assert not d['received_bus_counts'] and not d['id_inventory'] and not d['candidate_counts']


def test_samples_span_the_capture_instead_of_filling_at_start():
  c = capture.Capture()
  for t in range(120):
    c.consume(event((2, 0x123, bytes([t]))), t)
  item = c.result(120, False)['id_inventory'][0]
  assert item['count'] == 120 and item['length_counts'] == {1: 120}
  assert [(s['elapsed_s'], s['data_hex']) for s in item['samples']] == [(0, '00'), (20, '14'), (40, '28')]


def test_id_limit_does_not_hide_candidate_responses(monkeypatch):
  monkeypatch.setattr(capture, 'MAX_ID_KEYS', 2)
  c = capture.Capture()
  c.consume(event(*[(0, a, b'\x01') for a in range(1, 9)]), 0)
  c.consume(event((2, 0x7EC, b'\x03\x62\x01\x01')), 1)
  d = c.result(60, False)
  assert len(d['id_inventory']) == 2 and d['untracked_id_frames'] == 7
  assert d['candidate_counts'] == [{'bus': 2, 'address': '0x7EC', 'count': 1}]
  assert d['samples'][0]['data_hex'] == '03620101'


def test_malformed_oversize_data_is_not_copied_into_inventory():
  c = capture.Capture()
  c.consume(event((0, 0x123, bytes(65)), (0, 0x20000000, b'a')), 0)
  assert not c.result(60, False)['id_inventory']


def test_inventory_payload_storage_is_bounded_even_for_high_frame_counts():
  c = capture.Capture()
  frames = event(*[(0, a, bytes(64)) for a in range(capture.MAX_ID_KEYS + 10)])
  for t in range(0, 120, 5):
    c.consume(frames, t)
  d = c.result(120, False)
  assert len(d['id_inventory']) == capture.MAX_ID_KEYS
  assert sum(len(i['samples']) for i in d['id_inventory']) == capture.MAX_ID_KEYS * capture.ID_SAMPLES
  assert d['untracked_id_frames'] == 24 * 10
  assert len(json.dumps(d)) < 1024 * 1024
