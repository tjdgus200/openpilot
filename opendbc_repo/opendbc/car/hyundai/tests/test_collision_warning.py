"""Independent PC checks for warning-only original SCC qualification.

These are numerical/source tests, not a vehicle collision-warning validation.
"""
from copy import deepcopy
from types import SimpleNamespace as NS

import pytest

from opendbc.can import CANPacker, CANParser
from opendbc.car import structs
from opendbc.car.hyundai.collision_warning import OffCruiseWarning, RiskInputs, SccRiskSource, write_inputs
from opendbc.car.hyundai.hyundaicanfd import hkg_can_fd_checksum
from opendbc.car.hyundai.scc_decel import CameraSccSource

T = 10_000_000_000
STEP = 20_000_000
MAX_AGE = 100_000_000


def metadata(stamp=T, **changes):
  values = dict(version=1, sourceValid=True, sourceMonoTime=stamp, raw=-3.2,
                distance=15.0, relativeSpeed=-10.0, targetValid=True, inactiveClear=False)
  values.update(changes)
  return NS(**values)


def state(stamp=T, *, meta=None, **changes):
  values = dict(vEgo=15.0, vEgoRaw=15.0, gearShifter='drive',
                canValid=True, canTimeout=False, parkingBrake=False, brakeHoldActive=False,
                standstill=False, gasPressed=False, brakePressed=False,
                cruiseState=NS(enabled=False), pv5CollisionWarning=meta or metadata(stamp))
  values.update(changes)
  return NS(**values)


def update(tracker=None, *, now=T, state_stamp=None, meta=None, enabled=False, state_valid=True, **changes):
  return (tracker or OffCruiseWarning()).update(state(now, meta=meta, **changes), now if state_stamp is None else state_stamp,
                                               now, enabled=enabled, state_valid=state_valid)


def wire():
  return CANPacker('hyundai_canfd_generated'), CANParser('hyundai_canfd_generated', [('SCC_CONTROL', 50)], 2)


def frame(packer, counter, **changes):
  values = dict(COUNTER=counter, MainMode_ACC=1, ACCMode=1, SysFailState=0,
                aReqRaw=-3.2, aReqValue=-1.0, ACC_ObjDist=15.0,
                ACC_ObjRelSpd=-10.0, HUD_LEAD_INFO=1)
  values.update(changes)
  return packer.make_can_msg('SCC_CONTROL', 2, values)


def receive(packer, parser, counter, stamp, **changes):
  parser.update([(stamp, [frame(packer, counter, **changes)])])


def primed():
  packer, parser = wire()
  source = SccRiskSource(hkg_can_fd_checksum, 2)
  receive(packer, parser, 1, T)
  assert not source.update(parser, T).source_valid
  receive(packer, parser, 2, T + STEP)
  result = source.update(parser, T + STEP)
  assert result.source_valid and result.target_valid
  return packer, parser, source


def test_first_qualified_danger_sample_warns_immediately_without_controls():
  packer, parser, source = primed()
  inputs = source.update(parser, T + STEP)
  cs = structs.CarState.new_message(vEgo=15, vEgoRaw=15, gearShifter='drive', canValid=True)
  write_inputs(cs, inputs)
  result = OffCruiseWarning().update(cs, T + STEP, T + STEP, enabled=False, state_valid=True)
  assert result.level == 2 and result.audible
  assert cs.pv5CollisionWarning.sourceMonoTime == T + STEP
  assert cs.pv5CollisionWarning.raw == pytest.approx(-3.2, abs=.011)


def test_serialized_metadata_is_display_only_and_no_actuators_written():
  cs = structs.CarState.new_message(vEgo=15, vEgoRaw=15, gearShifter='drive', canValid=True)
  before = cs.to_dict()
  inputs = RiskInputs(version=1, source_valid=True, stamp=T, raw=-3.2,
                      distance=15.0, relative_speed=-10.0, target_valid=True)
  write_inputs(cs, inputs)
  after = cs.to_dict()
  assert {k: v for k, v in before.items() if k != 'pv5CollisionWarning'} == {
    k: v for k, v in after.items() if k != 'pv5CollisionWarning'}
  assert cs.pv5CollisionWarning.version == 1
  assert cs.pv5CollisionWarning.sourceValid
  assert cs.pv5CollisionWarning.targetValid


