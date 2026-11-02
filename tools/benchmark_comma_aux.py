#!/usr/bin/env python3
"""Offline CPU microbenchmarks; never import this into a vehicle process.

Compare trusted Git baseline methods with current code, identical synthetic
inputs and warmed caches. No network, CAN, BLE, camera or inference execution.
"""
import ast
import argparse
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace as NS

import numpy as np

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from openpilot.common.params_watch import ParamsWatch
from openpilot.selfdrive.navigation import renderer
from openpilot.selfdrive.navigation.preview import write_preview
from openpilot.selfdrive.navigation.route import RouteTracker


def method(text, name, namespace):
  node = next(n for n in ast.walk(ast.parse(text)) if isinstance(n, ast.FunctionDef) and n.name == name)
  exec(compile(ast.Module(body=[node], type_ignores=[]), '<trusted-repository-method>', 'exec'), namespace)
  return namespace[name]


def measure_pair(before, after, count):
  times = [[], []]
  # Interleaved ABBA batches reduce, but do not eliminate, thermal/order bias.
  for _ in range(4):
    for index in (0, 1, 1, 0):
      fn = (before, after)[index]
      started = time.process_time_ns()
      for _ in range(count):
        fn()
      times[index].append((time.process_time_ns() - started) / count / 1e6)
  a, b = (float(np.median(values)) for values in times)
  return {'before_ms': a, 'after_ms': b, 'saved_ms': a - b, 'reduction_percent': 100 * (a - b) / a}


class FileParams:
  def __init__(self, path):
    self.path, self.reads = path, 0

  def get_param_path(self):
    return str(self.path)

  def get(self, key):
    self.reads += 1
    return (self.path / key).read_text()

  def get_int(self, key):
    return int(self.get(key))

  def get_float(self, key):
    return float(self.get(key))

  def get_bool(self, key):
    return self.get(key) == '1'


def benchmark(base):
  def old(path):
    return subprocess.check_output(['git', 'show', f'{base}:{path}'], cwd=REPO, encoding='utf-8')
  path = 'openpilot/selfdrive/carrot/carrot_serv.py'
  original_settings = method(old(path), 'update_params', {})
  current_text = (REPO / path).read_text()
  current_settings = method(current_text, 'update_params', {})
  tree = ast.parse(current_text)
  call = next(n for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == 'ParamsWatch')
  keys = ast.literal_eval(call.args[1])
  result = {'baseline': base, 'scope': 'PC Linux CPU; file-backed Python Params adapter, synthetic map; not comma or Jetson performance',
            'native_rasterizer': renderer._native_draw_segments is not None}
  with tempfile.TemporaryDirectory(prefix='comma-aux-', dir='/dev/shm' if sys.platform.startswith('linux') else None) as temporary:
    root = Path(temporary)
    for key in keys:
      (root / key).write_text('main_ko' if key == 'LanguageSetting' else '1')
    original = NS(params=FileParams(root))
    current = NS(params=FileParams(root), _params_loaded=False)
    current._params_watch = ParamsWatch(current.params, keys)
    try:
      if current._params_watch.fd < 0:
        raise RuntimeError('Benchmark requires real Linux settings watch')
      original_settings(original)
      current_settings(current)
      a, b = original.params.reads, current.params.reads
      result['settings'] = measure_pair(lambda: original_settings(original), lambda: current_settings(current), 1000)
      result['settings']['reads_per_unchanged_tick'] = [a, current.params.reads - b]
      result['settings']['avoided_file_reads_per_second_at_20hz'] = a * 20
      (root / 'AutoNaviSpeedSafetyFactor').write_text('110')
      current_settings(current)
      assert current.autoNaviSpeedSafetyFactor == 1.1
    finally:
      current._params_watch.close()

    old_project = method(old('openpilot/selfdrive/navigation/route.py'), 'project', {
      'np': np, 'PROJECTION_WINDOW': (-30., 100.),
    })
    rng = np.random.default_rng(81)
    route = np.column_stack((127. + .0001 * np.sin(np.arange(4096) / 50), np.linspace(37., 37.02, 4096)))
    a, b = RouteTracker(route), RouteTracker(route)
    for lat in np.linspace(37., 37.01, 100):
      assert old_project(a, 127., lat) == b.project(127., lat)
    result['route_4096'] = measure_pair(lambda: old_project(a, 127., 37.01), lambda: b.project(127., 37.01), 1000)

    old_render = method(old('openpilot/selfdrive/navigation/renderer.py'), 'render', dict(vars(renderer)))
    for count in (256, 4096):
      # Four immutable decoded tiles, 1280 road segments in an approximately
      # 1km view, using the production dynamic projection and rasterization.
      source = NS(min_zoom=15, max_zoom=15)
      a, b = renderer.MapRenderer(source), renderer.MapRenderer(source)
      tiles = []
      for _ in range(4):
        xy = np.column_stack((127. + rng.uniform(-.005, .005, 320), 37. + rng.uniform(-.005, .005, 320)))
        tile = (renderer.mercator(xy), renderer.mercator(xy + .0002), rng.integers(0, 5, 320),
                rng.integers(-1, 2, 320), np.ones((320, 2), dtype=bool))
        for array in tile:
          array.flags.writeable = False
        tiles.append(tile)
      for obj in (a, b):
        obj.roads = lambda x, y=0, tiles=tiles: tiles[x]
        obj.view_tiles = lambda *_: ([(i, 0) for i in range(4)], True)
      route_part = route[np.linspace(0, len(route) - 1, count).astype(int)].copy()
      positions = [renderer.Position(37. + i * .000001, 127., i * 3.) for i in range(16)]
      for pos in positions:
        expected, actual = old_render(a, pos, route_part), b.render(pos, route_part)
        assert (expected.valid, expected.road_distance) == (actual.valid, actual.road_distance)
        np.testing.assert_array_equal(expected.image, actual.image)
      index = [0, 0]

      def draw(which, index=index, positions=positions, a=a, b=b, route_part=route_part):
        index[which] = (index[which] + 1) % len(positions)
        if which:
          return b.render(positions[index[which]], route_part)
        return old_render(a, positions[index[which]], route_part)

      result[f'render_{count}'] = measure_pair(lambda: draw(0), lambda: draw(1), 16)
      result[f'render_{count}']['merged_cache_bytes'] = sum(v.nbytes for v in b._merged)
      result[f'render_{count}']['route_cache_bytes'] = b._route.nbytes + b._route_mercator.nbytes

    image = actual.image
    result['unused_png'] = measure_pair(lambda: write_preview(image, root / 'map.png'), lambda: None, 100)
    result['unused_png']['avoided_encodes_per_second_when_10hz_model_map_active'] = 2
    # Avoided rendering depends on active consumers; do not sum overlapping wins.
    result['no_map_consumers'] = {'render_calls_per_second_before': 'up to 10 when UsbGpuActive',
                                  'render_calls_per_second_after': 0,
                                  'black_visionipc_keepalive_hz_after': 1}
  return result


if __name__ == '__main__':
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument('--base', default='9254cb32f34c834876aa8670750a0323fdd224a7')
  parser.add_argument('--output', type=Path)
  args = parser.parse_args()
  values = benchmark(args.base)
  text = json.dumps(values, indent=2)
  if args.output:
    args.output.write_text(text + '\n')
  print(text)
