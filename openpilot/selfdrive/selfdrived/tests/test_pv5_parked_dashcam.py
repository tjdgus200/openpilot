from types import SimpleNamespace as NS

import pytest

from openpilot.common import pv5_parked_dashcam as park

TICK = 100_000_000


def state(**kwargs):
  opts = dict(gearShifter='park', standstill=True, canValid=True, canTimeout=False, vEgoRaw=0., vEgo=0.,
              gasPressed=False, stockAeb=False, cruiseState=NS(enabled=False), wheelSpeeds=NS(fl=0., fr=0., rl=0., rr=0.))
  return NS(**(opts | kwargs))


def control(**kwargs):
  return NS(**(dict(enabled=False, latActive=False, longActive=False) | kwargs))


def enter(gate, start=TICK):
  assert gate.update(True, start)
  return start


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
  dict(gearShifter='drive'), dict(gearShifter='reverse'), dict(gearShifter='neutral'), dict(gearShifter='unknown'),
  dict(canValid=False), dict(canTimeout=True),
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
  assert park.supported_for(cp)
  saved = {}
  params = NS(get=saved.get)
  assert not park.setting_enabled(params)
  monkeypatch.setenv('PV5_PARKED_DASHCAM_TRIAL','1')
  assert park.setting_enabled(params)
  saved[park.PARAM] = False
  assert not park.setting_enabled(params)
  saved[park.PARAM] = True
  assert park.setting_enabled(params)
  assert not park.supported_for(cp, replay=True)
  assert not park.supported_for(NS(carFingerprint='OTHER',notCar=False,passive=False))
  assert not park.supported_for(NS(carFingerprint='KIA_PV5',notCar=False,passive=True))
  monkeypatch.setenv('SIMULATION','')
  assert not park.supported_for(cp)
  assert park.fresh(True,TICK,TICK+park.FRESH_NS-1)
  assert not park.fresh(True,TICK,TICK+park.FRESH_NS)
  assert not park.fresh(True,TICK+1,TICK)
  assert not park.fresh(False,TICK,TICK)


def test_model_pause_needs_fresh_vehicle_state_and_revokes_on_drive_or_stale_request():
  from collections import UserDict

  sm = UserDict(carState=state(), carControl=control(),
                selfdriveState=NS(parkedDashcamRequested=True, enabled=False, active=False, state='disabled'))
  sm.valid = dict.fromkeys(sm, True)
  sm.logMonoTime = dict.fromkeys(sm, TICK)
  assert park.recording_requested(sm, TICK)
  assert not park.recording_requested(sm, TICK+park.FRESH_NS)
  sm['carState'].gearShifter = 'drive'
  assert not park.recording_requested(sm, TICK)
  sm['carState'].gearShifter = 'park'
  sm['selfdriveState'].parkedDashcamRequested = False
  assert not park.recording_requested(sm, TICK)


