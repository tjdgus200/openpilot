"""Opt-in Jetson HUD comparison profiles; default invocation only prints a plan.

Use the existing renderer/USB protocol, never a second inference server. This
tool does not install dependencies, edit Params/systemd, stop services or deploy.
"""
import argparse
from dataclasses import asdict, dataclass
import importlib
import json
import math
import os
from pathlib import Path
import shlex
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
PARAM = 'JetsonHudQuality'
RUN_PROFILE = None
RUN_FPS = False


@dataclass(frozen=True)
class Profile:
  codec: str
  bitrate: str = 'auto'
  quality: int | None = None
  jpeg_encoder: str | None = None


PROFILES = {
  'baseline': Profile('h264'),
  'h264-4m': Profile('h264', bitrate='4M'),
  'h264-8m': Profile('h264', bitrate='8M'),
  'h264-12m': Profile('h264', bitrate='12M'),
  'jpeg-q68': Profile('jpeg', quality=68, jpeg_encoder='turbojpeg'),
  'jpeg-q85': Profile('jpeg', quality=85, jpeg_encoder='turbojpeg'),
  'jpeg-q92': Profile('jpeg', quality=92, jpeg_encoder='turbojpeg'),
  'jpeg-pillow-q85': Profile('jpeg', quality=85, jpeg_encoder='pillow'),
}
NAMES = tuple(PROFILES)
SHORT_NAMES = ('H-auto', 'H4M', 'H8M', 'H12M', 'J68', 'J85', 'J92', 'P85')


def selection(value):
  try:
    index = int(value)
  except (TypeError, ValueError, OverflowError):
    return 0
  return index if not isinstance(value, bool) and 0 <= index < len(NAMES) else 0


class CpuJpegError(RuntimeError):
  pass


def codec_options(name):
  profile = PROFILES[name]
  args = ['--usb-codec', profile.codec]
  if profile.codec == 'h264':
    args += ['--usb-h264-backend', 'ffmpeg', '--usb-h264-ffmpeg-encoder', 'libx264',
             '--usb-h264-ffmpeg-threads', '1', '--usb-h264-gop', '1',
             '--usb-h264-bitrate', profile.bitrate]
  else:
    args += ['--usb-jpeg-encoder', profile.jpeg_encoder, '--usb-jpeg-quality', str(profile.quality)]
  return args


def renderer_options(name, seconds=60):
  if isinstance(seconds, bool) or not math.isfinite(seconds) or not 1 <= seconds <= 600:
    raise ValueError('Duration must be between 1 and 600 seconds')
  args = ['--input', 'live', '--output', 'usb', '--fps', '10',
          '--cluster-hud-mode', '1', '--duration', f'{seconds:g}',
          '--profile-render', '--profile-interval', '5']
  return [*args, *codec_options(name)]


def plan(name, seconds=60):
  argv = [sys.executable, str(ROOT / 'tools/jetlink/hud.py'), '--jetson-quality-profile', name,
          *renderer_options(name, seconds)]
  return {'profile': name, 'settings': asdict(PROFILES[name]), 'target_fps': 10,
          'duration_seconds': seconds, 'argv': argv, 'command_linux': shlex.join(argv),
          'prepared_only': True, 'device_validated': False,
          'note': 'Generate on the target runtime to resolve its Python/root paths; no service changes.'}


def write_candidates(directory, seconds=60):
  directory = Path(directory)
  directory.mkdir(parents=True, exist_ok=True)
  values = {name: json.dumps(plan(name, seconds), ensure_ascii=False, indent=2) + '\n' for name in PROFILES}
  # Check the entire set before writing, so a conflicting old plan is retained.
  for name, text in values.items():
    path = directory / f'{name}.json'
    if path.exists() and path.read_text(encoding='utf-8') != text:
      raise FileExistsError(f'Existing candidate differs: {path.name}')
  for name, text in values.items():
    path = directory / f'{name}.json'
    if not path.exists():
      with path.open('x', encoding='utf-8', newline='\n') as output:
        output.write(text)


def preflight(name):
  if sys.platform != 'linux':
    raise RuntimeError('Execution is Jetson-only; omit --run to prepare on this PC')
  model_path = Path('/sys/firmware/devicetree/base/model')
  if not model_path.is_file() or b'Jetson' not in model_path.read_bytes():
    raise RuntimeError('Jetson model identity unavailable')
  # Never stop or replace the running HUD. Its owner must arrange a bench
  # session separately; the existing panel_owner lock remains the final guard.
  result = subprocess.run(['systemctl', 'is-active', 'carrot-jetlink-hud.service'],
                          capture_output=True, text=True, timeout=5, check=False)
  if result.returncode != 3 or result.stdout.strip() not in ('inactive', 'failed'):
    raise RuntimeError('HUD service must already be inactive; this tool will not stop it')
  profile = PROFILES[name]
  if profile.jpeg_encoder == 'turbojpeg':
    importlib.import_module('hud_cpu_jpeg').Encoder()  # Validate the pinned native runtime, without encoding.
  elif profile.jpeg_encoder == 'pillow':
    importlib.import_module('PIL.Image')


def execute(name, seconds):
  preflight(name)
  env = {**os.environ, 'OPENBLAS_NUM_THREADS': '1', 'OMP_NUM_THREADS': '1', 'PYTHONUNBUFFERED': '1'}
  env.setdefault('DISPLAY', ':0')
  if not env.get('XAUTHORITY'):
    authority = Path(f'/run/user/{os.getuid()}/gdm/Xauthority')
    if not authority.is_file():
      raise RuntimeError('Set the existing HUD session XAUTHORITY before running')
    env['XAUTHORITY'] = str(authority)
  # Match the operating HUD's priority/CPU placement without changing any
  # other process. These settings and the environment survive execve().
  cpus = os.sched_getaffinity(0).intersection({2, 3, 4, 5})
  if not cpus:
    raise RuntimeError('Operating HUD CPU affinity unavailable')
  os.sched_setaffinity(0, cpus)
  os.nice(max(0, 19 - os.getpriority(os.PRIO_PROCESS, 0)))
  argv = plan(name, seconds)['argv']
  os.execve(sys.executable, argv, env)


def main(argv=None):
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument('--profile', choices=PROFILES, default='baseline')
  parser.add_argument('--seconds', type=float, default=60)
  action = parser.add_mutually_exclusive_group()
  action.add_argument('--list', action='store_true')
  action.add_argument('--write-candidates', type=Path)
  action.add_argument('--run', action='store_true', help='Explicitly run one profile on an idle Jetson HUD')
  args = parser.parse_args(argv)
  try:
    selected = plan(args.profile, args.seconds)
    if args.run:
      execute(args.profile, args.seconds)
    elif args.write_candidates:
      write_candidates(args.write_candidates, args.seconds)
      print(f'Prepared {len(PROFILES)} candidate plans; no runtime changed')
    else:
      print(json.dumps({name: plan(name, args.seconds) for name in PROFILES} if args.list else selected,
                       ensure_ascii=False, indent=2))
  except (OSError, ValueError, RuntimeError, ImportError, subprocess.TimeoutExpired) as exc:
    parser.exit(1, f'Candidate not started: {exc}\n')


if __name__ == '__main__':
  main()
