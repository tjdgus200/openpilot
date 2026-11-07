"""PC-only verification/rendering of the exact selected PV5 archive, offline.

Requires pytest/numpy/pycapnp/Pillow/raylib plus an available native graphics
context (Xvfb/Mesa on headless Linux). Does not contact a device or download data.
"""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[2]

# Executes in a child with only the extracted package on its source import path.
PROBE = '''
import base64,json,sys,time
from pathlib import Path
from dataclasses import replace
from unittest.mock import patch
import server,hud,hud_health,hud_protocol,boot_status
from model_pin import read_spec
from cluster_live import OpenpilotLiveSource,standby_state
from cluster_models import ClusterAlert
from openpilot.cereal import log
from openpilot.common.hud_driving_feedback import DRIVING_NOTICE_TEXT
from openpilot.selfdrive.carrot import signal_mode
from openpilot.common.performance_diagnostics import ENABLED
assert not ENABLED and not server.PERF_DIAGNOSTICS
record=json.loads(Path(sys.argv[1]).read_text())
def add_event(name,event):
 record['events'][name]=base64.b64encode(event.to_bytes()).decode()
 record['received'][name]=record['sent']
 record['mono'][name]=int(record['sent']*1e9)
 record['valid'][name]=record['alive'][name]=True
with log.Event.from_bytes(base64.b64decode(record['events']['carState'])) as event:
 car_event=event.as_builder()
car_event.carState.canValid=True
car_event.carState.gearShifter='drive'
add_event('carState',car_event)
with log.Event.from_bytes(base64.b64decode(record['events']['modelV2'])) as event:
 model_event=event.as_builder()
model_event.modelV2.meta.laneChangeState='laneChangeStarting'
model_event.modelV2.meta.laneChangeDirection='left'
add_event('modelV2',model_event)
add_event('carParams',log.Event.new_message(carParams={'carFingerprint':'KIA_PV5'}))
add_event('radarState',log.Event.new_message(radarState={'leadCutInRisk':{'status':True,'radar':True,'dRel':18.}}))
hud.read_snapshot=lambda:(time.monotonic(),record)
hud_protocol.read_snapshot=hud.read_snapshot
remote=hud.RemoteSubMaster(list(record['events']))
remote.update()
source=object.__new__(OpenpilotLiveSource)
source.sm,source.params=remote,None
source._next_egpu_param_read_t=float('inf')
source._max_lateral_accel,source._energy_gauge_label=3.,'battery'
source._carrot_navi_media=None
source._current_carrot_navi=lambda _:None
source._live_cluster_alert=lambda *_:None
state=source._with_live_hud_state(replace(standby_state(),speed_kph=72.,gear_text='D'))
assert state.pv5_lane_center_status=='correcting'
assert abs(state.pv5_lane_center_fraction-.75)<1e-6
assert abs(state.left_lane_probability-.99)<1e-6 and abs(state.right_lane_probability-.99)<1e-6
assert state.steering_output_normalized is not None
assert state.driving_notices==('cutin','laneChange')
assert state.driving_lane_change_direction=='left'
notices=state.driving_notices
state=replace(state,driving_notices=())
assert boot_status.model_name(read_spec(server.ROOT)['sha256'])!='--'
hud_health._fresh=lambda *_:{'severity':'ok','thermal_severity':'ok','temp_c':59.,'power_w':12.4}
signal_mode.signal_mode=lambda:0
ui=hud_health.HealthRenderer(theme_mode='dark',screen_mode=8)
ui._host_metrics=lambda:{'timing':{'window_s':10.,'count':200,'last_frame_age_s':.1,
 'infer_ms':{'mean':25.,'p99':28.,'max':31.},'server_ms':{'mean':27.,'p99':30.,'max':33.}}}
lines={}
draw=ui._draw_runtime_line
def capture(segments,*layout):
 lines[layout[-1]]=''.join(text for text,_ in segments)
 draw(segments,*layout)
ui._draw_runtime_line=capture
try:
 ui.open(hidden=True)
 assert ui._telemetry_font is not None
 assert ui._telemetry_font.texture.id!=hud_health.rl.get_font_default().texture.id
 ui.render_to_file(state,Path(sys.argv[2]))
 assert 'TRT 10s AVG:25.0 P99:28.0 MAX:31.0 ms' in lines['infer']
 assert '| COMMA 40.0 ms' in lines['infer']
 assert sum(ui._runtime_line_layouts['infer'][2]) < 520
 from PIL import Image
 picture=Image.open(sys.argv[2]).convert('RGB')
 assert picture.size==(1920,480) and len(picture.getcolors(1000000))>50
 popup_path=Path(sys.argv[2]).with_name('hud-popup.png')
 popup=replace(state,deceleration_banner='방지턱 감속',steering_warning=1)
 anchor=(340,148)
 ui.render_to_file(popup,popup_path)
 assert Image.open(popup_path).convert('RGB').getpixel(anchor)!=(77,46,9)
 ui._popup_slide.top(time.monotonic()-1.,82,76,urgent=True)
 ui.render_to_file(popup,popup_path)
 assert Image.open(popup_path).convert('RGB').getpixel(anchor)==(77,46,9)
 ui._popup_slide.reset()
 ui.render_to_file(replace(popup,steering_warning=2),popup_path)
 assert Image.open(popup_path).convert('RGB').getpixel(anchor)==(156,18,27)
 ui.render_to_file(replace(popup,steering_warning=2,alert=ClusterAlert('Vehicle warning',size=2)),popup_path)
 assert Image.open(popup_path).convert('RGB').getpixel(anchor)!=(156,18,27)
 ui.render_to_file(replace(state,deceleration_banner='방지턱 감속'),popup_path)
 ui._popup_slide.top(time.monotonic()-1.,82,76,urgent=True)
 ui.render_to_file(replace(state,deceleration_banner='방지턱 감속'),popup_path)
 pixel=Image.open(popup_path).convert('RGB').getpixel(anchor)
 assert max(abs(a-b) for a,b in zip(pixel,(31,70,143),strict=True))<=2
 texts=[]
 draw_text=ui._draw_text_with_stroke
 def capture_text(text,*args,**kwargs):
  texts.append(text)
  draw_text(text,*args,**kwargs)
 ui._draw_text_with_stroke=capture_text
 ui.render_to_file(replace(state,driving_notices=notices),popup_path)
 assert DRIVING_NOTICE_TEXT['cutin'] in texts and DRIVING_NOTICE_TEXT['laneChange'] not in texts
 ui._driving_notices.reset()
 ui._popup_slide.reset()
 texts.clear()
 ui.render_to_file(replace(state,driving_notices=('laneChange',)),popup_path)
 assert DRIVING_NOTICE_TEXT['laneChange'] in texts
 ui._driving_notices.reset()
 ui._popup_slide.reset()
 lane_state=replace(state,driving_notices=('laneChange',))
 for now in (1000.,1000.3,1003.,1030.):
  texts.clear()
  with patch('time.monotonic',return_value=now):
   ui.render_to_file(lane_state,popup_path)
  assert DRIVING_NOTICE_TEXT['laneChange'] in texts
  if now>1000.:
   with Image.open(popup_path) as frame:
    assert frame.convert('RGB').getpixel(anchor)==(7,89,86)
 for now,visible in ((1030.1,True),(1030.21,True),(1030.33,False)):
  texts.clear()
  with patch('time.monotonic',return_value=now):
   ui.render_to_file(state,popup_path)
  assert (DRIVING_NOTICE_TEXT['laneChange'] in texts)==visible
 camera_path=popup_path.with_name('hud-camera-decel.png')
 camera_title='과속카메라 단속 50 km/h 감속 중'
 for name,payload in (
  ('carrotMan',{'desiredSource':'cam','desiredSpeed':60,'decelCameraTargetKph':50,'decelCameraDistanceM':250}),
  ('carControl',{'longActive':True}),('longitudinalPlan',{'cruiseTarget':60.,'aTarget':-.2}),
 ):
  add_event(name,log.Event.new_message(**{name:payload}))
 remote=hud.RemoteSubMaster(list(record['events']))
 remote.update()
 source.sm=remote
 cruise=replace(standby_state(),speed_kph=72.,gear_text='D',cruise_kph=80.,cruise_display_state='engaged')
 braking=source._with_live_hud_state(cruise)
 assert braking.deceleration_banner==camera_title+'\\n250 m 남음'
 assert ui._measure_text(camera_title,40)[0] < 620
 ui._driving_notices.reset()
 ui._popup_slide.reset()
 ui._popup_slide.top(time.monotonic()-1.,82,112,urgent=True)
 texts.clear()
 ui.render_to_file(replace(braking,driving_notices=()),camera_path)
 assert camera_title in texts
 with Image.open(camera_path) as camera:
  rgb=camera.convert('RGB')
  assert max(abs(a-b) for a,b in zip(rgb.getpixel((315,175)),(31,70,143),strict=True))<=2
  assert (177,220,255) in rgb.crop((205,100,275,175)).getdata()
 add_event('carrotMan',log.Event.new_message(carrotMan={'desiredSource':'vturn','desiredSpeed':60}))
 record['mono']['carrotMan']+=1
 remote.update()
 assert source._with_live_hud_state(cruise).deceleration_banner=='커브 감속 중\\n목표 60 km/h'
 attention_path=popup_path.with_name('hud-driver-attention.png')
 attention=ClusterAlert('전방을 주시하세요','운전자 부주의',size=3,status=2,alert_type='driverDistracted3/permanent')
 texts.clear()
 ui.render_to_file(replace(braking,alert=attention),attention_path)
 assert attention.text1 in texts and attention.text2 in texts and camera_title not in texts
 with Image.open(attention_path) as frame:
  assert (255,225,91) in frame.convert('RGB').crop((470,55,650,175)).getdata()
 camera_state=replace(braking,driving_notices=())
 expired_state=replace(camera_state,deceleration_banner=None)
 with patch('time.monotonic',return_value=1000.):
  ui.render_to_file(camera_state,popup_path)
 for now,visible,at_rest in ((1000.05,True,False),(1000.30,True,True),(1001.29,True,True),
                             (1001.30,True,True),(1001.44,True,False),(1001.53,False,False)):
  texts.clear()
  with patch('time.monotonic',return_value=now):
   ui.render_to_file(expired_state,popup_path)
  assert (camera_title in texts)==visible, (now,texts)
  with Image.open(popup_path) as frame:
   pixel=frame.convert('RGB').getpixel((315,175))
   assert (max(abs(a-b) for a,b in zip(pixel,(31,70,143),strict=True))<=2)==at_rest, (now,pixel)
finally:
 ui.close()
assert ui._telemetry_font is None and not ui._window_open
'''


