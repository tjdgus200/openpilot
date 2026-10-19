# Jetson CPU auxiliary vision over Wi-Fi

On this branch `ShareData` retains its default-off and onroad readiness gates,
but neither ONNX runs on comma. Existing grayscale lane preprocessing and
masked/letterboxed RGB V-ASM preprocessing stay on comma. The exact uint8 input
pixels are transmitted without JPEG loss to a separate Orin CPU process.
The two existing model files, class mappings, lane thresholds, ROI definitions,
V-ASM smoothing and OEM BSD OR merge are retained.

The V-ASM model classifies configured left/right regions into near-threat/no
nearby/distant-or-rear classes; it does not output forward vehicle boxes,
distances or positions. No new generic forward-car detector or arbitrary
front-car distance threshold is introduced. Existing adjacent-lane radar checks
remain in DesireHelper alongside the visual side-threat check.

## Execution and transport

- One separate `vision_cpu.py` worker on each end, independent of the driving
  model and HUD renderer. Jetson uses OpenCV DNN CPU explicitly, one OpenCV/BLAS
  thread, ordinary scheduling, nice10 and at most the first two already allowed
  CPUs. comma relay uses nice19. No realtime priority or GPU provider is used.
- One request in flight; socket backlog1, bounded request380,000 and response4096
  bytes, 0.75s local completion deadline, exact pixel shapes and model SHA-256.
  The host inserts idle time equal to a quarter of the previous compute wall
  time: an approximately80% sustained single-thread wall-duty budget, not a
  hardware/cgroup CPU-utilization guarantee or a guarantee against memory/power
  contention. No batch or catch-up inference queue is created.
- TCP7717 is bound only to an observed private WLAN address. Session key is
  negotiated by the existing USB control exchange, inherited privately, then
  purpose-derived for auxiliary vision. Images/results use only Wi-Fi, mutual
  authentication and AES-GCM, with request IDs and ordered sequence numbers.
  The main driving-model data path remains USB. No key is placed in argv,
  diagnostics, telemetry or this repository.
- Missing/mismatched host support does not start a local comma ONNX fallback or
  place auxiliary images/results on USB. Missing results become unknown.
- Initial experimental intervals are500ms lane and500ms BSD total, with no
  accelerated follow-up. Older shorter stored intervals are capped, not rewritten.
  Uint8 payloads alone at those rates would be about8.72Mbit/s before framing/retries/HUD traffic. This is a calculated
  upper request-rate budget, not a measured WLAN rate; actual CPU timing can
  reduce request rate. Very short interval settings cannot make CPU compute fast.

## Native classification and automatic initiation

`CarState.laneTypeSources` records native validity separately from vision. On
Hyundai CAN-FD it requires an actually received cached camera message timestamp
within300ms, nonzero reported lane probability and a recognized type. This300ms
is the branch's conservative receive-age policy, not a claimed OEM specification.
Discovery must complete before automatic permission. A capable vehicle's stale
or undecided native result never becomes replacement dashed permission.

Fresh native solid/edge/barrier types cannot be cleared by ONNX dashed. Native
dashed plus ONNX solid becomes a veto while retaining the OEM color. Only an
unavailable native classifier uses ONNX as its replacement. Legacy scalar zero
alone cannot establish that an OEM classifier is available.

With the feature enabled, automatic initiation additionally requires a fresh
ONNX dashed observation (<=1s from capture) and a freshly evaluated, clear target
side (<=1.5s). Age includes camera, preprocessing, network and CPU time; remote
completion never resets capture age or compares unsynchronized host clocks.
Future captures, unknown sides, stale results and model mismatches hold automatic
initiation. Existing geometry, radar, OEM BSD and driver-torque policies still
apply. A communication loss does not abort an already crossing maneuver.
This is an added veto, not proof that a lane change is safe.

Both comma and Jetson must be updated together. The host bundle includes the
unchanged auxiliary model files and CPU sources, and advertises the capability
only if OpenCV and both files exist. The tested Orin runtime already contains
OpenCV4.8.0. Other runtimes missing it remain unavailable; no dependency is
silently downloaded in the driving process. The official signed update channel,
its signing root, model selection and rollback behavior are not replaced.

## Validation and limits (2026-10-19)

PC checks cover the actual lane-change state machine, native/vision disagreement,
unknown/stale/future/unevaluated inputs, OEM BSD preservation, request limits,
identity and encrypted sequencing. Native IPC/Params are import-only desktop
adapters; real cereal schemas and production policy/ONNX code are exercised.
The final source passed146 focused PC checks, including both actual ONNX model
outputs against the lossless RPC input path. No test in that count operated a
vehicle or established real-camera recognition accuracy.

