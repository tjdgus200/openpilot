"""Offline PC microbenchmark, never imported or launched by vehicle processes.

Compares the recorded pre-change uniform sampler with current OFF/ON metering.
This is not a camera/NN replay, a device latency measurement or driving approval.
"""
import argparse
import hashlib
import json
from pathlib import Path
import statistics
import subprocess
import tempfile


ROOT = Path(__file__).resolve().parents[2]
HEADER = 'openpilot/system/camerad/cameras/model_fov_meter.h'
SOURCE = r'''
#include "meter.h"
#include <chrono>
#include <iostream>
#include <vector>
#include <cstdlib>
#include <cstring>

int main(int argc, char **argv) {
  if (argc != 3) return 1;
  const int mode = std::atoi(argv[1]);
  const bool assist = mode == 1 || mode == 3;
  const int scene = std::atoi(argv[2]);
  constexpr size_t bytes = 2048 * 1208;
  std::vector<uint8_t> pixels(bytes * 16);
  uint32_t seed = 37;
  for (size_t i = 0; i < pixels.size(); ++i) {
    seed = seed * 1664525U + 1013904223U;
    pixels[i] = scene == 0 ? 128 : scene == 1 ? uint8_t(seed >> 24) : scene == 3 ? (i % bytes / 2048 < 600 ? 255 : 2) :
      (i % bytes / 2048 < 600 ? 200 : uint8_t(3 + (seed >> 29)));
  }
  ModelFovMeter meter;
  if (!meter.configure(1928, 1208, true, {}, 4, 2048)) return 2;
#if CURRENT
  ExposureBoxTarget lane;
  lane.valid = true;
  lane.center_x = 1030;
#endif
  std::vector<double> elapsed;
  elapsed.reserve(12000);
  double checksum = 0;
  uint64_t bits_hash = 1469598103934665603ULL;
  for (int i = 0; i < 12500; ++i) {
    const uint8_t *frame = pixels.data() + (i % 16) * bytes;
    const auto start = std::chrono::steady_clock::now();
#if CURRENT
    const uint64_t now = uint64_t(i + 1) * 50000000ULL;
    lane.valid_until = now + 250000000ULL;
    if (!meter.measure(frame, bytes, assist, mode >= 2 ? &lane : nullptr, now)) return 3;
    const float grey = meter.assisted_grey(mode != 0, now);
#else
    if (!meter.measure(frame, bytes, assist)) return 3;
    const float grey = meter.assisted_grey(assist, uint64_t(i + 1) * 50000000ULL);
#endif
    const auto stop = std::chrono::steady_clock::now();
    // The baseline and OFF checksums must be exactly equal, including padding.
    checksum += grey + meter.black_fraction + meter.white_fraction;
    for(float value : {grey,meter.black_fraction,meter.white_fraction}) {
      uint32_t bits; std::memcpy(&bits,&value,sizeof(bits)); bits_hash=(bits_hash^bits)*1099511628211ULL;
    }
    if (i >= 500) elapsed.push_back(std::chrono::duration<double, std::micro>(stop - start).count());
  }
  double sum = 0;
  for (double value : elapsed) sum += value;
  std::sort(elapsed.begin(), elapsed.end());
  std::cout.precision(17);
  std::cout << "{\"avg_us\":" << sum / elapsed.size()
            << ",\"p99_us\":" << elapsed[size_t(.99 * (elapsed.size() - 1))]
            << ",\"bits_hash\":\"" << bits_hash << "\""
            << ",\"checksum\":" << checksum << ",\"meter_bytes\":" << sizeof(meter) << "}";
}
'''


def main():
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument('--baseline-ref', required=True)
  parser.add_argument('--baseline-header', type=Path,
                      help='Exact header exported from baseline-ref when Git is unavailable in the build environment')
  parser.add_argument('--output', type=Path, required=True)
  args = parser.parse_args()
  baseline = args.baseline_header.read_bytes() if args.baseline_header else subprocess.check_output(
    ['git', '-c', f'safe.directory={ROOT.as_posix()}', 'show', f'{args.baseline_ref}:{HEADER}'], cwd=ROOT)
  report = {'scope': 'PC cached sampler only; not C3X/Orin inference or physical exposure',
            'baseline_ref': args.baseline_ref, 'baseline_header_sha256': hashlib.sha256(baseline).hexdigest(),
            'samples_per_call': 2304, 'frames_per_round': 12000,
            'rounds': 5, 'frame_pool': 16, 'scenes': {}}
  with tempfile.TemporaryDirectory(prefix='pv5-ae-bench-') as tmp:
    folder = Path(tmp)
    binaries = {}
    for name, header in [('baseline', baseline), ('current', (ROOT / HEADER).read_bytes())]:
      src = folder / name
      src.mkdir()
      (src / 'meter.h').write_bytes(header)
      (src / 'lane_exposure.h').write_bytes((ROOT / HEADER).with_name('lane_exposure.h').read_bytes())
      (src / 'bench.cc').write_text(SOURCE)
      binary = src / 'bench'
      subprocess.run(['g++', '-O2', '-std=c++17', '-DCURRENT=' + str(int(name == 'current')),
                      str(src / 'bench.cc'), '-o', str(binary)], check=True, capture_output=True)
      binaries[name] = binary
    for scene in range(4):
      rounds = {mode: [] for mode in ['baseline', 'baseline_assist', 'off', 'on', 'lane', 'lane_assist']}
      for i in range(5):
        modes = list(rounds)
        for mode in modes[i % 3:] + modes[:i % 3]:
          binary = binaries['baseline' if mode.startswith('baseline') else 'current']
          rounds[mode].append(json.loads(subprocess.check_output([str(binary), str({'baseline':0,'baseline_assist':1,'off':0,'on':1,'lane':2,'lane_assist':3}[mode]), str(scene)])))
      assert all(a['checksum'] == b['checksum'] and a['bits_hash'] == b['bits_hash'] for a, b in zip(rounds['baseline'], rounds['off']))
      assert all(a['checksum'] == b['checksum'] and a['bits_hash'] == b['bits_hash'] for a, b in zip(rounds['baseline_assist'], rounds['on']))
      summary = {mode: {key: statistics.median(r[key] for r in runs)
                        for key in ['avg_us', 'p99_us', 'meter_bytes']} for mode, runs in rounds.items()}
      summary['off_exact_checksum_parity'] = True
      summary['static_assist_exact_checksum_parity'] = True
      summary['lane_minus_off_avg_us'] = summary['lane']['avg_us'] - summary['off']['avg_us']
      summary['on_minus_off_avg_us'] = summary['on']['avg_us'] - summary['off']['avg_us']
      report['scenes'][str(scene)] = {'summary': summary, 'rounds': rounds}
  args.output.parent.mkdir(parents=True, exist_ok=True)
  args.output.write_text(json.dumps(report, indent=2) + '\n')
  print(json.dumps({key: scene['summary'] for key, scene in report['scenes'].items()}, indent=2))


if __name__ == '__main__':
  main()