@pytest.mark.parametrize("jetlink_wrapper", [False, True])
def test_model_loop_pauses_without_a_ready_backend_and_resets_before_resuming(jetlink_wrapper):
  import ast
  from collections import UserDict
  from pathlib import Path

  source = Path(__file__).resolve().parents[2] / 'modeld' / 'modeld.py'
  tree = ast.parse(source.read_text(encoding='utf-8'))
  main = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'main')
  attach = next(i for i, node in enumerate(main.body)
                if isinstance(node, ast.Assign) and ast.unparse(node.targets[0]) == 'reset_parked_history')
  def assignment_to(node, names):
    return isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name) and node.targets[0].id in names

  publisher_init = next(i for i, node in enumerate(main.body) if assignment_to(node, {'parked_pm'}))
  setup = main.body[attach:publisher_init+1] + [node for node in main.body if assignment_to(node, {'parked_paused', 'parked_status_at'})]
  pause = next(n for n in ast.walk(main) if isinstance(n, ast.If) and ast.unparse(n.test) == 'parked_pm is not None')
  loop = ast.parse('for frame in frames:\n  update(frame)\n  inferred.append(frame)\n').body[0]
  loop.body.insert(1, pause)
  program = compile(ast.fix_missing_locations(ast.Module(body=[*setup, loop], type_ignores=[])), str(source), 'exec')

  sm = UserDict(carState=state(), carControl=control(),
                selfdriveState=NS(parkedDashcamRequested=True, enabled=False, active=False, state='disabled'))
  sm.valid = dict.fromkeys(sm, True)
  sm.logMonoTime = dict.fromkeys(sm, TICK)
  camera = NS(frame_id=0)
  resets, sent, inferred = [], [], []
  clock = [TICK]

  def update(frame):
    camera.frame_id = frame
    clock[0] = frame * TICK
    sm.logMonoTime = dict.fromkeys(sm, clock[0])
    if frame == 3:
      sm['carState'].gearShifter = 'drive'

  reset_name = 'reset_parked_history' if jetlink_wrapper else 'reset'
  model = NS(**{reset_name: lambda: resets.append(camera.frame_id)})
  park_api = NS(supported_for=lambda *args: True, recording_requested=park.recording_requested)
  messaging = NS(new_message=lambda *args, **kw: NS(parkedDashcamState=NS(inferencePaused=False)))
  publisher = NS(send=lambda _, msg: sent.append(msg.parkedDashcamState.inferencePaused))
  exec(program, dict(USBGPU=False, SIMULATION=False, demo=False, CP=object(), pv5_parked_dashcam=park_api,
                     model=model, PubMaster=lambda _: publisher, frames=(1, 2, 3), update=update, inferred=inferred,
                     sm=sm, time=NS(monotonic_ns=lambda: clock[0]), messaging=messaging, meta_main=camera,
                     log=NS(ModelDataV2=NS(Action=object)), PublishState=object, frame_dropped_filter=NS(x=1.)))
  assert sent == [True, True, False]
  assert inferred == resets == [3]


def test_parked_state_round_trip_uses_dedicated_service():
  from openpilot.cereal import log
  from openpilot.cereal.services import SERVICE_LIST

  request = log.Event.new_message(valid=True)
  request.init('selfdriveState')
  assert not request.selfdriveState.parkedDashcamRequested
  assert not request.selfdriveState.parkedDashcamBlocking
  request.selfdriveState.parkedDashcamRequested = True
  request.selfdriveState.parkedDashcamBlocking = True
  with log.Event.from_bytes(request.to_bytes()) as parsed:
    assert parsed.selfdriveState.parkedDashcamRequested
    assert parsed.selfdriveState.parkedDashcamBlocking
  report = log.Event.new_message(valid=True)
  report.init('parkedDashcamState')
  report.parkedDashcamState.inferencePaused = True
  with log.Event.from_bytes(report.to_bytes()) as parsed:
    assert parsed.valid and parsed.parkedDashcamState.inferencePaused
  assert SERVICE_LIST['parkedDashcamState'].frequency == 10


@pytest.mark.parametrize("turn_off_in_park", [False, True])
@pytest.mark.parametrize("cold_start", [False, True])
def test_selfdrived_enters_park_before_models_are_ready_and_requires_fresh_plan_on_exit(turn_off_in_park, cold_start):
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
  st = NS(sm=process, events=events, initialized=not cold_start, model_startup_complete=not cold_start, enabled=False, active=False,
          state_machine=NS(state='disabled'), parked_dashcam=park.ParkedDashcamRequest(),
          parked_dashcam_enabled=False, car_state_valid=True, car_state_mono_time=0)
  cs = state()

  def tick(now):
    events.names[:] = ['existing_alert']
    clock[0] = st.car_state_mono_time = now
    process.logMonoTime['carControl'] = now
    ns['update_parked_dashcam'](st, cs)
    assert 'existing_alert' in events.names

  tick(TICK)
  assert not st.parked_dashcam.requested
  assert events.names == ['existing_alert']

  st.parked_dashcam_enabled = True
  for i in range(2, 22):
    tick(i*TICK)
    assert st.parked_dashcam.requested
  assert events.names == ['existing_alert','initializing']
  process['parkedDashcamState'].inferencePaused = True
  process.logMonoTime['parkedDashcamState'] = 22*TICK
  tick(22*TICK)
  assert events.names == ['existing_alert','dashcam','initializing']

  if turn_off_in_park:
    st.parked_dashcam_enabled = False
  else:
    cs.gearShifter = 'drive'
  tick(23*TICK)
  assert not st.parked_dashcam.requested
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
  if turn_off_in_park:
    for i in range(27, 48):
      tick(i*TICK)
    assert not st.parked_dashcam.requested


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