@pytest.mark.parametrize('bus', [0, 1, 128, 130])
def test_wrong_bus_and_transmit_echo_never_qualify(bus):
  _, parser, source = primed()
  parser.bus = bus
  assert not source.update(parser, T + STEP).source_valid


def test_absent_parser_clears_stale_previous_source():
  _, _, source = primed()
  assert not source.update(None, T + 2 * STEP).source_valid


@pytest.mark.parametrize('changes', [dict(ACCMode=0), dict(ACCMode=2), dict(MainMode_ACC=0), dict(SysFailState=1)])
def test_off_or_override_wire_mode_does_not_weaken_original_source_contract(changes):
  packer, parser, source = primed()
  receive(packer, parser, 3, T + 2 * STEP, **changes)
  assert not source.update(parser, T + 2 * STEP).source_valid


def test_crc_rejection_needs_fresh_consecutive_requalification():
  packer, parser, source = primed()
  msg = frame(packer, 3)
  bad = bytearray(msg[1])
  bad[16] ^= 1
  parser.update([(T + 2 * STEP, [(msg[0], bytes(bad), msg[2])])])
  assert not source.update(parser, T + 2 * STEP).source_valid
  receive(packer, parser, 4, T + 3 * STEP)
  assert not source.update(parser, T + 3 * STEP).source_valid
  receive(packer, parser, 5, T + 4 * STEP)
  assert source.update(parser, T + 4 * STEP).source_valid


@pytest.mark.parametrize('counter', [2, 12, 255])
def test_repeated_or_discontinuous_counter_invalidates_source(counter):
  packer, parser, source = primed()
  receive(packer, parser, counter, T + 2 * STEP)
  assert not source.update(parser, T + 2 * STEP).source_valid


def test_original_source_accepts_counter_wrap_and_batched_frames():
  packer, parser = wire()
  parser.message_states[416].counter = 253
  source = SccRiskSource(hkg_can_fd_checksum, 2)
  receive(packer, parser, 254, T)
  assert not source.update(parser, T).source_valid
  parser.update([(T + STEP, [frame(packer, 255)]), (T + 2 * STEP, [frame(packer, 0)])])
  assert source.update(parser, T + 2 * STEP).source_valid


@pytest.mark.parametrize('hud', [0, 3])
def test_wire_target_marker_is_required(hud):
  packer, parser, source = primed()
  receive(packer, parser, 3, T + 2 * STEP, HUD_LEAD_INFO=hud)
  assert not source.update(parser, T + 2 * STEP).target_valid


def test_warning_observer_does_not_change_existing_actuation_source_or_parser():
  packer, parser = wire()
  baseline = CameraSccSource(hkg_can_fd_checksum)
  observed = CameraSccSource(hkg_can_fd_checksum)
  warning = SccRiskSource(hkg_can_fd_checksum, 2)
  for i, raw in enumerate([0., -3.2, -3.8, -2., 0.]):
    now = T + i * STEP
    receive(packer, parser, i + 1, now, aReqRaw=raw)
    expected = baseline.update(parser, 2, now)
    before = deepcopy(parser.vl['SCC_CONTROL']), deepcopy(parser.ts_nanos['SCC_CONTROL']), bytes(parser.dat[416])
    warning.update(parser, now)
    actual = observed.update(parser, 2, now)
    after = parser.vl['SCC_CONTROL'], parser.ts_nanos['SCC_CONTROL'], bytes(parser.dat[416])
    assert expected == actual and before == after


@pytest.mark.parametrize('changes', [
  dict(canValid=False), dict(canTimeout=True), dict(parkingBrake=True), dict(brakeHoldActive=True),
  dict(standstill=True), dict(vEgo=1.999), dict(vEgoRaw=1.999), dict(vEgo=float('nan')),
  dict(vEgoRaw=float('inf')), dict(gearShifter='park'),
  dict(gearShifter='neutral'), dict(gearShifter='reverse'),
  dict(gearShifter='unknown'), dict(cruiseState=NS(enabled=True)),
])
def test_invalid_state_and_low_speed_parking_are_silent(changes):
  assert update(**changes).level == 0


