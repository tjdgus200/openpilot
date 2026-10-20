# N16 input staging and auxiliary-load measurements

Scope: `codex/jetson-nav-runtime-port`, C3X and Orin, N16 export
`01effc679eb46d6036ce0302673a87fe2dcfcc95a679daeeb929679e53c3647a`.
This is a bench investigation, not a vehicle validation or a guarantee of a
50 ms deadline. The manager remained offroad, Panda ignition was off, the
harness was disconnected, safety was `noOutput`, and transmit counters were
zero. No vehicle control process was started.

## Camera pipeline and auxiliary load

The complete run used real cameras, QCOM preprocessing, USB/IPC, TensorRT,
driver monitoring and location/planner processes. Cruise state and a nonzero
map were synthetic bench inputs; no road scene or closed-loop cruise behavior
was validated. Four consecutive 90 s phases used the same comma boot/model.
The first 15 s and final 5 s of each phase were excluded from the table.

| Phase | Frames | Full pipeline mean / P99, ms | Above 50 ms | Host mean / P99, ms |
| --- | ---: | ---: | ---: | ---: |
| Auxiliary off, A1 | 1399 | 42.03 / 53.37 | 31 | 25.48 / 25.69 |
| Wi-Fi HUD | 1399 | 43.32 / 55.82 | 45 | 26.09 / 28.09 |
| Wi-Fi HUD + lane classifier | 1398 | 43.85 / 56.66 | 51 | 26.29 / 28.39 |
| Auxiliary off, A2 | 1400 | 41.98 / 53.02 | 23 | 25.46 / 25.71 |

HUD adds about 1.3 ms to the mean in this sequential run; the lane classifier
adds about another 0.5 ms. These are observations from one A/B/return sequence,
not independent randomized estimates. The lane classifier completed 132/132
trimmed requests at approximately 1.89 Hz; host CPU time averaged 204.44 ms
per request and its P99 was 209.34 ms. Wi-Fi RPC averaged 266.98 ms, P99
309.69 ms. **BSD did not execute in this run** because the bench request gate
was wrong. Do not describe this as a lane-plus-BSD comparison.

Published model rate was approximately 20 Hz, but the lane phase had one
camera frame-ID gap, one reported drop, one invalid odometry message and one
bad pose-input indication. Baseline P99 already exceeded 50 ms. There was no
wrong-model result or fallback in the complete run. A mean below 50 ms and a
rounded 20 Hz average do not establish a per-frame deadline.

Full-pipeline residuals include IPC, scheduling, response delivery and other
unattributed time; they are **not pure USB bus time**. Queue handoff and USB
send window maxima were intermittent. Window maxima are not frame P99 values,
and separate component P99 values must not be added.

Attempts aborted during camera warmup/reboot, and the subsequent fixed-input
run whose lane/BSD requests all failed, are excluded from performance claims.
The cause of those power resets was not established by the charger change.
Auxiliary USB current blocking remained disabled and no persistent independent
power enrollment was installed.

## Isolated Orin CUDA and staging measurement

The same cached TensorRT 10.3 Orin-sm87 plan was loaded from a private source
copy. CUDA timing events were inserted into the graph to separate H2D,
TensorRT execution, output D2H and the retained state finite check. This
instrumented graph is not identical to the uninstrumented production graph.
There were four 300-frame phases at 20 Hz (A1/B1/A2/B2), all 1200 completed.
The first 20 frames of each phase were excluded from timing summaries.

| Interval | Existing mean, ms (A1 / A2) | Candidate mean, ms (B1 / B2) |
| --- | ---: | ---: |
| CPU input preparation | 1.078 / 1.066 | 0.554 / 0.554 |
| Engine run wall time | 24.911 / 24.779 | 24.654 / 24.883 |
| CPU prepare + run + feedback | 26.089 / 25.940 | 25.271 / 25.504 |
| CUDA H2D | 0.073 / 0.073 | 0.074 / 0.074 |
| CUDA TensorRT execution | 23.996 / 23.885 | 23.792 / 24.026 |
| CUDA output D2H | 0.012 / 0.012 | 0.012 / 0.011 |
| CUDA state finite check | 0.381 / 0.376 | 0.382 / 0.374 |

CPU preparation P99 was 1.215/1.224 ms and became 0.686/0.708 ms. The
repeatable benefit is approximately 0.52 ms of input preparation, not a
demonstrated improvement to TensorRT kernels. Module power samples were
approximately 13.7–13.8 W in every phase; this short run cannot establish an
energy saving. Initial 32-frame output sequences matched bit for bit across
all four phases. Full input-buffer equivalence, reset and malformed-frame
contracts also have focused automated coverage.

