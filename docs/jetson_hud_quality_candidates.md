# Jetson HUD quality comparison candidates — 2026-10-27

Eight candidates are selectable through the new `JetsonHudQuality` Carrot Web
setting. Source implementation/PC checks do not imply device installation or a
measured performance improvement. All candidates use the existing host renderer
and USB protocol. Value 0 preserves the operating HUD's 10fps, FFmpeg/libx264 and
one encoding/conversion thread; the comma-side `ClusterHudEncoder` is unchanged.

| Profile | Encoder | Changed setting |
| --- | --- | --- |
| `baseline` | CPU libx264 | Existing automatic bitrate (2333k at 10fps) |
| `h264-4m` | CPU libx264 | 4 Mbps target |
| `h264-8m` | CPU libx264 | 8 Mbps target |
| `h264-12m` | CPU libx264 | 12 Mbps target; upper comparison point |
| `jpeg-q68` | CPU TurboJPEG | JPEG quality 68 |
| `jpeg-q85` | CPU TurboJPEG | JPEG quality 85 |
| `jpeg-q92` | CPU TurboJPEG | JPEG quality 92 |
| `jpeg-pillow-q85` | CPU Pillow | Quality 85, alternate CPU encoder control |

All use 10fps, the panel's existing resolution (current design 1920x480),
orientation, HUD overlays and freshness guards. H.264 comparisons keep GOP=1,
the existing preset and one FFmpeg encoding/conversion thread. JPEG keeps the
existing 4:2:0 subsampling, baseline/non-progressive encoding and synchronous
frame submission. TurboJPEG uses the pinned private ARM64 CPU library/wrapper
in `tools/jetlink/jpeg_runtime`, with complete licenses and a native integrity
check. No OS packages are installed. Failure returns to baseline H264, visibly
marks fallback and never credits H264 observations to the JPEG candidate.
No CUDA/nvJPEG path is selected or imported by these profiles.

## Carrot Web selection and comparison

Select Settings > External HUD > Screen & Camera > Jetson HUD Quality Candidate.
Only this small integer is added to the existing one-second comma settings
snapshot. Onroad changes to a new candidate require fresh (300ms), valid/alive
stationary Park data; returning to baseline is always allowed. Offroad/explicit
bench selection can apply without a car. The existing renderer preference watcher
closes/reopens only HUD resources at a main-loop boundary. Model/USB sessions,
vehicle Params and driving code are not restarted or modified.

The Tools Jetlink card shows eight rows with GPU mean/P99, sample count,
GPU >50ms count/rate and server P99. The existing HUD inference strip keeps its
10-second values and adds the effective profile name and `B:` lowest observed
GPU-P99 profile. Brightness/W/temperature, BSD and battery8 layouts are retained.

An optional read-only control request reads a maximum 64 actual existing
FrameStats records once per second, inside Jetson. Frame recording/inference
code is unchanged. The display deduplicates timestamps/session, discards the
first 15 seconds, and retains at most 6000 current-trial records (about five
minutes at 20Hz). It sorts actual samples, never averages overlapping window
P99s. A >3s sample gap restarts warm-up. Old/missing controls show waiting.

Only eight bounded summaries (16KiB maximum) are persisted in Jetson RAM;
they survive a HUD profile change but are cleared for a different model/runtime
session or reboot. No raw sample history crosses to comma. The summaries return
through the existing once-per-20-frames telemetry path; the Web card reuses its
existing two-second poll, without another comma worker, timer or network path.
No winner is shown until two candidates each have >=1000 samples spanning >=60s.
The star means lowest *observed GPU P99*, not a causal quality/safety verdict;
it never automatically changes the selected profile. The current trial restarts
when revisiting a candidate, and unsupported JPEG output is explicitly fallback.

## Preparing and selecting

From the repository, on the PC or a prepared Jetson runtime:

```sh
python3 tools/jetlink/hud_quality.py --list
python3 tools/jetlink/hud_quality.py --write-candidates /tmp/hud-quality-plans
python3 tools/jetlink/hud_quality.py --profile jpeg-q85 --seconds 300
```

