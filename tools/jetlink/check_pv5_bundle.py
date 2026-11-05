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
import json,sys,time
from pathlib import Path
from dataclasses import replace
import server,hud,hud_health,hud_protocol,boot_status
from model_pin import read_spec
from cluster_live import OpenpilotLiveSource,standby_state
from openpilot.selfdrive.carrot import signal_mode
from openpilot.common.performance_diagnostics import ENABLED
assert not ENABLED and not server.PERF_DIAGNOSTICS
record=json.loads(Path(sys.argv[1]).read_text())
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
assert boot_status.model_name(read_spec(server.ROOT)['sha256'])!='--'
hud_health._fresh=lambda *_:{'severity':'ok','thermal_severity':'ok','temp_c':59.,'power_w':12.4}
signal_mode.signal_mode=lambda:0
ui=hud_health.HealthRenderer(theme_mode='dark',screen_mode=8)
try:
 ui.open(hidden=True)
 assert ui._telemetry_font is not None
 assert ui._telemetry_font.texture.id!=hud_health.rl.get_font_default().texture.id
 ui.render_to_file(state,Path(sys.argv[2]))
 from PIL import Image
 picture=Image.open(sys.argv[2]).convert('RGB')
 assert picture.size==(1920,480) and len(picture.getcolors(1000000))>50
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
  report = dict(model=args.model, model_sha256=spec['sha256'], package_source=release['source_commit'],
                bundle_sha256=digest, isolated_imports=True, native_hud_render=True,
                center_and_lane_snapshot=True, diagnostics_off=True, physical_device_tested=False,
                fixture_scope='synthetic eligible lane/control state; real schema, remote adapter and native renderer')
  (args.output / 'bundle-check.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
  print(json.dumps(report))
  return 0


if __name__ == '__main__':
  raise SystemExit(main())