Separate temporary Orin CPU probes, without changing production services, used
12 synthetic constant inputs per model, excluding the first two from warm stats:

| Model | Warm mean | Warm max | First execution |
|---|---:|---:|---:|
| Lane416 |200.006ms|200.474ms|277.349ms|
| V-ASM352 |72.843ms|73.328ms|86.157ms|

The combined probe's peak RSS was178,132KiB (~174MiB). It ran nice19, at most two
allowed CPUs, one OpenCV thread, OpenCV4.8.0 CPU backend. These are synthetic
forward-pass timings, not recognition accuracy, WLAN round-trip timing, sustained
coexistence with20Hz driving inference, closed-loop replay or vehicle validation.
No such validation is claimed. Keep the feature off for normal use until the
paired runtime and real-camera/vehicle behavior have been checked.


### Interrupted coexistence trial

A desk trial used real camerad/modeld/driver-monitoring/location/planner work,
stationary simulated cruise and a synthetic nonzero map through the normal
model input path. It did not enable CAN controls or represent a driven vehicle.
The comma rebooted during the first auxiliary phase. Retained kernel records
show CPU7 realtime throttling followed by a BUG in `dump_throttled_rt_tasks` and
kernel panic. The installed kernel confirms `CONFIG_PANIC_ON_RT_THROTTLING=y`;
RT runtime/period were950000/1000000us. The exact Python process role and reason
for exhausting the budget remain unresolved. This is a failed validation gate.

Recovered Jetson data covers only36 seconds/36 lane calls, with **no BSD calls**.
Lane warm wall mean was211.199ms. Total Jetson CPU averaged11.167% before this
partial phase and15.228% during it; available memory averaged4725.5 and4603.5MiB.
These partial observations cannot choose a safe combined lane/BSD cadence or
establish20Hz coexistence. Keep `ShareData=0`; no ONNX production rate or
vehicle use is validated by this result. The subsequently packaged500ms limits
are provisional load caps only. Diagnostic wrappers and
camera-consumer activity are among the unexcluded factors, not proven causes.

## Paired installation package

The owner requested code and install assets pushed while disconnecting the
devices. The branch pins an owner-signed USB host selection. Updating comma to
the matching installation commit prepares the paired package; the installed
boot receiver validates its signature, assets, runtime ABI and isolated driving
model probe before activation. The existing official update route and owner
trust remain separate. No automatic ShareData enable is added. With an old or
missing host, results stay unavailable and automatic initiation is vetoed when
the feature is requested. Do not interpret GitHub delivery as installation on
a disconnected device, or PC checks as onroad/cruise/map coexistence validation.

### Delivery record

Source `93788f61fcf3c983c0540f1a5a52476c6c02aa74` is packaged as
[`jetson-aux-cpu-wifi-20261019-93788f61`](https://github.com/tjdgus200/openpilot/releases/tag/jetson-aux-cpu-wifi-20261019-93788f61).
The bundle is81,643,822bytes with SHA-256
`86b90d67c1b62713cf78134e23c2267e0bd95ba49217621a6ecd4c439c75a1ea`.
Both existing auxiliary model hashes match the source files, and59 pinned main
runtime/model-pin/renderer files are unchanged from the prior installed package.
The exported HUD entrypoint check passed outside the checkout. A further49 PC
delivery checks passed, covering signed USB transfer/rejection, retained main
stateful-session behavior, legacy HUD negotiation, isolated optional-worker
startup failure and an authenticated low-rate session across a >1s request gap. The owner-signed
selector uses the previously enrolled public key; no new key registration is
required on the previously prepared device. PC validation and public delivery
are complete; the owner disconnected before installation of this package.

On the next desk connection, update the matching comma installation branch
`codex/jetson-nav-runtime-port`, then power-cycle Jetson normally without a
recovery jumper. Its existing boot receiver verifies and installs the selected
package through comma USB. Keep `ShareData=0` while confirming runtime readiness;
paired real-camera and loaded coexistence validation remain outstanding. No
vehicle controls or kernel scheduling policies were changed for this delivery.

### Direct-AP availability caveat

The owner subsequently reported the boot display alternating retrying/waiting
with only a USB192.168.60.x address. That indicates no displayed direct-WLAN
address, not a proven Wi-Fi driver or model failure. Current code creates or
recreates the comma AP only offroad and only realigns a live AP offroad. Cold
boot directly into onroad, or a shared-radio station channel change onroad, may
therefore leave the direct link unavailable. Direct-only policy deliberately
removes phone/router fallback; it does not itself guarantee AP availability.
Which condition occurred in the vehicle is unconfirmed. This delivery retains
the existing guard and does not attempt live network reconfiguration or claim
that the reported connection failure is fixed. No device diagnostics were
started after the owner disconnected.
