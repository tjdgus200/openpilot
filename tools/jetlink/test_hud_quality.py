"""PC-only profile/CPU-JPEG/status checks, with no USB or CUDA access."""

import pytest
from contextlib import nullcontext
import ast
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import hashlib
import struct
from types import SimpleNamespace as NS

sys.path.insert(0, str(Path(__file__).parent))
import hud_quality as quality


def extract_class(path, name, namespace):
  node = next(node for node in ast.walk(ast.parse(path.read_text(encoding='utf-8'))) if isinstance(node, ast.ClassDef) and node.name == name)
  exec(compile(ast.Module(body=[node], type_ignores=[]), str(path), 'exec'), namespace)
  return namespace[name]


class TestProfile:
  def test_h264_comparisons_change_only_bitrate(self):
    baseline = quality.renderer_options('baseline')
    position = baseline.index('--usb-h264-bitrate') + 1
    for name, bitrate in [('h264-4m', '4M'), ('h264-8m', '8M'), ('h264-12m', '12M')]:
      with nullcontext():
        expected = baseline.copy()
        expected[position] = bitrate
        assert quality.renderer_options(name) == expected

  def test_every_profile_uses_existing_renderer_at_ten_fps(self):
    for name in quality.PROFILES:
      with nullcontext():
        value = quality.plan(name, 300)
        argv = value['argv']
        assert argv[:2] == [sys.executable, str(quality.ROOT / 'tools/jetlink/hud.py')]
        assert argv[argv.index('--fps') + 1] == '10'
        assert argv[argv.index('--duration') + 1] == '300'
        assert not value['device_validated']
        assert '--usb-async' not in argv
        assert '--usb-fast' not in argv
        assert '--cluster-hud-encoder' not in argv
        assert 'nvjpeg' not in ' '.join(argv)
        assert argv[argv.index('--output') + 1] == 'usb'

  def test_cpu_jpeg_explicit_encoder_and_quality(self):
    for name, q in [('jpeg-q68', 68), ('jpeg-q85', 85), ('jpeg-q92', 92), ('jpeg-pillow-q85', 85)]:
      with nullcontext():
        args = quality.renderer_options(name)
        assert args[args.index('--usb-codec') + 1] == 'jpeg'
        assert int(args[args.index('--usb-jpeg-quality') + 1]) == q
        expected = 'pillow' if 'pillow' in name else 'turbojpeg'
        assert args[args.index('--usb-jpeg-encoder') + 1] == expected

  def test_invalid_duration_rejected_before_start(self):
    for value in [0, -1, 601, float('nan'), float('inf'), True]:
      with nullcontext(), pytest.raises(ValueError):
        quality.renderer_options('baseline', value)

  def test_default_cli_only_prints_plan(self, monkeypatch):
    calls = []
    monkeypatch.setattr(quality, 'execute', lambda *args: calls.append(args))
    with redirect_stdout(io.StringIO()) as output:
      quality.main([])
    assert not calls
    assert json.loads(output.getvalue())['profile'] == 'baseline'

  def test_writing_candidates_is_repeatable_but_never_overwrites_different_plan(self):
    with tempfile.TemporaryDirectory() as temp:
      directory = Path(temp)
      quality.write_candidates(directory)
      quality.write_candidates(directory)
      assert len(list(directory.glob('*.json'))) == 8
      file = directory / 'jpeg-q85.json'
      file.write_text('retained', encoding='utf-8')
      with pytest.raises(FileExistsError):
        quality.write_candidates(directory)
      assert file.read_text(encoding='utf-8') == 'retained'

  def test_pc_cannot_start_device_comparison(self, monkeypatch):
    calls = []
    monkeypatch.setattr(quality.sys, 'platform', 'win32')
    monkeypatch.setattr(quality.os, 'execve', lambda *args: calls.append(args))
    with pytest.raises(RuntimeError, match='Jetson-only'):
      quality.execute('jpeg-q85', 60)
    assert not calls

  def test_active_service_is_never_stopped_or_duplicated(self, monkeypatch):
    monkeypatch.setattr(quality.sys, 'platform', 'linux')
    monkeypatch.setattr(quality.Path, 'is_file', lambda _: True)
    monkeypatch.setattr(quality.Path, 'read_bytes', lambda _: b'NVIDIA Jetson Orin Nano')
    calls = []

    def run(*args, **kwargs):
      calls.append(args)
      return NS(returncode=0, stdout='active\n')

    monkeypatch.setattr(quality.subprocess, 'run', run)
    with pytest.raises(RuntimeError, match='already be inactive'):
      quality.preflight('baseline')
    assert calls == [(['systemctl', 'is-active', 'carrot-jetlink-hud.service'],)]

  def test_missing_turbojpeg_does_not_silently_select_another_encoder(self, monkeypatch):
    monkeypatch.setattr(quality.sys, 'platform', 'linux')
    monkeypatch.setattr(quality.Path, 'is_file', lambda _: True)
    monkeypatch.setattr(quality.Path, 'read_bytes', lambda _: b'NVIDIA Jetson Orin Nano')
    monkeypatch.setattr(quality.subprocess, 'run', lambda *a, **kw: NS(returncode=3, stdout='inactive\n'))

    def unavailable(_):
      raise ModuleNotFoundError('turbojpeg')

    monkeypatch.setattr(quality.importlib, 'import_module', unavailable)
    with pytest.raises(ModuleNotFoundError):
      quality.preflight('jpeg-q85')

  def test_existing_auto_bitrate_baseline(self):
    sys.path.insert(0, str(quality.ROOT / 'openpilot/selfdrive/carrot/cluster'))
    from cluster_config import resolved_usb_h264_bitrate

    assert resolved_usb_h264_bitrate('auto', 10, 30) == '2333k'