def test_engaged_and_invalid_state_envelopes_are_silent():
  assert update(enabled=True).level == 0
  assert update(state_valid=False).level == 0


@pytest.mark.parametrize('offset, expected', [(-MAX_AGE-1, 0), (-MAX_AGE, 2), (1, 0), (0, 2)])
def test_state_timestamp_boundaries(offset, expected):
  assert update(state_stamp=T + offset).level == expected


@pytest.mark.parametrize('offset, expected', [(-MAX_AGE-1, 0), (-MAX_AGE, 2), (1, 0), (0, 2)])
def test_source_timestamp_boundaries(offset, expected):
  assert update(meta=metadata(T + offset)).level == expected


@pytest.mark.parametrize('changes', [
  dict(version=0), dict(version=2), dict(sourceValid=False), dict(targetValid=False), dict(sourceMonoTime=0),
  dict(raw=float('nan')), dict(raw=float('inf')), dict(raw=-10.25), dict(raw=-2.999),
  dict(distance=float('nan')), dict(distance=float('inf')), dict(distance=0.), dict(distance=40.001),
  dict(relativeSpeed=float('nan')), dict(relativeSpeed=float('-inf')),
])
def test_absent_mixed_invalid_or_nonsensical_metadata_has_no_fallback(changes):
  assert update(meta=metadata(**changes)).level == 0


def test_legacy_message_without_metadata_is_quiet():
  cs = state()
  del cs.pv5CollisionWarning
  assert OffCruiseWarning().update(cs, T, T).level == 0


@pytest.mark.parametrize('distance, closing, expected', [
  (15.0, 10., 2), (15.001, 10., 0),  # TTC 1.5 boundary
  (24., 16., 2), (40.001, 16., 0),
  (1.25, 1.25, 0),  # short TTC without meaningful required deceleration
  (5., 0., 0), (5., -.2, 0), (20., 2., 0),  # same speed, pulling away, normal follow
  (.5, 5., 3), (.1, 5., 3), (0., 5., 0),
  (7.5, 10., 3), (7.501, 10., 2),  # urgency rise must not wait
])
def test_physical_risk_and_nuisance_boundaries(distance, closing, expected):
  assert update(meta=metadata(distance=distance, relativeSpeed=-closing)).level == expected


@pytest.mark.parametrize('raw, expected', [(-10.23, 3), (-4.001, 3), (-4., 3), (-3.5, 3), (-3.499, 2), (-3., 2)])
def test_raw_request_severity_boundaries(raw, expected):
  assert update(meta=metadata(raw=raw)).level == expected


@pytest.mark.parametrize('pedals', [dict(gasPressed=True), dict(brakePressed=True), dict(gasPressed=True, brakePressed=True)])
def test_manual_pedal_input_does_not_hide_qualified_imminent_collision(pedals):
  cue = update(**pedals)
  assert cue.level == 2 and cue.audible


def test_short_audio_pulse_does_not_repeat_for_persistent_risk_or_duplicate_calls():
  tracker = OffCruiseWarning()
  assert update(tracker).audible
  assert update(tracker, now=T + 100_000_000).audible
  assert not update(tracker, now=T + 150_000_000).audible
  for i in range(20, 80):
    now = T + i * 10_000_000
    assert not update(tracker, now=now).audible
    assert not update(tracker, now=now).audible


def test_new_emergency_after_lower_risk_pulse_is_immediate():
  tracker = OffCruiseWarning()
  assert update(tracker).audible
  assert not update(tracker, now=T + 200_000_000).audible
  severe = update(tracker, now=T + 220_000_000, meta=metadata(T + 220_000_000, raw=-3.6, distance=4., relativeSpeed=-10.))
  assert severe.level == 3 and severe.audible