These commands only print/write plans. Each JSON contains its exact argv,
settings and an explicit unvalidated marker. A PC-generated argv contains PC
paths: regenerate it inside the target runtime before execution. Plans are not
installers, systemd overrides or automatic profile-selection configuration.

For a later controlled bench session, an explicit run is available:

```sh
python3 tools/jetlink/hud_quality.py --profile jpeg-q85 --seconds 300 --run
```

Execution requires Linux/Jetson identity and an already inactive HUD service.
The tool never stops a running service. It sets only its own priority/affinity
to match the HUD service and replaces itself with the existing renderer. The
normal panel ownership lock remains active. It does not start another model,
restart inference, alter USB ownership, change Params or install a new runtime.
The renderer duration bounds rendering after startup, not the preceding runtime,
telemetry or USB-display wait. A later test must separately arrange the bench
session and restore its HUD service afterward; this preparation does neither.

JPEG sends now publish the same existing one-second host health record as H.264,
so a working JPEG candidate is not misclassified solely for missing HUD status.
The normal H.264 arguments and health packet fields are unchanged.

## Matched measurement plan

Compare baseline, 4M/8M H.264, JPEG Q85, then the remaining candidates if useful.
Use the same camera/map/cruise workload, brightness, power/clocks and thermals;
include a warm-up and repeated baseline before attributing an improvement.

- Measure delivered HUD FPS, sharpness of the same text/lines, JPEG bytes/frame,
  actual H.264 bitrate, USB send latency/timeouts and reconnects. Encoder quality
  numbers and target bitrate are controls, not promised optical quality/bandwidth.
- Record renderer and FFmpeg-child CPU, RSS, system CPU/memory and GPU load. The
  existing `PROFILE proc_cpu` is the renderer process only; it excludes the
  FFmpeg child and must not be used alone to compare H.264 with in-process JPEG.
- Separately measure complete comma request/response P50/P95/P99, >50ms count
  and 20Hz continuity. The new GPU/server observations do not measure the whole
  USB round trip or establish driving safety.
- CPU JPEG still shares RAM bandwidth/power with inference. Raised H.264 bitrate
  and JPEG may increase USB traffic; no candidate is assumed safe or better until
  the actual device/panel comparison passes. Web selection requires both the new
  comma source and its corresponding Jetson package. An older host keeps its
  original behavior and cannot report these comparisons. Default 0 remains the
  original H264 path until the operator explicitly selects a candidate.

The abandoned, unvalidated CUDA JPEG prototype and setting-forwarding patch are
archived only in the PC's `jetson-nvme/hud-web-encoder-20261027` directory. They
are outside the runtime/export and are not used by any candidate above.

## Validation status

Linux PC regression: 448 passed, 6 skipped. The skips are five native FFmpeg
checks (covered separately on Windows) and an absent optional latest-upstream
reference checkout. The run includes profile fallback/clean resource release,
Park gating, sample deduplication/session/gap/memory bounds, existing BSD and
battery overlays, package/USB update validation, and 25 Wiki generator checks.
Web selection/comparison checks: 28 passed; generated Web build and Python lint
passed. The new Korean/English guides and three generated setting Wiki review
pages validate; these PV5-specific Wiki pages are staged locally rather than
overwriting the upstream carrot-wip Wiki catalog.

Windows native FFmpeg checks encode/decode all four H264 candidates; real
Pillow output is also decoded. ARM64 ELF identity/hash and native wrapper
arguments are checked on PC, but loading that ARM library on Jetson, optical
quality, delivered 10fps, total CPU/memory/USB load and loaded model P99 are
not yet measured. No measured winner is supplied with the software.

Published source: `9210de4bd430c323336b9b1a26851f393713a749`. The selected
[PC-tested Jetson prerelease](https://github.com/tjdgus200/openpilot/releases/tag/jetson-pv5-hud-quality-20261027-9210de4b)
contains a 65,020,650-byte host bundle, SHA256
`94fb65c532619734bce80ab1e7c3b9c3854d22f2e766baa65ac92121c993642e`.
All four public assets were anonymously downloaded and matched local bytes.
Independent exported server imports, HUD help and retained overlays pass.
GitHub rejected workflow dispatch because repository Actions is disabled;
remote CI did not run. Publication is distinct from device installation.
