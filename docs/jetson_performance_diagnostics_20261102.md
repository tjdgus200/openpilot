# Production performance instrumentation

The owner requested removal of performance-measurement/display overhead on
2026-11-02. Normal launches now leave `CARROT_PERF_DIAGNOSTICS` unset.
Only the exact value `1`, set before starting the relevant private test processes,
enables the retained performance diagnostic tools. No web toggle or automatic
enable-on-Park behavior is added.

In normal operation:

- No `hud-runtime-stats` thread, local control-socket statistics queries, GPU-load
  reads, HUD quality trial history or comparison JSON from that sampler.
- No `input-path-stats` thread or two 6000-row receive-to-TX-ready windows.
  Qualified N16 fusion, its startup input/output/state parity checks, CPU fallback
  and fresh Park/zero/inactive path selection remain active and independent.
- No served-frame samples appended for server FPS/mean/P99 summaries. Basic
  protocol response timing fields remain wire-compatible; they are not a claim
  of measured end-to-end latency. Output validation and GPU completion waits stay.
- No extra TX-ready timestamp observations, source/USB/output FPS counters or
  separate USB-rate heartbeat file. The normal HUD connection heartbeat remains.
- No rolling runtimeTiming aggregates/scheduler reads in modeld, jetlinkd or
  plannerd. Error/slow-frame logs, process health and service frequency checks
  remain. These checks must never be confused with optional benchmarking.
- No HUD FPS/inference AVG/P99/MAX or resource-performance footer. Temperature,
  power, early black boot resources, BSD/brightness, battery data and trip SOC
  remain. The Tools card omits both performance panels unconditionally, even
  when older hosts send measurements. Their table-only path buttons are also
  removed. Saved Q92/FPS/fusion values and the normal settings controls remain.

Saved Q92 and 10/15/20/25/30 target rates, model-first idle budget, transport
deadlines, receive timestamps used for freshness/ownership, watchdogs, bounded
queues, CRC, full output/state validation and safety alerts are unchanged.
Explicit render profiling and offline benchmark tools are still available.

PC tests verify diagnostics off/on admission, absence of diagnostic worker/sample
allocation, byte-identical framing, model outputs/recurrent feedback, FPS
selection and existing ownership/reconnect checks. Test results and four package
source/hash records are retained in the workstation task artifacts. They do not
measure actual Jetson CPU savings, thermal behavior, P99, BLE/USB or road safety.

All four model packages must carry this change before their PV5 USB pins move;
keep each existing model hash/size, distinct source identity and paired runtime.

- flip-flop: separate committed package identity; catalog `md-v3.json`.

- ctv3m: separate committed package identity; catalog `ctv3m.json`.

- light-speed: separate committed package identity; catalog `light-speed.json`.

## Package delivery

PC389 passed; one unrelated native-FFmpeg test skipped. Web9 passed, build and user-doc validator passed. Four isolated bundles pass imports, production-off worker checks, HUD/boot resources and model queue parity. All16 public assets were downloaded anonymously and matched local size/SHA256. Model identities and N16 default remain unchanged; no physical device was installed or tested.

- [n16](https://github.com/tjdgus200/openpilot/releases/tag/jetson-pv5-no-perf-n16-20261102-f9a5b680), source `f9a5b680`.
- [flip-flop](https://github.com/tjdgus200/openpilot/releases/tag/jetson-pv5-no-perf-flip-flop-20261102-3f8c86de), source `3f8c86de`.
- [ctv3m](https://github.com/tjdgus200/openpilot/releases/tag/jetson-pv5-no-perf-ctv3m-20261102-76adb819), source `76adb819`.
- [light-speed](https://github.com/tjdgus200/openpilot/releases/tag/jetson-pv5-no-perf-light-speed-20261102-d1cd52b9), source `d1cd52b9`.