def test_one_second_continuous_fresh_safe_data_rearms_new_risk():
  tracker = OffCruiseWarning()
  assert update(tracker).audible
  for i in range(1, 53):
    now = T + i * STEP
    assert update(tracker, now=now, meta=metadata(now, raw=0., distance=30., relativeSpeed=0.)).level == 0
  assert update(tracker, now=T + 54 * STEP).audible


def test_invalid_data_and_stale_cached_safe_metadata_do_not_rearm_episode():
  tracker = OffCruiseWarning()
  assert update(tracker).audible
  for i in range(1, 101):
    now = T + i * STEP
    old_safe = metadata(T, raw=0., distance=30., relativeSpeed=0.)
    assert update(tracker, now=now, meta=old_safe, state_valid=False).level == 0
  result = update(tracker, now=T + 102 * STEP)
  assert result.level == 2 and not result.audible


def test_duplicate_safe_timestamp_cannot_accumulate_rearm_time():
  tracker = OffCruiseWarning()
  assert update(tracker).audible
  for i in range(1, 81):
    now = T + i * STEP
    result = update(tracker, now=now, meta=metadata(T + STEP, raw=0., distance=30., relativeSpeed=0.))
    assert result.level == 0
  assert not update(tracker, now=T + 82 * STEP).audible


def test_invalid_gap_breaks_fresh_safe_confirmation():
  tracker = OffCruiseWarning()
  assert update(tracker).audible
  for i in range(1, 31):
    now = T + i * STEP
    update(tracker, now=now, meta=metadata(now, raw=0.))
  update(tracker, now=T + 31 * STEP, state_valid=False)
  for i in range(32, 61):
    now = T + i * STEP
    update(tracker, now=now, meta=metadata(now, raw=0.))
  assert not update(tracker, now=T + 61 * STEP).audible


def test_required_deceleration_boundary_and_minimum_moving_speed():
  assert update(vEgo=2., vEgoRaw=2., meta=metadata(distance=1.6, relativeSpeed=-2.)).level == 2
  assert update(meta=metadata(distance=1.6001, relativeSpeed=-2.)).level == 0


def test_missing_target_is_not_proof_of_safe_interval_for_rearming():
  tracker = OffCruiseWarning()
  assert update(tracker).audible
  for i in range(1, 65):
    now = T + i * STEP
    assert update(tracker, now=now, meta=metadata(now, targetValid=False, raw=0.)).level == 0
  assert not update(tracker, now=T + 65 * STEP).audible


def test_backward_clock_is_not_a_new_risk_episode():
  tracker = OffCruiseWarning()
  assert update(tracker).audible
  assert not update(tracker, now=T + 200_000_000).audible
  assert update(tracker, now=T + 100_000_000).level == 0
  assert not update(tracker, now=T + 220_000_000).audible



@pytest.mark.parametrize('bad', [dict(raw=float('nan')), dict(distance=float('inf')),
                                 dict(relativeSpeed=float('nan')), dict(distance=-1.)])
def test_invalid_numeric_target_is_not_fresh_safe_evidence(bad):
  tracker = OffCruiseWarning()
  assert update(tracker).audible
  for i in range(1, 65):
    now = T + i * STEP
    assert update(tracker, now=now, meta=metadata(now, **bad)).level == 0
  assert not update(tracker, now=T + 65 * STEP).audible


def test_fresh_valid_receding_target_can_complete_episode():
  tracker = OffCruiseWarning()
  assert update(tracker).audible
  for i in range(1, 55):
    now = T + i * STEP
    assert update(tracker, now=now, meta=metadata(now, raw=0., distance=45., relativeSpeed=2.)).level == 0
  assert update(tracker, now=T + 55 * STEP).audible



def test_inactive_clear_requires_qualified_transport_and_zero_acc_mode():
  packer, parser, source = primed()
  receive(packer, parser, 3, T + 2 * STEP, ACCMode=0, aReqRaw=0.)
  result = source.update(parser, T + 2 * STEP)
  assert not result.source_valid and result.inactive_clear
  assert result.stamp == T + 2 * STEP