def main(argv=None):
  if not __debug__:
    raise RuntimeError('Validation requires assertions; do not run Python with -O')
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument('--model', required=True)
  parser.add_argument('--bundle', type=Path, required=True)
  parser.add_argument('--output', type=Path, required=True)
  parser.add_argument('--snapshot', type=Path, help='Use an already generated synthetic HUD fixture (for Windows native rendering)')
  args = parser.parse_args(argv)
  if Path('/TICI').exists() or Path('/etc/nv_tegra_release').exists():
    raise RuntimeError('Run native package validation on a PC')
  sys.path[:0] = list(map(str, [ROOT, ROOT / 'third_party/jetlink', ROOT / 'tools/jetlink',
                              ROOT / 'openpilot/selfdrive/carrot/cluster']))
  from openpilot.common import big_model_catalog as catalog
  if args.model not in catalog.available():
    raise ValueError('Unknown or unpaired PV5 model')
  release, spec = catalog.usb_release(args.model), catalog.model_spec(args.model)
  args.output = args.output.resolve()
  args.output.mkdir(parents=True, exist_ok=True)
  (args.output / 'bundle-check.json').unlink(missing_ok=True)
  with args.bundle.open('rb') as f:
    digest = hashlib.file_digest(f, 'sha256').hexdigest()
  if (digest, args.bundle.stat().st_size) != (release['bundle']['sha256'], release['bundle']['size']):
    raise ValueError('Archive is not the pinned published PV5 package')
  if args.snapshot:
    if args.snapshot.stat().st_size > 96 * 1024:
      raise ValueError('Oversized synthetic snapshot')
    record = json.loads(args.snapshot.read_text(encoding='utf-8'))
  else:
    from jetlink.spec import ModelSpec
    from openpilot.cereal import log
    from test_pv5_model_integration import center_scenario, publish_center
    assist, _, _, model, car, now, correction = center_scenario(ModelSpec.from_dict(spec), .27)
    controls = publish_center(assist, correction, 20.)
    controls.controlsState.lateralControlState.init('angleState')
    controls.controlsState.desiredCurvature = .002
    events = {'modelV2': model, 'carState': car, 'controlsState': controls,
              'deviceState': log.Event.new_message(deviceState={'started': True}),
              'carControl': log.Event.new_message(carControl={'latActive': True})}
    record = dict(version=1, sent=now, params={}, received=dict.fromkeys(events, now),
                  valid=dict.fromkeys(events, True), alive=dict.fromkeys(events, True),
                  mono=dict.fromkeys(events, int(now*1e9)),
                  events={name: base64.b64encode(event.to_bytes()).decode() for name, event in events.items()})
  fixture = args.output / 'synthetic-hud-input.json'
  fixture.write_text(json.dumps(record), encoding='utf-8')
  with tempfile.TemporaryDirectory(prefix='pv5-native-bundle-') as temporary:
    root = Path(temporary)
    with tarfile.open(args.bundle) as archive:
      members = archive.getmembers()
      if (sum(m.size for m in members) > 512 << 20 or any(not (m.isfile() or m.isdir() or m.issym()) or
          Path(m.name).is_absolute() or m.name.startswith('/') or '..' in Path(m.name).parts for m in members)):
        raise ValueError('Unexpected archive members')
      for member in members:
        if member.issym() and (Path(member.linkname).is_absolute() or
            not (root / member.name).parent.joinpath(member.linkname).resolve().is_relative_to(root.resolve())):
          raise ValueError('Archive link escapes package')
      archive.extractall(root, filter='data')
    if (root / 'SOURCE_COMMIT').read_text().strip() != release['source_commit']:
      raise ValueError('Package source identity mismatch')
    for name in ('cinque_v2.json', 'jetson_model.json'):
      if json.loads((root / 'openpilot/selfdrive/modeld/jetlink' / name).read_text()) != spec:
        raise ValueError('Package model pin mismatch')
    # Drop checkout imports; retain externally installed/test dependencies only.
    deps = [p for p in sys.path if p and not Path(p).resolve().is_relative_to(ROOT)]
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1', PYTHONUTF8='1',
               PYTHONPATH=os.pathsep.join(map(str, [root, root / 'tools/jetlink', root / 'third_party/jetlink',
                   root / 'openpilot/selfdrive/carrot/cluster', *deps])))
    env.pop('CARROT_PERF_DIAGNOSTICS', None)
    for command in ([sys.executable, '-P', str(root / 'tools/jetlink/server.py'), '--check-imports'],
                    [sys.executable, '-P', '-c', PROBE, str(fixture), str(root / 'hud-native.png')]):
      result = subprocess.run(command, cwd=root, env=env, timeout=60, capture_output=True)
      if result.returncode:
        raise RuntimeError('Isolated package probe failed: ' +
                           result.stderr.decode('utf-8', errors='replace')[-2000:])
    # Native raylib cannot export to all Unicode paths on Windows.
    shutil.copyfile(root / 'hud-native.png', args.output / 'hud.png')
    shutil.copyfile(root / 'hud-popup.png', args.output / 'hud-popup.png')
    shutil.copyfile(root / 'hud-camera-decel.png', args.output / 'hud-camera-decel.png')
    shutil.copyfile(root / 'hud-driver-attention.png', args.output / 'hud-driver-attention.png')
  report = dict(model=args.model, model_sha256=spec['sha256'], package_source=release['source_commit'],
                bundle_sha256=digest, isolated_imports=True, native_hud_render=True,
                center_and_lane_snapshot=True, inference_readout=True, driving_popup=True, driving_notices=True,
                deceleration_details=True, driver_attention_icon=True, popup_hold=True, popup_exit=True,
                condition_driven_popups=True,
                diagnostics_off=True, physical_device_tested=False,
                fixture_scope='synthetic eligible lane/control state; real schema, remote adapter and native renderer')
  (args.output / 'bundle-check.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
  print(json.dumps(report))
  return 0


if __name__ == '__main__':
  raise SystemExit(main())
