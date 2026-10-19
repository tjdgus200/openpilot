# Jetson HUD activation and temperature, 2026-10-06

The user requested that a HUD attached to Jetson run independently of the
vehicle's External HUD Display switch, show Jetson temperature and warn when hot.

## Behavior

- Only the Jetson renderer's read-only Params adapter forces `ClusterHud=1`.
  No saved vehicle setting changes. Its existing onroad/debug output gate stays:
  ignition off blanks the panel unless an always-on debug mode was selected.
- Physical USB detection and unplug/retry behavior remain. The vehicle preview
  worker may start with `ClusterHud=0` only when a fresh Jetson HUD output
  heartbeat confirms a connected panel. Stale telemetry (three seconds), no
  panel or ignition off ends that worker, subject to the original direct-HUD gate.
  Vehicle state/camera transport remains on the vehicle; rendering and encoding
  for the Jetson-attached panel run on Jetson at the existing 10 FPS.
- The Jetson badge gains a second line showing the maximum valid internal sensor
  temperature. It follows left/right layout; modes without the driving status
  row use the upper-right corner. Expired/missing/invalid data shows `--°C`.
- Existing per-zone thermal policy is retained: warn at limit minus 5°C, error
  at the limit, excluding passive trips bound only to `hot-surface-alert`.
  Each temperature is compared with its own zone's configured limit, not another
  sensor's limit. The banner reports the nearest limit's temperature and limit.
- Thermal identity stays separate from storage/runtime faults. Korean/English
  warning strips request cooling checks. Vehicle alerts suppress the health
  strip and render last; no new audio, control event, reboot or shutdown is added.
  NVIDIA's existing thermal protection is unchanged.

NVIDIA's standard Orin Nano/NX table lists software throttling at 99°C, hardware
throttling at 103°C and software shutdown at 104.5°C. The HUD reads installed
sysfs limits rather than assuming every device uses those defaults. With a
99°C trip, the displayed warning starts at 94°C. A 70°C surface notification is
not treated as processor overheating.

Source: [NVIDIA Jetson Linux thermal specifications](https://docs.nvidia.com/jetson/archives/r36.4.3/DeveloperGuide/SD/PlatformPowerAndPerformance/JetsonOrinNanoSeriesJetsonOrinNxSeriesAndJetsonAgxOrinSeries.html#thermal-specifications).

## Validation and delivery

Focused desktop coverage verifies independent activation, forwarded ignition
and brightness, heartbeat creation/expiry/removal, per-zone 94/99°C boundaries,
custom limits, surface-only trips, invalid/stale temperature, storage-fault
identity, Korean/English banners and vehicle-alert precedence. Production
raylib renders cover normal/warning/error, vehicle alert, navigation, graph and
swapped panel layout. These synthetic checks do not establish physical USB,
real thermal sensor, loaded inference timing, or on-vehicle update validation.

The catalog and Korean/English guides explain direct-device versus Jetson
attachment. The Wiki setting's manual explanation is synchronized separately.
Host changes require a signed Jetson source bundle; a vehicle-only commit does
not update an already-installed host. Model, TensorRT/JetPack ABI and base image
stay pinned. Release hashes and local render/test evidence are retained in
`.analysis/archive/2026-10-06/jetson-hud/` (private, not committed).

Source release `a564ce1dc082909b1fdc73b72e64d920336b34d6` was exported and
signed with the existing updater trust root. Bundle size is 64,503,897 bytes;
SHA-256 is `84aae85882796c72d62c824585e62e953e953c7ac1d145c315c7660e5915c701`.
The immutable NAS bundle and manifest passed full HTTPS readback. The vehicle
pin selects this release through the existing offroad staging/boot activation
path; the NAS stable channel and SD installation image are unchanged.
Focused checks: 65 passed, 5 platform skips; Wiki tests: 25 passed; user-guide
and generated Wiki validation passed. Physical host activation is unvalidated.


## Owner power badge extension (2026-10-19)

The owner requested replacement of the `jetSON` label, not another row.
The existing badge now shows `power_w` as one-decimal watts above the existing
maximum internal temperature. Geometry, font size, warning colors/priority and
HUD frame rate are retained. Power has no invented warning threshold.

NVIDIA documents INA3221 `in*_input` in millivolts, `curr*_input` in milliamps,
and the Orin Nano/NX `VDD_IN` rail as **total module power**:
[NVIDIA Jetson Linux36.4.3 power-monitor guide](https://docs.nvidia.com/jetson/archives/r36.4.3/DeveloperGuide/SD/PlatformPowerAndPerformance/JetsonOrinNanoSeriesJetsonOrinNxSeriesAndJetsonAgxOrinSeries.html#software-based-power-consumption-modeling).
Use voltage×current/1,000,000 for watts. Do not sum CPU/GPU/SOC rails into VDD_IN
or equate this with carrier/USB-display/adapter total power. Sampling these two
nodes is not simultaneous laboratory power measurement and can miss brief peaks.

`host_health.py` reads only an unambiguous INA3221 rail named VDD_IN in the
existing worker every0.5seconds. Thermal/storage/network refresh stays at1second. Scheduling subtracts I/O duration and retains a50ms minimum sleep after an overrun, avoiding catch-up bursts. No sysfs write, extra thread/process, GPU work,
subprocess or inference-frame sensor read is added. Missing/ambiguous sensors,
read failures and invalid values yield null/`-- W`, never a guessed mode rating.
The existing three-second health freshness bound clears stale watt readings.

The actual connected Orin exposed hwmon1/ina3221, channel1 VDD_IN; an initial
read was5048mV and1520mA, or7.67296W. This is a single stationary sample, not a
sustained inference-power benchmark. Source/renderer regression and signed
activation are recorded after the updated package is applied. This HUD change
is excluded from both public Wi-Fi and inference-review commits.


The initial one-second power version3cd796c8 was signed and applied after its
normal execution probe first encountered CUDA out-of-memory. Only this task's
finished transfer-asset caches were advised DONTNEED; no files, global memory
policy, model checks or power/clock settings were changed. The same probe then
passed and services resumed. This temporal association does not establish a
universal memory-failure cause or a new inference optimization. The owner
confirmed watts and temperature were both visible. The subsequent requested
cadence is0.5seconds for power, retaining1second for the slower sensors/network.


## Owner placement change (2026-10-19)

In driving layouts with a Wi-Fi status icon, place the existing 104×60 power
and temperature badge ten design pixels to its right. Center both on the same
status row and shift the center clock right as needed to prevent overlap.
The panel's existing left/right transform still applies. Full-navigation/graph
layouts without that row keep the upper-right fallback. Sampling remains0.5s
for watts and1s for slower health fields; this layout change adds no workers,
network traffic or inference work.

The installed0.5s version404b9861 produced17 device samples with16 intervals:
mean0.500500s, minimum0.500355s, maximum0.500531s. This verifies that bounded
sampling interval, not loaded driving performance. Owner confirmed watts and
temperature visible before this placement change. Focused PC checks for this
placement passed142 cases, including three clock widths and the explicit
full-navigation clock position. Physical placement confirmation remains pending.