@pytest.mark.parametrize('changes', [dict(ACCMode=2), dict(ACCMode=3),
                                     dict(SysFailState=1), dict(aReqRaw=-3.), dict(aReqRaw=-4.)])
def test_unqualified_mode_or_strong_raw_is_not_inactive_clear(changes):
  packer, parser, source = primed()
  receive(packer, parser, 3, T + 2 * STEP, **(dict(ACCMode=0, aReqRaw=0.) | changes))
  assert not source.update(parser, T + 2 * STEP).inactive_clear


def test_inactive_message_crc_failure_and_gap_cannot_rearm_source():
  packer, parser, source = primed()
  msg = frame(packer, 3, ACCMode=0, aReqRaw=0.)
  bad = bytearray(msg[1])
  bad[16] ^= 1
  parser.update([(T + 2 * STEP, [(msg[0], bytes(bad), msg[2])])])
  result = source.update(parser, T + 2 * STEP)
  assert not result.source_valid and not result.inactive_clear
  receive(packer, parser, 4, T + 3 * STEP, ACCMode=0, aReqRaw=0.)
  assert not source.update(parser, T + 3 * STEP).inactive_clear
  receive(packer, parser, 5, T + 4 * STEP, ACCMode=0, aReqRaw=0.)
  assert source.update(parser, T + 4 * STEP).inactive_clear
  receive(packer, parser, 30, T + 5 * STEP, ACCMode=0, aReqRaw=0.)
  assert not source.update(parser, T + 5 * STEP).inactive_clear


def test_transport_qualified_inactive_interval_can_rearm_without_a_target():
  tracker = OffCruiseWarning()
  assert update(tracker).audible
  for i in range(1, 55):
    now = T + i * STEP
    safe = metadata(now, sourceValid=False, inactiveClear=True, targetValid=False, raw=0., distance=0., relativeSpeed=0.)
    assert update(tracker, now=now, meta=safe).level == 0
  assert update(tracker, now=T + 55 * STEP).audible


def test_stale_inactive_clear_is_not_safe_transport_evidence():
  tracker = OffCruiseWarning()
  assert update(tracker).audible
  for i in range(1, 75):
    now = T + i * STEP
    safe = metadata(T, sourceValid=False, inactiveClear=True, targetValid=False, raw=0.)
    assert update(tracker, now=now, meta=safe).level == 0
  assert not update(tracker, now=T + 75 * STEP).audible


def test_source_stale_and_future_remove_transport_inactive_proof():
  packer, parser, source = primed()
  receive(packer, parser, 3, T + 2 * STEP, ACCMode=0, aReqRaw=0.)
  assert source.update(parser, T + 2 * STEP).inactive_clear
  assert not source.update(parser, T + 2 * STEP - 1).inactive_clear
  assert not source.update(parser, T + 2 * STEP + MAX_AGE + 1).inactive_clear


def test_stronger_wire_deceleration_is_not_discarded_by_controller_accel_clamp():
  packer, parser, source = primed()
  receive(packer, parser, 3, T + 2 * STEP, aReqRaw=-10.23)
  info = source.update(parser, T + 2 * STEP)
  assert info.source_valid and info.raw == pytest.approx(-10.23, abs=.011)
  cs = structs.CarState.new_message(vEgo=15, vEgoRaw=15, gearShifter='drive', canValid=True)
  write_inputs(cs, info)
  cue = OffCruiseWarning().update(cs, T + 2 * STEP, T + 2 * STEP)
  assert cue.level == 3 and cue.audible





@pytest.mark.parametrize('distance, valid', [(0., False), (.1, True), (.2, True), (.5, True), (204.6, False), (204.7, False)])
def test_actual_wire_distance_resolution_does_not_drop_imminent_positive_distance(distance, valid):
  packer, parser, source = primed()
  now = T + 2 * STEP
  receive(packer, parser, 3, now, ACC_ObjDist=distance)
  inputs = source.update(parser, now)
  assert inputs.target_valid == valid
  cs = structs.CarState.new_message(vEgo=15, vEgoRaw=15, gearShifter='drive', canValid=True)
  write_inputs(cs, inputs)
  result = OffCruiseWarning().update(cs, now, now)
  assert result.level == (3 if valid else 0)
  assert result.audible == valid


