# Jetson HUD20: bounded pipeline and derived-data caches

## 2026-11-13 snapshot publisher recovery

A reported failure leaves the Jetson HUD waiting for comma data while external
inference remains active, until the comma is restarted. The installed failure
cause has not been confirmed. A matching source defect is reproducible: if the
owned `hud_publisher.py` child exits, the existing `Publisher` keeps returning no
packet for the remaining model session and never starts another snapshot child.

`hud_protocol.py` now uses `hud_recovery.py` to request replacement of an exited
owned snapshot child from a temporary low-priority worker. Healthy operation
creates no recovery monitor thread or timer. The packet caller does not spawn,
wait for or terminate a process. Failed attempts back off from2 to30 seconds;
at most one recovery is pending. A live but stale child is left alone, so slow
inference or source-data loss does not trigger speculative process termination.

Model/session/transport ownership, model-first send admission, FPS preferences,
wire formats and the original300ms sender/500ms receiver freshness rules are
unchanged. Recovery does not reset USB or reopen the display. Session-specific
packet names isolate late cleanup. Closing during a restart retires the late
owned child. Initial snapshot spawn failure stays optional; media behavior is
otherwise unchanged. Existing logging records recovery attempts without keys,
payloads or vehicle data; no additional telemetry sampler is enabled.

The PC fixture returns no new frame from the old source after the producer exits,
then receives a new frame after one replacement with the patched Publisher.208
focused PC checks cover this lifecycle, an actual local child exit, backoff,
blocked spawns, close races, duplicate/stale data, FPS and model-priority timing.
This sender-side change is delivered through the `carrot-nav-pv5` comma source
branch. The existing five model-paired Jetson package pins and model identities
are retained: their receiver wire format is unchanged, and the recovery helper
is instantiated by the comma's existing display publisher. Updating the comma
source is required; replacing a Jetson model bundle alone does not update that
publisher. No device access, installation or reboot was performed for this change.
The PC results do not prove the reported device failed for this reason or establish real hardware
CPU/USB/control timing. A live-but-stalled producer or another link fault still
requires a separately authorized manual diagnostic session.

## Delivered behavior

Production Jetson Q92 uses one CPU JPEG/USB worker alongside the existing
renderer. The saved10/15/20/25/30fps selection is preserved, including onroad
startup/reconnect. This change does not overwrite the owner's saved choice;
20fps remains a target, never a guaranteed output rate. Comma snapshot sending,
model-first admission, the2ms display send budget, model deadlines and all model
inputs/outputs are unchanged. Jetson HUD service remains Nice19. No additional
inference, benchmark workers, FPS counters or performance UI are enabled.

- Two reusable RGBA buffers own their bytes. The raylib image is copied once
  before its borrowed view is freed. The worker's encoding buffer cannot be
  overwritten/resized. One pending frame is replaced by the latest; no backlog
  or catch-up burst. Full-frame Q92 encoding/readback remains; this is not zero-copy.
- Pending frames expire150ms after staging, checked before and after encoding.
  Already-started USB writes retain the existing driver/ACK timeouts. A driver
  stall can still delay the panel;150ms is not an end-to-end display deadline.
- Shutdown discards pending production frames, joins the worker and closes the
  display only after it stops. A stuck worker prevents another USB open in that
  process. Encoder errors still take the existing H.264 fallback after teardown.
- An unchanged atomically published snapshot skips repeated file reads/JSON
  parsing after its opened-file identity/size/timestamps match. Every cache hit
  still checks the original500ms snapshot lifetime; bad/missing/new packets
  invalidate the old cache. File checks retain the existing20ms throttle.
- Consumers share immutable decoded event bytes only. Mutable cereal builders
  remain private to each consumer. The cache is bounded to the latest snapshot
  and fixed services.
- The two heatmap plans cache colors/numeric strings by actual values, never
  age or validity. Expired/missing values select grey/`--` immediately on the
  next render. Partial108-channel updates,15 temperature sensors, colors and
  labels remain. Drawing/readback still runs at the selected frame cadence.

BSD pulsing/brightness, vehicle alerts, W/temperature, battery/charging view,
tripSOC, per-field5s/60s expiry, Park updates, USB input-power block, model
selection and qualified input fusion remain. No vehicle control, CAN request,
BLE collection, TensorRT input or model/runtime specification is changed.

