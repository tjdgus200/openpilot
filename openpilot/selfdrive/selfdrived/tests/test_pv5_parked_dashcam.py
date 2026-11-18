from types import SimpleNamespace as NS

import pytest

from openpilot.common import pv5_parked_dashcam as park

TICK = 100_000_000


def state(**kwargs):
  opts = dict(gearShifter='park', standstill=True, canValid=True, canTimeout=False, vEgoRaw=0., vEgo=0.,
              gasPressed=False, stockAeb=False, cruiseState=NS(enabled=False))
  return NS(**(opts | kwargs))


def control(**kwargs):
  return NS(**(dict(enabled=False, latActive=False, longActive=False) | kwargs))


def enter(gate, start=TICK):
  for i in range(20):
    assert not gate.update(True, start + i * TICK)
  assert gate.update(True, start + 20 * TICK)
  return start + 20 * TICK


def test_entry_restarts_on_any_interruption_and_exit_needs_new_camera_and_plan():
  gate = park.ParkedDashcamRequest()
  since = enter(gate)
  assert gate.block_engagement(True, since, since, True)
  release = since + TICK
  assert not gate.update(False, release)
  assert gate.block_engagement(True, since, release+1, True)
  assert gate.block_engagement(True, release+1, release, True)
  assert gate.block_engagement(True, release+1, release+1, False)
  assert gate.block_engagement(False, release+1, release+1, True)
  assert not gate.block_engagement(True, release+1, release+1, True)
  enter(gate, release + TICK)


def test_scheduler_stall_cannot_count_as_park_evidence():
  gate = park.ParkedDashcamRequest()
  first = enter(gate)
  assert not gate.update(True, first + park.FRESH_NS)
  assert gate.block_engagement(True, first, first, True)
  gate = park.ParkedDashcamRequest(start_after=first)
  assert gate.block_engagement(True, first, first, True)
  assert not gate.block_engagement(True, first+1, first+1, True)


@pytest.mark.parametrize('kwargs', [
  dict(gearShifter='drive'), dict(gearShifter='reverse'), dict(gearShifter='unknown'), dict(standstill=False),
  dict(vEgoRaw=.0001), dict(vEgo=-.0001), dict(vEgo=float('nan')), dict(canValid=False), dict(canTimeout=True),
  dict(gasPressed=True), dict(stockAeb=True), dict(cruiseState=NS(enabled=True)),
])
def test_park_must_be_proved_even_with_valid_gear(kwargs):
  assert park.parked(state(), control())
  assert not park.parked(state(**kwargs), control())


@pytest.mark.parametrize('field', ['enabled','latActive','longActive'])
def test_active_control_prevents_entire_transition(field):
  assert not park.parked(state(), control(**{field: True}))


def test_only_opted_in_real_pv5_and_fresh_data(monkeypatch):
  for k in ('PV5_PARKED_DASHCAM_TRIAL','REPLAY','SIMULATION','OPENPILOT_PREFIX','ZMQ'):
    monkeypatch.delenv(k, raising=False)
  monkeypatch.setattr(park.os.path,'isfile',lambda path: path=='/AGNOS')
  cp = NS(carFingerprint='KIA_PV5', notCar=False, passive=False)
  assert not park.enabled_for(cp)
  monkeypatch.setenv('PV5_PARKED_DASHCAM_TRIAL','1')
  assert park.enabled_for(cp)
  assert not park.enabled_for(cp, replay=True)
  assert not park.enabled_for(NS(carFingerprint='OTHER',notCar=False,passive=False))
  assert not park.enabled_for(NS(carFingerprint='KIA_PV5',notCar=False,passive=True))
  monkeypatch.setenv('SIMULATION','1')
  assert not park.enabled_for(cp)
  assert park.fresh(True,TICK,TICK+park.FRESH_NS-1)
  assert not park.fresh(True,TICK,TICK+park.FRESH_NS)
  assert not park.fresh(True,TICK+1,TICK)
  assert not park.fresh(False,TICK,TICK)


def test_model_pause_needs_jetson_output_and_revokes_on_drive_or_stale_request():
  from collections import UserDict

  sm = UserDict(carState=state(), carControl=control(),
                selfdriveState=NS(parkedDashcamRequested=True, enabled=False, active=False, state='disabled'))
  sm.valid = dict.fromkeys(sm, True)
  sm.logMonoTime = dict.fromkeys(sm, TICK)
  options = dict(jetson_started=True, jetson_active=True, jetson_ready=True)
  for key in options:
    assert not park.pause_allowed(sm, TICK, **(options | {key: False}))
  assert park.pause_allowed(sm, TICK, **options)
  assert not park.pause_allowed(sm, TICK+park.FRESH_NS, **options)
  sm['carState'].gearShifter = 'drive'
  assert not park.pause_allowed(sm, TICK, **options)
  sm['carState'].gearShifter = 'park'
  sm['selfdriveState'].parkedDashcamRequested = False
  assert not park.pause_allowed(sm, TICK, **options)