class TestDisplay:
  def display(self, directory, fail=False):

    class Base:
      def send_jpeg(self, frame):
        if fail:
          raise RuntimeError('USB failed')
        return frame

      send_h264_chunk = send_jpeg

    namespace = {
      'cluster': NS(TuringUsbDisplay=Base),
      'time': NS(monotonic=lambda: 10.0),
      'Path': lambda _: directory / 'status.json',
      'json': json,
      'os': os,
      'vehicle_stats': NS(sample=lambda: NS(cpu_core_percents=[1, 2], memory_used_percent=30)),
      'remote_nav_preview': NS(fresh=lambda: None, status={'available': False}),
      'remote_signals': NS(status=lambda: ('unknown', False, False), enabled=False),
      'display_metrics': {},
      'self_quality': dict,
      'FrameRate': __import__('hud_rate').FrameRate,
      'read_snapshot': lambda: None,
      'VehicleNetworkAddress': lambda: NS(address=lambda: 'test'),
    }
    cls = extract_class(quality.ROOT / 'tools/jetlink/hud.py', 'Display', namespace)
    return cls()

  def test_jpeg_and_h264_publish_same_existing_health_format(self):
    with tempfile.TemporaryDirectory() as temp:
      directory = Path(temp)
      values = []
      for method in ['send_jpeg', 'send_h264_chunk']:
        display = self.display(directory)
        assert getattr(display, method)(b'frame') == b'frame'
        values.append(json.loads((directory / 'status.json').read_text()))
        assert display.next_status == 11.0
      assert values[0] == values[1]
      assert values[0]['updated'] == 10.0

  def test_failed_send_does_not_publish_healthy_status(self):
    with tempfile.TemporaryDirectory() as temp:
      directory = Path(temp)
      for method in ['send_jpeg', 'send_h264_chunk']:
        with pytest.raises(RuntimeError, match='USB failed'):
          getattr(self.display(directory, fail=True), method)(b'frame')
      assert not (directory / 'status.json').exists()

  def test_existing_pillow_cpu_encoder_produces_decodable_jpeg(self):
    try:
      from PIL import Image
    except ImportError:
      pytest.skip('Pillow is not installed on this PC')
    path = quality.ROOT / 'openpilot/selfdrive/carrot/cluster/cluster_usb_display.py'
    tree = ast.parse(path.read_text(encoding='utf-8'))
    method = next(node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == '_encode_jpeg_pillow')
    namespace = {'Any': object}
    exec(compile(ast.Module(body=[method], type_ignores=[]), str(path), 'exec'), namespace)
    rgba = bytes([230, 50, 30, 255]) * (32 * 16)
    fake = NS(jpeg_quality=85, _jpeg_buffer=io.BytesIO(), _profile_start=lambda: 0, _profile_add=lambda *args: None)
    data = namespace['_encode_jpeg_pillow'](fake, rgba, 32, 16)
    with Image.open(io.BytesIO(data)) as image:
      assert image.size == (32, 16)
      pixel = image.convert('RGB').getpixel((16, 8))
      assert all((abs(a - b) <= 4 for a, b in zip(pixel, (230, 50, 30), strict=True)))
    assert rgba == bytes([230, 50, 30, 255]) * (32 * 16)


