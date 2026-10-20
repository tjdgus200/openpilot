# N16 deployment and actual-camera bench results, 2026-10-20

N16 is installed on the owner's Orin and selected by the comma. A five-minute
stationary simulated-cruise interval with real camera input and nonzero map data
produced **20.003786Hz**, with no source-camera frame gaps, invalid observed
model/odometry messages, bad pose-input checks or invalid planner messages.
This is a bench throughput result, not a guarantee that every inference finishes
within 50ms or that the model is safe for driving.

## Public deployment

- Fork branch: [`codex/jetson-nav-runtime-port`](https://github.com/tjdgus200/openpilot/tree/codex/jetson-nav-runtime-port).
- Tested comma commit: `cf036fa5783793cfa39d9efd2869039a35337d42`.
- Installed Jetson source: `a1d723a6b4db051070c0eab5ffceb59c982998e4`.
- [Host package](https://github.com/tjdgus200/openpilot/releases/tag/jetson-n16-20261020-a1d723a6).
- [Installation and official-branch return instructions](jetson_n16_install_20261020.md).
- [Original contract/CPU/isolated-Orin investigation](jetson_n16_compatibility_20261020.md).

The model is the pinned official `96244b1f/12800` ONNX, SHA-256
`01effc679eb46d6036ce0302673a87fe2dcfcc95a679daeeb929679e53c3647a`.
Only model identity/display label and matching experimental USB deployment
metadata changed. Official server selection, official signing trust, input/output
layout, validation/fallback policy, inference options, OS and firmware remain.
Both owner-enrolled SHA-256 delivery and matching legacy signed metadata exist.
The latter does not enroll this fork's signing key on someone else's Jetson.

The host package contains committed public runtime code/assets; weights remain at
the pinned official Hugging Face URL. No device credentials, private signing key,
camera recordings, CAN captures or raw device results are published.

## Test method

The owner confirmed that comma 3 and Orin were disconnected from the vehicle.
The test required fresh Panda ignition-off, `noOutput`, disconnected harness and
zero CAN transmit counters. Global manager state remained offroad throughout.
It started real cameras, modeld, driver monitoring, sensors, locationd and planner,
using stored real CarParams/calibration. Stationary car/cruise/control-state
messages were simulated; card, controlsd and selfdrived were not started.

The map source produced changing nonzero 256x256 images at approximately 10Hz
through the real `mapsd` VisionIPC interface. The unmodified MapInput receiver
performed its normal shape, timestamp and route-ID checks. A test-process-only
navigation context supplied a matching route ID and right-turn instruction.
Normal input packing, actual USB/IPC, Jetson TensorRT and output parsing followed.
This exercises map transport/consumption, not live GPS, tile download/rendering
or real navigation behavior. Synthetic image compression is not a worst-case
transport-bandwidth qualification.

After startup and 15 seconds of warm-up, the worker measured approximately
30 seconds without simulated cruise, then 300 seconds with cruise and maps.
It restored touched settings, calibration and CPU power state and terminated all
its children. The normal services and selected N16 model were left installed.
The comma's eight-core onroad power state was enabled, but the global manager
stayed offroad: USB-worker placement and whole-vehicle control-process co-load
were not identical to an actual onroad manager session.

## Results

The observer collected 6,603 outputs across both measured phases. The comparison
below uses the producer-side per-frame trace to avoid interpreting observer
conflation or window-boundary differences as source-frame loss.

| Check | 30s baseline | 300s simulated cruise/map |
| --- | ---: | ---: |
| Producer publications within matched window | 601 | 6,001 |
| Producer output rate | 20.025415Hz | 20.003786Hz |
| Camera ID gaps / duplicates / reported drops | 0 / 0 / 0 | 0 / 0 / 0 |
| Internal fallback / other model hash | 0 / 0 | 0 / 0 |
| Observed invalid model / odometry / planner | 0 / 0 / 0 | 0 / 0 / 0 |
| Pose input / posenet check failures | 0 / 0 | 0 / 0 |
| Driver-monitoring rate from IDs | 20.023950Hz | 20.003718Hz |

Navigation inputs were present in all 6,001 matched cruise profile rows; nonzero
map pixels were present in 5,981. Initial map connection/freshness gating accounts
for the difference; unavailable data was not represented as a fresh map.

| Cruise interval | Mean | P99 | Maximum |
| --- | ---: | ---: | ---: |
| Camera preprocessing | 4.230ms | 4.479ms | 12.885ms |
| Jetson engine run through reply event | 24.795ms | 26.716ms | 27.487ms |
| Jetson server total | 26.170ms | 28.139ms | 28.872ms |
| Client roundtrip minus server total | 11.743ms | 21.570ms | 31.346ms |
| Complete JoiningModel call | 43.096ms | 53.488ms | 67.007ms |
| Approximate camera EOF to publication | 53.440ms | 74.890ms | 88.346ms |
| Publication interval | 49.991ms | 60.042ms | 71.898ms |

The residual is transport, local IPC, serialization and scheduling combined;
it is not physical USB time alone. Engine run includes transfers/checks through
the reply event, not just TensorRT GPU kernels. Capture age is derived from
installed camera-age, inference and postprocessing diagnostics, not optical
latency. Different rows' maxima need not occur on the same frame.

**138 of 6,001 complete inference calls (2.30%) exceeded 50ms**. There were zero
observed source-camera gaps, but that does not turn nominal 20Hz throughput into
a per-frame 50ms deadline guarantee. No matched before/after trial establishes
that changing model weights caused a particular performance improvement.

Once-per-second, approximately aligned resource samples during cruise:

| Resource | Comma | Orin |
| --- | ---: | ---: |
| Aggregate CPU mean / sampled maximum | 32.89% / 40.81% | 10.07% / 15.51% |
| Minimum available memory | 879,087,616 bytes | 4,605,722,624 bytes |
| Module VDD_IN power mean / sampled maximum | not measured | 13.735W / 14.040W |

CPU percentages average across all cores, not one thread. Sampling does not
capture short scheduling peaks; host/comma alignment uses received health ages.
GPU load was not available from the sampled sysfs path and is not inferred from
inference duration. Maximum reported temperatures across the run were comma
59.10°C / Orin 59.312°C. HUD-connected telemetry was true at all 186 status polls;
this does not establish uninterrupted Wi-Fi delivery or optical display FPS.

## Installation and validation notes

The existing installer verifies file sizes/SHA, staged source/model identity and
candidate execution before activation. Its candidate probe passed GPU-state
equivalence and finite execution. TensorRT skipped some build tactics after
allocation failures, then completed a working plan; no inference error was
observed in the successful measured run. This is not a memory-stress qualification.

Two preparation attempts were excluded. Direct activation initially waited for
the old boot approval; running the standard USB boot-selection gate completed
normal approval without bypassing checks. Next, a hash guard detected the previous
model inherited from the comma manager's cached modules and stopped the test.
A normal comma reboot loaded N16; the successful run verifies its hash throughout.
For installation, restart the comma itself rather than only killing jetlinkd.

The normal official public key is unchanged. Both normal Jetson services are
active, the cached engine spec and last-loaded identity match N16, and the comma
returned to offroad with parameter/calibration equality and no test children.

Remaining limits: synthetic map/navigation and stationary cruise context, no live
tile-rendering/network qualification, no real moving vehicle/controller co-load,
no long thermal/fault recovery or road validation. CPU-versus-TensorRT numerical
acceptance remains separate from the graph/state-mode equivalence checks in the
earlier report. No driving-safety guarantee is made by this deployment.
