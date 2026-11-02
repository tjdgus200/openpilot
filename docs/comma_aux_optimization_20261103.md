# Comma auxiliary CPU optimization, 2026-11-03

## Approved scope

Apply the four reviewed auxiliary-work candidates. Control algorithms, CAN,
BLE collection, model inputs/outputs, driver monitoring, saved HUD Q92/FPS,
and existing PV5 behavior are unchanged. This is comma source work, with no
vehicle installation, Jetson runtime change or package-pin change.

1. CarrotServ retains the existing typed Params getters/conversions and20Hz
   update call. One nonblocking Linux inotify read plus directory identity stat
   invalidates the settings snapshot. All23 read-site keys are watched;22 reads
   normally execute, with the23rd only for fallback language. Atomic rename,
   in-place writes and deletion invalidate before the next navigation update.
   Overflow, directory/symlink replacement, lost/unsupported watch or failure
   falls back to the original per-tick reads for the rest of that object life.
   Failed/partial settings reads are never marked loaded. No TTL, worker,
   debounce delay, settings-write policy or global Params semantics is added.
   These reads retain the original non-transactional multi-key semantics.
2. modeld records private UsbGpuNavModel at startup/load/fallback/recovery using
   the loaded model's same nav_map views contract as MapInput. It has the same
   clear flags as UsbGpuActive, no default/persistent user setting, no per-frame
   write. mapsd checks it at the existing1s demand cadence. Missing status from
   older producers conservatively retains the old UsbGpuActive demand. Fresh
   Jetlink nav-model demand, HUD mode7 and the existing web-demand lease remain.
   Unsupported local eGPU models no longer cause unused rendering. If that
   case was not occurring, this item provides no rendering reduction.
3. Reuse only fixed route coordinates/projection scale and latest immutable
   tile-set concatenations. Dynamic pose, heading, style latitude, road-distance
   and validity calculations still execute. Route keys own their data and
   compare value/shape/dtype, so in-place changes and precision changes invalidate.
   Replacing decoded tiles or changing the ordered tile set invalidates the
   merged view. One merged allocation is capped at2MiB; referenced decoded tiles
   may retain up to a further2MiB beyond the existing128-tile LRU. Route cache
   holds at most4096 points (128KiB for key+Mercator); tracker adds an owned
   route/path (128KiB at4096 points). Oversized data uses the original uncached
   calculation. In the benchmark the merged view was62.5KiB.
4. PNG preview encoding runs only for existing HUD mode7 or a fresh web preview
   lease, after an actual render. The model VisionIPC image/send path is separate.
   A stopped source's image timestamps/mtime are not renewed by cached output.
   Existing preview2s expiry, model-map500ms lifetime, instruction2s lifetime,
   route IDs, invalid black frames, slow-render rejection, CPU budget and
   little-core/nice15 placement remain.

## Verification and limits

The focused Linux checks use real inotify, tmpfs/files and the production
Cython rasterizer compiled with -fno-fast-math -ffp-contract=off. They compare
bit-exact images, moving pose, route value/dtype changes, route ownership,
cache bounds/invalidation, settings changes/failure, model/preview demand and
actual mapsd loop frame publication/black invalidation. CarrotServ's production
update_params method is isolated from native IPC and exercised against real
file-backed typed getters; this is not a native Params integration test.

97 focused tests passed with native rasterization and real Linux notifications.
The wider existing HUD/model/USB/battery suite passed863 tests and3 subtests,
with5 device/native-dependent skips and one unavailable loggerd source check
deselected. Native msgq is unavailable in this PC environment; the full navd
route-engine IPC test was not run. The companion artifact records both suites.
No on-device AGNOS/full native Params build, physical C3 CPU, Jetson inference
P99, live GPS/map service or road behavior was validated. No production timing
counters, profiling UI or continuous benchmark workers were added. Normal
source installation must rebuild native Params for the added private key.

## Offline comparison

Reproduce manually with tools/benchmark_comma_aux.py --base
9254cb32f34c834876aa8670750a0323fdd224a7 --output result.json in Linux.
For native rasterization, build the existing navigation/_rasterizer.pyx using
its SConscript's floating-point flags first. The local run_pc.py harness builds
that same source only into a temporary PC directory. No devices are used.

Results are medians of8 CPU-time batch means per variant in ABBA order, with
warm caches. The map workload uses4 synthetic immutable tiles,1280 segments,
16 moving poses and256/4096-point routes. It compares trusted baseline method
bodies with current code and checks image/pose equivalence before timing.
The Params backend is a Python adapter reading real tmpfs files, not the
native C++ binding, so its timing ratio cannot predict comma Params CPU savings.
PNG is a representative rendered256x256 map at compression level1.

| Workload | Before | After | Scope reduction |
| --- | ---: | ---: | ---: |
| Unchanged settings update, Python file adapter | 0.1548ms | 0.00126ms | 99.2% of this adapter workload |
| 4096-point route projection | 0.0593ms | 0.0283ms | 52.3% |
| Full map render,256-point route | 1.549ms | 1.538ms | 0.7% |
| Full map render,4096-point route | 1.749ms | 1.678ms | 4.1% |
| PNG call without preview consumer | 0.7085ms | skipped | Encoding removed |

The two exploratory runs varied: whole-render savings were0.7-0.9% for256
points and3.2-4.1% for4096 points. This is not a confidence interval or an ARM
prediction. See docs/artifacts/comma_aux_20261103.json for final measured times. Avoided
file operations are440/s in the unchanged-settings20Hz case; the directory
stat and nonblocking event read still occur each tick. Consumer-free map
rendering drops from up to10/s to0 only in the old unconditional eGPU-demand
case; black VisionIPC keepalive remains approximately1Hz. Unused PNG encoding
is skipped (up to2/s with an active model map, or approximately1/s while idle).
When HUD/web preview is needed, its encoding cost remains. Rendering needed by
navigation models is never removed.

Do not add these percentages or describe them as whole-device CPU reduction.
The small whole-render improvement may be close to PC measurement variability;
the main structural gain is avoiding work for absent consumers. Timings and
savings on ARM/C3 require physical comparison. These tests support regression
confidence but do not constitute a road-safety certification.