class TestNativeH264:
  def test_real_cpu_h264_encodes_and_decodes_each_bitrate(self):
    ffmpeg = shutil.which('ffmpeg')
    if not ffmpeg:
      pytest.skip('Native FFmpeg unavailable')
    sys.path[:0] = [str(quality.ROOT), str(quality.ROOT / 'openpilot/selfdrive/carrot/cluster')]
    from cluster_config import resolved_usb_h264_bitrate
    from cluster_h264_pipeline import H264UsbPipeline

    width, height, count = (64, 96, 4)
    row = bytes((220, 40, 30, 255)) * (width // 2) + bytes((30, 200, 40, 255)) * (width // 2)
    frame = row * height
    for name in ['baseline', 'h264-4m', 'h264-8m', 'h264-12m']:
      with nullcontext():
        pipeline = object.__new__(H264UsbPipeline)
        pipeline.encoder_width, pipeline.encoder_height = (width, height)
        pipeline.fps, pipeline.gop, pipeline.ffmpeg_threads = (10, 1, 1)
        pipeline.bitrate = resolved_usb_h264_bitrate(quality.PROFILES[name].bitrate, 10, 30)
        command = pipeline._ffmpeg_command(ffmpeg, 'libx264', 'h264')
        assert command[command.index('-b:v') + 1] == pipeline.bitrate
        encoded = subprocess.run(command, input=frame * (count + 1), capture_output=True, check=True, timeout=15).stdout
        decoded = subprocess.run(
          [ffmpeg, '-v', 'error', '-f', 'h264', '-i', 'pipe:0', '-f', 'rawvideo', '-pix_fmt', 'rgba', 'pipe:1'],
          input=encoded,
          capture_output=True,
          check=True,
          timeout=15,
        ).stdout
        assert len(decoded) == len(frame) * count
        for index in range(count):
          for x, expected in [(width // 4, (220, 40, 30)), (3 * width // 4, (30, 200, 40))]:
            offset = index * len(frame) + (height // 2 * width + x) * 4
            assert all((abs(a - b) <= 8 for a, b in zip(decoded[offset : offset + 3], expected, strict=True)))


class TestProfileGate:
  def test_profile_watcher_exits_for_new_fps_even_onroad(self):
    source = quality.ROOT / 'tools/jetlink/hud.py'
    selected, allowed = [3], [False]
    namespace = {'selection': __import__('hud_rate').selection, 'PARAM': 'JetsonHudFps', 'override': None,
                 'requested': 0, 'settings': NS(get_int=lambda _: selected[0]),
                 'profile_allowed': lambda _: allowed[0]}
    watcher = extract_class(source, 'QualityWatch', namespace)()
    assert watcher.read() == 1
    namespace['requested'] = 3
    assert watcher.read() == 0
    namespace['override'] = 5
    selected[0] = 0
    assert watcher.read() == 0

  def test_jpeg_failure_recovers_once_without_crediting_the_failed_profile(self):
    source = quality.ROOT / 'tools/jetlink/hud.py'
    main = next(n for n in ast.parse(source.read_text(encoding='utf-8')).body if isinstance(n, ast.FunctionDef) and n.name == 'main')
    loop = next(n for n in reversed(main.body) if isinstance(n, ast.While))
    contexts, entered, closed, commands = [], [], [], []

    class Owner:
      def __enter__(self):
        entered.append(True)

      def __exit__(self, *args):
        closed.append(True)

    local_quality, runtime = NS(RUN_PROFILE=None), NS(QUALITY_STATUS={})

    def render(**kwargs):
      contexts.append(local_quality.RUN_PROFILE)
      commands.append(list(namespace['sys'].argv))
      if len(contexts) == 1:
        raise quality.CpuJpegError('test encoder failure')

    namespace = {'selection': __import__('hud_rate').selection, 'PARAM': 'JetsonHudFps', 'override': None,
                 'settings': NS(get_int=lambda _: 3), 'profile_allowed': lambda _: True,
                 'hud_quality': local_quality, 'hud_runtime_stats': runtime,
                 'sys': NS(argv=[]), 'base': ['hud.py'], 'codec_options': quality.codec_options,
                 'NAMES': quality.NAMES, 'RATES': (10,15,20,25,30), 'extra_args': [], 'cluster': NS(main=render),
                 'panel_owner': lambda **kw: Owner(), 'CpuJpegError': quality.CpuJpegError,
                 'QualityWatch': lambda: NS(read=lambda: 0)}
    runner = ast.FunctionDef(name='run', args=ast.arguments(posonlyargs=[], args=[], kwonlyargs=[], kw_defaults=[], defaults=[]),
                             body=[loop], decorator_list=[])
    exec(compile(ast.fix_missing_locations(ast.Module(body=[runner], type_ignores=[])), str(source), 'exec'), namespace)
    namespace['run']()
    assert contexts == [(3, 3, ''), (3, 0, 'CPU JPEG unavailable; using baseline H.264')]
    assert commands[0][commands[0].index('--usb-jpeg-quality') + 1] == '92'
    assert commands[0][-2:] == ['--fps', '25']
    assert commands[1][commands[1].index('--usb-codec') + 1] == 'h264'
    assert commands[1][-2:] == ['--fps', '10']
    assert len(entered) == len(closed) == 2
    assert runtime.QUALITY_STATUS == {}

  def test_fps_watcher_does_not_poll_vehicle_or_write_model_settings(self):
    source = quality.ROOT / 'tools/jetlink/hud.py'
    tree = ast.parse(source.read_text(encoding='utf-8'))
    watcher = next(n for n in ast.walk(tree) if isinstance(n,ast.ClassDef) and n.name=='QualityWatch')
    calls=[n for n in ast.walk(watcher) if isinstance(n,ast.Call)]
    assert {n.func.attr for n in calls if isinstance(n.func,ast.Attribute)} == {'get_int'}
    assert not any(isinstance(n,ast.Name) and n.id=='quality_vehicle' for n in ast.walk(tree))

  def test_bundled_library_hash_and_arm64_elf(self):
    from hud_cpu_jpeg import LIBRARY, LIBRARY_SHA256

    data = LIBRARY.read_bytes()
    assert hashlib.sha256(data).hexdigest() == LIBRARY_SHA256
    assert data[:6] == b'\x7fELF\x02\x01'
    assert struct.unpack_from('<H', data, 18)[0] == 183

  def test_cpu_encoder_passes_rgba_unchanged_and_bounds_output(self, monkeypatch):
    try:
      import numpy as np
    except ImportError:
      pytest.skip('NumPy unavailable in this Python environment')
    import hud_cpu_jpeg

    seen = []

    def encode(data, **kwargs):
      seen.append((data.copy(), kwargs))
      return b'\xff\xd8\xff\xd9'

    monkeypatch.setattr(hud_cpu_jpeg, 'load', lambda: (NS(TJPF_RGBA=7, TJSAMP_420=2), NS(encode=encode)))
    encoder = hud_cpu_jpeg.Encoder()
    rgba = bytes(range(256)) * 4
    assert encoder.encode(rgba, 16, 16, 85) == b'\xff\xd8\xff\xd9'
    assert seen[0][0].tobytes() == rgba
    assert seen[0][1] == {'quality': 85, 'pixel_format': 7, 'jpeg_subsample': 2}
    assert seen[0][0].dtype == np.uint8
    for width, height, q in [(1922, 16, 85), (16, 16, 96), (15, 16, 85)]:
      with pytest.raises(RuntimeError):
        encoder.encode(rgba, width, height, q)