## PC verification

863 tests passed;5 native/device-dependent checks skipped; one unavailable
loggerd source check deselected;3 subtests passed. Coverage includes real worker
threads, mutation of the caller's borrowed buffer, latest-only replacement,
resize/bounds, stale/slow encoding, encoder failure, USB-owner shutdown/rebind,
GC release, JSON corruption/replacement/expiry, private cereal builders,
heatmap labels/colors/partial channels, battery TTLs, BSD and all FPS choices,
model queues/deadlines/output/fusion regression. These are PC tests, not
physical driver, GPU, BLE or road validation.

## Offline model and measurements

Reproduce with `tools/jetlink/benchmark_hud_pipeline.py --output result.json`.
It runs only when invoked manually. The companion
`docs/artifacts/hud20_20261102.json` records the assumptions and complete results.

Linux PC microbenchmarks, mean per call (not Jetson):

| Operation | Before | After | Interpretation |
| --- | ---: | ---: | --- |
|108-channel label/color calculation|88.37µs|9.17µs|89.6% less work in this subroutine only|
|Unchanged64KiB snapshot|64.40µs|2.65µs|95.9% less work in this subroutine only|
|1920×462 ownership copy|81.54µs|80.75µs|No meaningful time saving claimed; reuse avoids allocations|

Both copies move the same bytes. Buffer reuse is not a claim of reduced DRAM
traffic. The two-buffer pool costs6.77MiB at1920×462, at most10.55MiB at the
accepted size limit, in addition to current render/JPEG resources on Jetson.

60s discrete-event simulation:20fps target,±10% stage jitter,0.5ms assumed
ownership copy, one pending frame. The stage times below are assumptions,
not measured Jetson stages. Parallel compute contention is swept0/10/20%.

|Assumed render / JPEG / USB|Sequential fps|Pipeline fps,0% contention|Change|
| --- | ---: | ---: | ---: |
|8/10/5ms|20.00|20.00|Already meets target|
|20/22/12ms|18.48|19.98|+8.1% throughput|
|15/18/40ms|13.68|17.20|+25.7%; USB still limits output|
|15/18/12ms,200ms stall every20 frames|16.73|17.03|+1.8%; does not cure stalls|

At20% assumed parallel compute slowdown, the second/third rows become
19.98/16.20fps. Higher throughput does **not** imply lower input-to-panel age:
the slow-USB case's average modelled frame age rises73.07→98.35ms (P99 after
124.75ms); buffers improve overlap at a memory/latency cost. Replaced frames
are dropped rather than replayed. A real USB stall remains visible.

No total Jetson CPU reduction, actual20fps, TensorRT P99 preservation or
vehicle safety certification is established by these estimates. Physical
Park checks must verify visual warnings/brightness/expiry, disconnect and
shutdown recovery, actual output rate, temperatures, and model deadline
behavior before relying on the new path in driving. The existing lower-FPS
selection remains available.

- flip-flop: separate committed package identity; catalog `md-v3.json`.

- ctv3m: separate committed package identity; catalog `ctv3m.json`.

- light-speed: separate committed package identity; catalog `light-speed.json`.

## Package delivery

863 PC tests passed; 5 native/device-dependent tests skipped, one unavailable loggerd source test deselected. Four isolated bundles pass imports, production-off workers/profilers, HUD/boot resources and model queue contracts. All16 public assets match local size/SHA256. No physical device was installed or tested.

- [n16](https://github.com/tjdgus200/openpilot/releases/tag/jetson-pv5-hud20-n16-20261102-78dfa2cd), source `78dfa2cd`.
- [flip-flop](https://github.com/tjdgus200/openpilot/releases/tag/jetson-pv5-hud20-flip-flop-20261102-e95fda90), source `e95fda90`.
- [ctv3m](https://github.com/tjdgus200/openpilot/releases/tag/jetson-pv5-hud20-ctv3m-20261102-94175459), source `94175459`.
- [light-speed](https://github.com/tjdgus200/openpilot/releases/tag/jetson-pv5-hud20-light-speed-20261102-0feeab42), source `0feeab42`.
