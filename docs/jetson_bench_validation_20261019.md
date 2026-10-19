# Jetson battery HUD and stationary timing checks, 2026-10-19

## Installed versions and scope

The owner connected a vehicle-disconnected comma 3, Jetson, USB HUD and phone hotspot.
Comma ran `e1468d4e`; the signed USB update activated Jetson source `85b13334`.
The owner confirmed that screen 8 now shows the battery page. The host logged about
10 Hz display output. Previously its pinned `d07e9f35` renderer normalized mode 8
to automatic mode 0. The ONNX hash, TensorRT plan, signing key, firmware requirements
and official server/restore selection were preserved.

Tests used real cameras, saved calibration, the installed model and planner, a
stationary simulated cruise state and repeatable nonzero navigation inputs. They
did not start vehicle control processes, set global IsOnroad, transmit vehicle CAN
or establish real navigation/road control behavior. Fresh ignition-off/noOutput
Panda state, disconnected harness, zero CAN TX counters and temperature limits were
required. Bounded workers restored parameters, calibration and CPU power state.

## Actual camera and model path

After warm-up, a 60-second lateral-input interval and 120-second simulated-cruise
interval produced 3,601 observed outputs. The latter exercised 2,400 nonzero map
updates. There were no observed camera ID gaps or invalid model/odometry/pose-input
or planner messages during those measured intervals; driver monitoring ran about
20 Hz. This does not reproduce actual vehicle motion, road imagery or full control
process co-load.

| Simulated cruise measurement | Mean ms | p99 ms | Maximum ms |
| --- | ---: | ---: | ---: |
| Camera preprocessing | 4.343 | 4.624 | 12.998 |
| Jetson server total | 26.202 | 28.243 | 29.120 |
| Client round trip minus server total | 11.614 | 21.848 | 30.722 |
| Complete JoiningModel call | 43.159 | 54.362 | 62.628 |
| Approximate camera EOF to publication | 53.297 | 74.608 | 83.771 |

The residual includes local IPC, serialization, transport and scheduling; it is
not a measurement of physical USB alone. The age estimate adds the installed
camera-age, inference and postprocessing diagnostics. It is not optical latency.
The JoiningModel distribution contains 76 calls over 50 ms among 2,399 matched
profile rows. Output rate was approximately 20 Hz, not a guarantee that every
frame completes within 50 ms or that the system is safe for road use.

## Jetson stage and input experiments

An isolated probe stopped only the installed inference service, used its exact
cached TensorRT plan and CUDA pinned bindings, then restored the service. The host
reported MAXN_SUPER, six online CPU cores, 1.728 GHz CPU, 1.020 GHz GPU and 3.199 GHz
EMC at inspection. No power/clock setting was changed by this probe.

The default-off candidate in `codex/jetson-input-optimization-review` was loaded
only from a temporary directory. Its native uint8-to-FP16 conversion and direct
compact-map restoration reduced actual pinned input preparation from mean
**0.938 to 0.448 ms**, p99 **1.030 to 0.478 ms**, over five alternating 400-call
batches per variant. All tested input binding bytes matched. A separate 64-frame
A/B/A inference sequence produced bit-identical driving outputs and final recurrent
state. Mean staging-plus-inference was 25.718/24.952/25.512 ms; clock/load variation
also affects those short whole-inference comparisons.

Separate CUDA timing events in a diagnostic graph measured:

| GPU stream stage | Mean ms |
| --- | ---: |
| Non-state input upload | 0.076 |
| TensorRT execution | 23.865 |
| Driving-output download | 0.012 |
| Full-state finite scan/status copy | 0.383 |
| GPU state feedback | 0.347 |

Additional event nodes and synchronization perturb this isolated 100-frame
measurement. These are not production end-to-end timings. GPU-resident state,
preallocated pinned buffers and CUDA graph reuse were already active. The scan
checks all recurrent state and must not be removed to save time. The approximately
0.49 ms input saving does not resolve the transport/scheduling tail. The candidate
remains disabled and was not installed as the production service.

## HUD interference and preview resource fix

A second stationary all-cruise test ran HUD-on/off/on for 60 seconds each, preserving
the comma sender and inference path. Excluding ten seconds at each phase boundary:

| Phase | JoiningModel mean / p99 ms | Residual mean / p99 ms |
| --- | ---: | ---: |
| HUD on, first | 42.847 / 54.176 | 11.495 / 21.581 |
| Jetson renderer stopped | 42.164 / 53.262 | 11.390 / 22.463 |
| HUD on, last | 43.450 / 56.002 | 11.959 / 24.151 |

Stopping the renderer reduced its resource consumption, but did not eliminate the
residual tail. This short run does not establish a repeatable tail improvement or
justify reducing display rate below 10 Hz. The renderer/encoder service group used
roughly half of one CPU core during the observed on periods, not half of all six
cores. The comma snapshot publisher and Wi-Fi worker used roughly 10% and 7.5% of
one core respectively; this excludes the separate camera JPEG child. Actual onroad
affinity/nice placement differs because the global manager stayed offroad.

The observer missed conflated messages while collecting process statistics,
appearing to show 19.65-19.80 Hz. The modeld-side per-frame trace instead contains
798/800/799 publications over each stable roughly 40-second window, approximately
20 Hz, with zero source-camera ID gaps or reported drops. Observer gaps must not
be presented as model or camera losses.

Review found an avoidable display workload: the comma encoded JPEG previews in
3D views even though the renderer did not consume them. The follow-up gates the
preview child to camera view modes 2/3/4, closes it when returning to 3D, and retains
the existing 10 Hz preview cadence when requested. Model camera buffers, transforms,
inference contracts and validity checks are unchanged. PC HUD/battery/hotplug/Wi-Fi
regression: **120 passed**, with three existing pytest configuration warnings.

After installing `c4b12c57` on comma and restarting only the offroad jetlinkd, a
separate bounded real-camera check selected 3D/road/3D/wide/3D views. The 3D phases
sent zero JPEG frames; the requested camera phases observed approximately
10.06/9.96 Hz and 101/100 distinct frames per stream. Original view/debug settings,
CPU power state and camera shutdown were verified afterward. This validates source
preview delivery, not optical display FPS. Test directories were removed from both
devices after collecting the private evidence.

## Remaining limits

There is no validated PV5 battery receive-signal profile in this change. Bench
battery values correctly remain unavailable. Real-cell voltage distribution cannot
be inferred from pack minimum/maximum/spread. A future heatmap needs validated
per-cell samples, bounded packet size, expiry, cached 2 Hz color updates and separate
resource measurements. No cell count, mapping or battery-health classification was
invented. All recordings, parameter backups, raw results and credentials remain
outside Git.