The timing nodes end before the trailing recurrent-state D2D feedback copies;
the table does not represent every GPU memory operation or memory-bandwidth
utilization.

## Uninstrumented USB/IPC return comparison

The candidate was then run temporarily through the actual USB server and
existing comma IPC client. An independent timer was installed before changing
the runtime-only systemd override. The original service was restored before
the final A2 run; the production release files and model were not replaced.
Each phase completed 1200 frames at a 20 Hz target. All 1200 float32 output
buffers, concatenated in frame order, had the same SHA-256 across A1/B/A2.
There were no nonfinite results, worker errors or comma reboot during these
three phases. Timing summaries omit the first 100 frames of each phase.

| Phase | Frames summarized | Prepare mean / P99, ms | Server mean / P99, ms | IPC client RTT mean / P99, ms | RTT above 50 ms |
| --- | ---: | ---: | ---: | ---: | ---: |
| Existing A1 | 1100 | 1.141 / 1.321 | 26.131 / 28.105 | 40.498 / 48.344 | 8 |
| Candidate B | 1100 | 0.656 / 0.767 | 25.609 / 27.566 | 40.038 / 48.514 | 5 |
| Restored A2 | 1100 | 1.143 / 1.307 | 26.163 / 28.133 | 41.352 / 50.934 | 14 |

This confirms roughly 0.49 ms less server preparation through the real wire
path, with unchanged complete output sequences. It does **not** demonstrate
a repeatable RTT P99 reduction: B is slightly worse than A1 and better than
A2. The offroad comma had CPUs 0–3 online. Inputs were fixed prepared pixels
and a nonzero integer map, with no camera, preprocessing, DM/planner or
auxiliary classifier workload. Wi-Fi was unstable, so this is not a stable
HUD-on or BSD-on comparison and cannot substitute for the camera table above.

## Candidate implementation and boundaries

`CARROT_TRT_INPUT_FASTPATH=1` explicitly selects the candidate for stateful
TensorRT engines. Default is off. The native helper is compiled and checked
once at engine load, never per frame. A requested but unavailable helper
fails load rather than silently claiming that the optimization ran.

* Convert the newest uint8 image directly into its existing pinned FP16 input
  using a 256-entry exact IEEE binary16 lookup table in C.
* For negotiated compact map packets, decode scalar views and copy the map
  directly to its pinned binding. Avoid reconstructing the intermediate
  256×256 float32 context and casting it again.
* Keep the raw float-context path, packet negotiation and output parser.
  Maps containing noninteger values retain the original wire representation.
* Keep every scalar finite check, output/state finite checks, recurrent state
  reset, buffer binding validation and bounded packet decoding. uint8 map
  pixels are finite by their wire type; no float scalar validation is removed.

No quantization, reduced state checks, model modification, frame batching,
stale-frame reuse, busy polling, realtime-priority increase, clock/power-mode
change or USB power-policy change is included.

The existing `last_gpu_us` counter measures the whole `engine.run()` wall
interval, including copies, synchronization and checks. It is not a pure GPU
kernel timer. At a paced 20 Hz, about 24 ms of GPU execution per 50 ms period
does not imply that sampled GPU utilization should remain 100%. This does
not establish kernel occupancy or memory-bandwidth efficiency.

## Remaining limits

The direct Wi-Fi link subsequently alternated between association and loss;
both endpoints had power saving off. The managed network credentials matched,
but the supplicant recorded association rejections and connection failures.
The numeric rejection alone does not identify a firmware, RF or AP root cause.
Failed WLAN requests cannot prove that BSD CPU inference ran. A valid combined
lane+BSD/full-camera comparison remains outstanding.

Do not enable this candidate by default based on the isolated result. Obtain
an uninstrumented end-to-end comparison, stable auxiliary connectivity,
power evidence and a longer vehicle-connected observation before making
20 Hz / P99 / driving-stability claims. Raw numeric captures and reproduction
scripts are retained privately under `.analysis/archive/`; credentials and
images are not included in this document or the commit.

Focused Linux validation: 86 passed, one skipped because the separate latest
Jetlink reference checkout was unavailable. Three existing pytest configuration
warnings remain. Coverage includes the actual session compact/LZ4 paths,
malformed reset-marked packets, exhaustive uint8 conversion values, FP16/FP32
input equality, nonfinite reset, device recurrence and v2/v3 wire boundaries.