def test_parked_state_round_trip_uses_dedicated_service():
  from openpilot.cereal import log
  from openpilot.cereal.services import SERVICE_LIST

  request = log.Event.new_message(valid=True)
  request.init('selfdriveState')
  assert not request.selfdriveState.parkedDashcamRequested
  request.selfdriveState.parkedDashcamRequested = True
  with log.Event.from_bytes(request.to_bytes()) as parsed:
    assert parsed.selfdriveState.parkedDashcamRequested
  report = log.Event.new_message(valid=True)
  report.init('parkedDashcamState')
  report.parkedDashcamState.inferencePaused = True
  with log.Event.from_bytes(report.to_bytes()) as parsed:
    assert parsed.valid and parsed.parkedDashcamState.inferencePaused
  assert SERVICE_LIST['parkedDashcamState'].frequency == 10


def test_selfdrived_blocks_enable_until_a_new_plan_consumes_fresh_model_data():
  import ast
  from collections import UserDict
  from pathlib import Path

  source = Path(__file__).resolve().parents[1] / 'selfdrived.py'
  tree = ast.parse(source.read_text())
  method = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == 'update_parked_dashcam')
  clock = [TICK]
  event = NS(dashcamMode='dashcam', selfdriveInitializing='initializing')
  ns = dict(time=NS(monotonic_ns=lambda: clock[0]), pv5_parked_dashcam=park, EventName=event, State=NS(disabled='disabled'))
  exec(compile(ast.Module(body=[method], type_ignores=[]), str(source), 'exec'), ns)
  process = UserDict(carControl=control(), deviceState=NS(started=True), parkedDashcamState=NS(inferencePaused=False),
                     modelV2=NS(timestampEof=0), longitudinalPlan=NS(modelMonoTime=0),
                     livePose=NS(inputsOK=True, sensorsOK=True, posenetOK=True),
                     driverMonitoringState=NS(dm2Disabled=False, cameraUnavailable=False, cameraFrameMonoTime=0))
  process.all_checks = lambda services: True
  process.valid = dict.fromkeys(process, True)
  process.logMonoTime = dict.fromkeys(process, 0)
  events = NS(names=['existing_alert'])
  events.add = events.names.append
  st = NS(sm=process, events=events, initialized=True, model_startup_complete=True, enabled=False, active=False,
          state_machine=NS(state='disabled'), parked_dashcam=park.ParkedDashcamRequest(),
          car_state_valid=True, car_state_mono_time=0)
  cs = state()

  def tick(now):
    events.names[:] = ['existing_alert']
    clock[0] = st.car_state_mono_time = now
    process.logMonoTime['carControl'] = now
    ns['update_parked_dashcam'](st, cs)
    assert 'existing_alert' in events.names

  for i in range(21):
    tick((i+1)*TICK)
  assert events.names == ['existing_alert','initializing']
  process['parkedDashcamState'].inferencePaused = True
  process.logMonoTime['parkedDashcamState'] = 22*TICK
  tick(22*TICK)
  assert events.names == ['existing_alert','dashcam','initializing']

  cs.gearShifter = 'drive'
  tick(23*TICK)
  assert events.names == ['existing_alert','initializing']
  for key in ('modelV2','longitudinalPlan','livePose'):
    process.logMonoTime[key] = 24*TICK
  process['modelV2'].timestampEof = 24*TICK-1
  process['longitudinalPlan'].modelMonoTime = 22*TICK
  tick(24*TICK)
  assert events.names == ['existing_alert','initializing']
  process['longitudinalPlan'].modelMonoTime = 24*TICK
  tick(25*TICK)
  assert events.names == ['existing_alert','initializing']
  process.logMonoTime['driverMonitoringState'] = 25*TICK
  process['driverMonitoringState'].cameraFrameMonoTime = 24*TICK
  tick(26*TICK)
  assert events.names == ['existing_alert']


def test_monitoring_never_pauses_in_drive_or_on_stale_road_confirmation():
  from collections import UserDict

  sm = UserDict(carState=state(), carControl=control(),
                selfdriveState=NS(parkedDashcamRequested=True, enabled=False, active=False, state='disabled'),
                parkedDashcamState=NS(inferencePaused=True))
  sm.valid = dict.fromkeys(sm, True)
  sm.logMonoTime = dict.fromkeys(sm, 2*TICK)
  assert park.monitoring_pause_allowed(sm, 2*TICK)
  sm.logMonoTime['parkedDashcamState'] = 2*TICK-park.FRESH_NS
  assert not park.monitoring_pause_allowed(sm, 2*TICK)
  sm.logMonoTime['parkedDashcamState'] = 2*TICK
  sm['carState'].gearShifter = 'drive'
  assert not park.monitoring_pause_allowed(sm, 2*TICK)


def test_park_read_only_can_report_nonzero_speed_without_recording_admission():
  from tools.pv5_remote.diagnose import park_diagnostic_status

  cp = NS(carFingerprint='KIA_PV5', notCar=False)
  observed = state(vEgoRaw=.03, vEgo=.04)
  assert park_diagnostic_status(cp, observed, True, TICK, TICK) == {
    'mode': 'park_read_only', 'standstill': True, 'raw_speed_zero': False, 'filtered_speed_zero': False}
  assert not park.parked(observed, control())
  assert park_diagnostic_status(cp, state(gearShifter='drive'), True, TICK, TICK)['reason'] == 'park_required'
  assert park_diagnostic_status(cp, observed, True, TICK, TICK+park.FRESH_NS)['reason'] == 'invalid_vehicle_state'