def test_park_ignores_sustained_motion_signals_on_entry_and_hold():
  request = park.ParkedDashcamRequest()
  car = state(standstill=False, vEgoRaw=.04, vEgo=.12, wheelSpeeds=NS(fl=.03, fr=.05, rl=.04, rr=.04))
  for i in range(1, 30):
    assert request.update(park.parked(car, control()), i * TICK)
  car.gearShifter = 'drive'
  assert not request.update(park.parked(car, control()), 30 * TICK)


def test_comma_brightness_uses_park_percent_and_restores_on_stale_ack_or_critical():
  import ast
  from collections import UserDict
  from pathlib import Path

  from openpilot.common.filter_simple import FirstOrderFilter

  source = Path(__file__).parents[2] / 'ui' / 'ui_state.py'
  tree = ast.parse(source.read_text(encoding='utf-8'))
  method = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == '_update_brightness')
  sm = UserDict(carState=state(), carControl=control(), parkedDashcamState=NS(inferencePaused=True),
                selfdriveState=NS(parkedDashcamRequested=True, enabled=False, active=False, state='disabled', alertStatus='normal'))
  sm.valid = dict.fromkeys(sm, True)
  sm.logMonoTime = dict.fromkeys(sm, 3*TICK)
  ui = NS(parked_dashcam_brightness=1, started=True, light_sensor=-1, sm=sm,
          CP=NS(carFingerprint='KIA_PV5', notCar=False))
  sent = []
  hardware = NS(set_screen_brightness=sent.append)
  thread = lambda *, target, args: NS(start=lambda: target(*args), is_alive=lambda: False)
  log = NS(SelfdriveState=NS(AlertStatus=NS(critical='critical')))
  env = dict(ui_state=ui, time=NS(monotonic_ns=lambda: 3*TICK), pv5_parked_dashcam=park, log=log,
             HARDWARE=hardware, threading=NS(Thread=thread))
  exec(compile(ast.Module(body=[method], type_ignores=[]), str(source), 'exec'), env)
  device = NS(_offroad_brightness=50, _awake=True, _last_brightness=50, _brightness_thread=None,
              _brightness_filter=FirstOrderFilter(50, 2., .05), _parked_brightness_active=False)

  env['_update_brightness'](device)
  assert sent == [1]
  sm['selfdriveState'].alertStatus = 'critical'
  env['_update_brightness'](device)
  assert sent == [1, 50]
  sm['selfdriveState'].alertStatus = 'normal'
  ui.parked_dashcam_brightness = 31
  env['_update_brightness'](device)
  assert sent[-1] == 31
  sm.logMonoTime['parkedDashcamState'] -= park.FRESH_NS
  env['_update_brightness'](device)
  assert sent[-1] == 50


def test_park_read_only_reports_speed_separately_from_dashcam_admission():
  from tools.pv5_remote.diagnose import park_diagnostic_status

  cp = NS(carFingerprint='KIA_PV5', notCar=False)
  observed = state(vEgoRaw=.03, vEgo=.04)
  assert park_diagnostic_status(cp, observed, True, TICK, TICK) == {
    'mode': 'park_read_only', 'standstill': True, 'raw_speed_zero': False, 'filtered_speed_zero': False}
  assert park.parked(observed, control())
  assert park_diagnostic_status(cp, state(gearShifter='drive'), True, TICK, TICK)['reason'] == 'park_required'
  assert park_diagnostic_status(cp, observed, True, TICK, TICK+park.FRESH_NS)['reason'] == 'invalid_vehicle_state'