def test_approach_warning_does_not_disappear_inside_half_metre():
  tracker = OffCruiseWarning()
  for i, distance in enumerate([1., .8, .5, .3, .1]):
    now = T + i * STEP
    cue = update(tracker, now=now, meta=metadata(now, distance=distance, relativeSpeed=-5.))
    assert cue.level == 3
  assert update(tracker, now=T + 5 * STEP, meta=metadata(T + 5 * STEP, distance=0.)).level == 0



LIFECYCLE_HIDDEN_STATES = [
  dict(enabled=True),
  dict(cruiseState=NS(enabled=True)),
  dict(standstill=True, vEgo=0., vEgoRaw=0.),
  dict(gearShifter='park', parkingBrake=True, standstill=True, vEgo=0., vEgoRaw=0.),
]


@pytest.mark.parametrize('hidden', LIFECYCLE_HIDDEN_STATES)
@pytest.mark.parametrize('inactive', [False, True])
def test_lifecycle_fresh_safe_evidence_rearms_while_warning_display_is_inhibited(hidden, inactive):
  tracker = OffCruiseWarning()
  assert update(tracker).audible
  for i in range(1, 55):
    now = T + i * STEP
    safe = metadata(now, raw=0., distance=30., relativeSpeed=0.,
                    sourceValid=not inactive, inactiveClear=inactive, targetValid=not inactive)
    cue = update(tracker, now=now, meta=safe, **hidden)
    assert cue.level == 0 and not cue.audible
  cue = update(tracker, now=T + 55 * STEP)
  assert cue.level == 2 and cue.audible


@pytest.mark.parametrize('hidden', LIFECYCLE_HIDDEN_STATES)
def test_lifecycle_hidden_danger_does_not_consume_first_off_cruise_warning(hidden):
  tracker = OffCruiseWarning()
  for i in range(1, 55):
    now = T + i * STEP
    cue = update(tracker, now=now, meta=metadata(now, raw=-4., distance=5.), **hidden)
    assert cue.level == 0 and not cue.audible
  # A hidden urgent condition must not raise the episode peak and suppress this
  # first displayed warning when Drive/motion/OFF-cruise admission becomes true.
  cue = update(tracker, now=T + 55 * STEP)
  assert cue.level == 2 and cue.audible


@pytest.mark.parametrize('hidden', LIFECYCLE_HIDDEN_STATES)
def test_lifecycle_stale_safe_source_during_inhibited_display_cannot_rearm(hidden):
  tracker = OffCruiseWarning()
  assert update(tracker).audible
  for i in range(1, 75):
    now = T + i * STEP
    safe = metadata(T, raw=0., sourceValid=False, inactiveClear=True, targetValid=False)
    cue = update(tracker, now=now, meta=safe, **hidden)
    assert cue.level == 0 and not cue.audible
  cue = update(tracker, now=T + 75 * STEP)
  assert cue.level == 2 and not cue.audible


def test_lifecycle_hidden_risk_neither_rearms_old_episode_nor_consumes_escalation():
  tracker = OffCruiseWarning()
  assert update(tracker).audible
  for i in range(1, 60):
    now = T + i * STEP
    assert update(tracker, now=now, enabled=True, meta=metadata(now, raw=-4., distance=5.)).level == 0
  assert not update(tracker, now=T + 60 * STEP).audible
  cue = update(tracker, now=T + 61 * STEP, meta=metadata(T + 61 * STEP, raw=-4., distance=5.))
  assert cue.level == 3 and cue.audible


def test_lifecycle_can_invalid_while_cruising_is_not_safe_source_evidence():
  tracker = OffCruiseWarning()
  assert update(tracker).audible
  for i in range(1, 65):
    now = T + i * STEP
    assert update(tracker, now=now, enabled=True, canValid=False,
                  meta=metadata(now, raw=0., sourceValid=False, inactiveClear=True, targetValid=False)).level == 0
  assert not update(tracker, now=T + 65 * STEP).audible
