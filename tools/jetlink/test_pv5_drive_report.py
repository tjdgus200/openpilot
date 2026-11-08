"""Offline diagnostics must expose gaps instead of inventing timing/accuracy."""
import json
import struct

import pytest

from pv5_drive_report import DriveReport, Metric, compare_reports, main, read_events, tegrastats_metrics


def tracking(report, now, *, valid=True, override=False, requested=20., applied=15., measured=13., reason='lead'):
  report.feed('carParams', now - .05, {'carFingerprint': 'KIA_PV5'})
  report.feed('carState', now - .02, {'canValid': True, 'gearShifter': 'drive', 'vEgo': 10.,
                                    'steeringAngleDeg': measured, 'steeringPressed': override})
  report.feed('carControl', now - .01, {'latActive': True, 'actuators': {'steeringAngleDeg': requested}})
  report.feed('carOutput', now - .009, {'actuatorsOutput': {'steeringAngleDeg': applied}})
  def line(y):
    return {'x': [0., 30.], 'y': [y, y]}
  report.feed('modelV2', now - .008, {'frameId': 1, 'frameIdExtra': 2, 'timestampEof': int((now - .04) * 1e9),
                                    'modelExecutionTime': .025, 'laneLineProbs': [0., .95, .9, 0.],
                                    'laneLines': [line(-5), line(-1.8), line(1.8), line(5)], 'position': line(.1)})
  report.feed('controlsState', now, {'pv5LaneCenterStatus': 'waiting', 'pv5LaneCenterWaitReason': reason}, valid)


def test_report_separates_path_command_tracking_and_capped_center_duration():
  report = DriveReport()
  tracking(report, 10.)
  tracking(report, 10.1, reason='blindspot')
  tracking(report, 15., reason='laneQuality')
  result = report.result()
  assert result['center']['seconds'] == pytest.approx({'lead': .1, 'blindspot': .2})
  assert result['coverage']['control_gap_seconds'] == pytest.approx(4.7)
  metrics = result['metrics']
  assert metrics['model_path_to_lane_center_m']['mean'] == pytest.approx(-.1)
  assert metrics['request_minus_applied_deg']['mean'] == 5.
  assert metrics['applied_minus_measured_deg']['mean'] == 2.
  assert metrics['main_eof_to_controls_publish_ms']['mean'] == pytest.approx(40.)
  assert metrics['model_to_controls_publish_ms']['mean'] == pytest.approx(8.)
  assert metrics['model_call_ms']['mean'] == 25.
  assert result['optional_runtime_phases'] == {}
  json.dumps(result, allow_nan=False)


def test_stale_invalid_override_non_pv5_do_not_produce_tracking_samples():
  report = DriveReport()
  tracking(report, 10., valid=False)
  tracking(report, 10.1, override=True)
  report.feed('carParams', 10.15, {'carFingerprint': 'OTHER'})
  report.feed('controlsState', 10.2, {'pv5LaneCenterStatus': 'correcting'})
  assert 'abs_tracking_deg' not in report.result()['metrics']
  report.feed('carParams', 10.21, {'carFingerprint': 'KIA_PV5'})
  report.feed('controlsState', 12., {'pv5LaneCenterStatus': 'correcting'})
  assert 'abs_tracking_deg' not in report.result()['metrics']


def test_exact_camera_frame_join_and_optional_phase_aggregates():
  report = DriveReport()
  report.feed('roadCameraState', 9.97, {'frameId': 1, 'timestampSof': 9_940_000_000, 'timestampEof': 9_960_000_000})
  report.feed('wideRoadCameraState', 9.971, {'frameId': 99})
  tracking(report, 10.)
  result = report.result()
  assert result['coverage']['roadCameraState.matched'] == 1
  assert result['coverage']['wideRoadCameraState.unmatched'] == 1
  assert result['metrics']['roadCameraState.sof_to_model_ms']['mean'] == pytest.approx(52.)
  for mean, maximum, count in ((25, 30, 2), (40, 50, 1)):
    report.feed('logMessage', 11. + mean, {'text': json.dumps({'msg': 'runtimeTiming', 'component': 'jetlinkd',
                      'metrics': {'server_gpu_ms': {'mean': mean, 'max': maximum, 'count': count}}})})
  assert report.result()['optional_runtime_phases']['jetlinkd.server_gpu_ms'] == {'mean': 30., 'n': 3, 'reported_max': 50}


def test_percentile_scope_is_bounded_and_bad_values_ignored():
  metric = Metric()
  for i in range(10000):
    metric.add(i)
  metric.add(float('nan'))
  assert metric.result()['n'] == 10000
  assert metric.result()['percentile_samples'] == 4096
  assert metric.result()['mean'] == 4999.5
  report = DriveReport()
  for i in range(1500):
    tracking(report, 10. + i * .2)
  assert len(report.result()['tracking_trace_recent']) == 1200
  assert len(report.cameras['roadCameraState']) <= 64


def test_full_schema_stream_and_cli_work_with_uncompressed_and_compressed_logs(tmp_path):
  import bz2
  import zstandard
  from openpilot.cereal import log
  events = [log.Event.new_message(logMonoTime=10_000_000_000, valid=True,
                                  carParams={'carFingerprint': 'KIA_PV5'}),
            log.Event.new_message(logMonoTime=10_100_000_000, valid=True,
                                  controlsState={'pv5LaneCenterStatus': 'correcting'})]
  payload = b''.join(event.to_bytes() for event in events)
  for suffix, data in (('', payload), ('.bz2', bz2.compress(payload)), ('.zst', zstandard.ZstdCompressor().compress(payload))):
    path = tmp_path / ('rlog' + suffix)
    path.write_bytes(data)
    assert [event[0] for event in read_events(path)] == ['carParams', 'controlsState']
    output = tmp_path / ('result' + suffix)
    assert main([str(path), '--output', str(output)]) == 0
    assert json.loads((output / 'report.json').read_text())['pv5_identified']
  path = tmp_path / 'malformed-rlog'
  path.write_bytes(struct.pack('<I', 1000))
  with pytest.raises(ValueError, match='segment'):
    list(read_events(path))


def test_tegrastats_is_offline_and_comparison_does_not_rank_probability_as_accuracy(tmp_path):
  stats = tmp_path / 'stats.txt'
  stats.write_text('EMC_FREQ 25%@1600 GR3D_FREQ 60%@[1000,1000]\nEMC_FREQ 50%@800 GR3D_FREQ 20%\n')
  parsed = tegrastats_metrics(stats)
  assert parsed['emc_percent_at_current_frequency']['mean'] == 37.5
  assert parsed['gpu_active_percent']['mean'] == 40.
  paths = []
  for key in ('n16', 'flip-flop', 'ctv3m', 'light-speed', 'light-speed-2'):
    path = tmp_path / (key + '.json')
    path.write_text(json.dumps({**DriveReport().result(), 'label': key}))
    paths.append(path)
  report = compare_reports(paths)
  assert len(report['reports']) == 5
  assert report['accuracy_ranking'] is None and not report['same_image_sequence_verified']
  assert main(['--compare', *map(str, paths), '--output', str(tmp_path / 'comparison')]) == 0
