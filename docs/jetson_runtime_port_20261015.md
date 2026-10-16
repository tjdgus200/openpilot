# Jetson runtime optimization port — experimental, 2026-10-15

Initial base: official carrot-nav `6f8aedb85357a8066d1df8451fb60c35902a7d7a`.
Updated test base: `8abfa30c5a9b365a4a6aee335ecdd8f3652bddad` (2026-10-16).
Reference: zoompilot/jetlink `d26001b4161f33bab28d806de6a99b96325a429b` (v0.8.7).
Branch: `codex/jetson-nav-runtime-port` in the owner's fork.

Follow-up: [owner-signed USB delivery](jetson_usb_experiment_update.md) adds an
opt-in deployment mechanism. The 2026-10-16 owner test release now ships a signed
experimental manifest and enables both options in `runtime_port_options.json`.
Combined regression testing also corrected AIO's shared
ctypes buffer prototype so it cannot break official/experimental signature checks.

## Scope and activation

Two independently selectable paths are ported. Their library defaults remain
off; this owner's test branch explicitly enables both through its bundled options.

| Environment variable | Process | Effect when exactly `1` |
| --- | --- | --- |
| `CARROT_TRT_DEVICE_STATE` | Jetson Python inference service | GPU state feedback, GPU finite checks, reply event before trailing feedback |
| `CARROT_JETLINK_AIO_WRITE` | comma `jetlinkd` | bounded asynchronous FunctionFS writes in 32 KiB requests |

These are laboratory process options, not new user settings. They are read at
engine/transport creation; changing an environment after startup does nothing.
Use only in a controlled bench/parked validation session. Enabling the comma
option does not install the host change. With the enrolled USB receiver, this
branch delivers its signed Jetson package at the next normal Jetson boot.

The official fallback host remains `989386eb`; the owner test package pins
`3225eef6`. The RL Nav ONNX and official network update/signature handling remain
unchanged. This does not alter QSPI or JetPack or replace the official trust key.
Model artifacts, camera pairing,
pose validity, control/disengagement rules and old-result reuse policy stay as
on the base revision. In particular, upstream's 46 ms held-output policy is NOT
ported. No changes to the AMD eGPU inference path are included.

## Device recurrence

Distinct GPU input/output state allocations are retained. The captured graph:

1. Uploads only current images/scalars.
2. Runs the existing TensorRT engine.
3. Copies driving output to pinned host memory.
4. Scans every float16/float32 state output for non-finite IEEE exponent bits
   on the GPU and copies a single status word to pinned host memory.
5. Records an external, blocking-sync CUDA event for the reply.
6. Copies next-state buffers to state inputs on the GPU.

The caller waits for step 5; step 6 remains ordered before the next inference on
the same stream. No state input H2D or state output D2H occurs in steady state.
The existing host state allocations are retained for binding/lifetime compatibility;
this port does not claim to free all redundant host memory.

Bad driving outputs retain the existing CPU validation and NOT_FINITE response.
Bad state outputs are rejected through queue feedback before a successful reply.
Reset schedules zeros on the same stream ahead of the next graph, after any old
feedback copy. Close synchronizes trailing work before releasing buffers/graphs.
Unsupported state dtypes/contracts, missing NVRTC, compilation failures or CUDA
errors reject the requested mode; they never disable finite checks to proceed.

The port needs the existing cuda-python bindings plus loadable CUDA runtime,
driver and NVRTC libraries. It compiles a small finite-check kernel at load,
before warmup/capture, not per frame. Real CUDA testing also found that the
existing capture wrapper's literal `1` is rejected by typed cuda-python APIs;
it now passes `cudaStreamCaptureModeThreadLocal` as the binding's enum.

## Asynchronous FunctionFS writes

The original Carrot stream sender still constructs headers, padding, sequence
numbers and wire-version locks. Only its write primitive changes when opted in.
At most 32 requests of 32 KiB are outstanding (1 MiB). FunctionFS copies source
bytes during io_submit, so callers may reuse their source buffers after submit.
This source-lifetime assumption is FunctionFS-specific, not a general AIO API
guarantee for arbitrary files/devices.

A normal-priority reaper owns completion handling. Per-request tokens, expected
byte counts and absolute deadlines detect out-of-order, short, negative or
otherwise invalid completions. On C3's 4.9 FunctionFS, successful completions
duplicate the byte count in `res2`; modern kernels return zero there. Both forms
are accepted only when `res` exactly matches the submitted length. The C3 case
was reproduced on the device and checked against
[the AGNOS kernel source](https://github.com/commaai/agnos-kernel-sdm845/blob/master/drivers/usb/gadget/function/f_fs.c).
Partial submissions advance only by the accepted
request count. Errors latch and disable the link; a partially sent message is
never resent through the synchronous path. An independent completion deadline
also runs when no later send/receive occurs. Existing submit watchdog and probe
deadlines are retained. Shutdown disables endpoints before draining/destroying
AIO; pending kernel ownership blocks cleanup/rebind rather than using an
unbounded io_destroy as cancellation.

The existing optional AIO reader is independent. No different reader scheduling
or read-pipeline activation is implied by enabling the writer. No change to
Carrot's v2 server, Wi-Fi bootstrap, HUD messages, signed updates or model contract
is required by the on-wire byte stream. Physical C3 USB testing on 2026-10-16 is recorded below; fault injection and
power-loss coverage remain hardware validation requirements.

## PC verification

Linux/WSL focused regression suite: **289 passed, 1 skipped**. Includes existing
state buffers, ResAction/RL Nav sessions and validation, v2/v3 framing, transport
discovery/cleanup, daemon timing, native finite checking, compression and AIO
reader tests plus the new port tests. The optional v3 reference integration test
is skipped because its configured external reference checkout is unavailable;
it is not counted as a pass. Pytest reports three pre-existing configuration
warnings for plugins disabled in this standalone test environment.

New tests cover 40-frame recurrent output parity with resets using deferred
CUDA operations, state/driving NaNs, non-finite scalar reset, copy/event ordering,
opt-in initialization failure, and unsupported state types. AIO tests cover
partial submission, completion reordering/errors, bounded queue, timeout without
subsequent calls, source-buffer lifetime, safe close refusal, and identical
v2/v3 bytes/padding across message boundaries. The native Linux AIO test performs
actual io_setup/io_submit/io_getevents/io_destroy against a regular file; this
verifies the syscall ABI, not USB hardware timing.

Separate actual GPU test: **1 passed** on desktop **RTX 2060 SUPER**, driver
610.88 under WSL, cuda-python 12.6.2.post1, CUDA runtime 12.9.79 and NVRTC 12.9.86.
It compiles and launches the real finite kernel, captures/replays the graph,
waits on the external event, checks float16/float32 zero/subnormal/finite/Inf/NaN
boundaries, compares D2D state bits and verifies reset ordering across 24 cases.
This test does not run TensorRT or the driving model and is not Orin validation.

Focused Ruff checks and `git diff --check` pass.

From a Linux checkout with test dependencies, run:

```sh
python3 -m pytest -q -o addopts= --confcutdir=tools/jetlink \
  tools/jetlink/test_device_state_port.py tools/jetlink/test_aio_write_port.py

# Optional actual CUDA test; cuda-python/runtime/NVRTC must be discoverable.
CARROT_TEST_CUDA=1 python3 -m pytest -q -o addopts= --confcutdir=tools/jetlink \
  tools/jetlink/test_device_state_cuda.py
```

## Required before enabling on a vehicle

- On the actual Orin/L4T/TensorRT versions, compare the same engine and input
  sequence with both device-state settings. Check driving AND all recurrent
  state output bits, reset/reconnect, non-finite rejection and teardown.
- Measure the finite scan cost, total engine/copy/validation time, reply time,
  and p99/max separately; copying less is not by itself a measured speedup.
- Compare comma-only AIO TX off/on over the physical cable, preserving USB
  role/speed, model, power profile and official failure/validity thresholds.
  Exercise disconnect/reconnect, short/failed transfers and memory pressure.
- Finally run both together with real camera pairs, checking fresh unique
  results, model/pose/DM validity, frame gaps and tail latency. A repeated old
  output is not a new inference. Parked or synthetic passes do not establish
  closed-loop driving safety or guaranteed 20 Hz.

Remove the two process options and restart the experimental processes to return
to the base paths. A signed production release/promotion is a separate step
after hardware validation; this branch does not bypass that deployment boundary.


## Physical C3 / Orin bench, 2026-10-16

Official comparison: comma `8abfa30c5a9b365a4a6aee335ecdd8f3652bddad`,
Jetson `989386eb`. Experimental: comma
`2396f2b5580edf3bada7abf0034ae797f2500279`, Jetson
`3225eef6bc89d756fdb1b8510a5afc9130cdf0ee`. Both used the same RL Nav
`1d18c42e...0961371a2e` model, C3 physical USB, L4T 36.4.7 / TensorRT 10.3.0.
Both experiments were enabled together; this is not an isolated AIO ablation.

After warmup, each run used 60 seconds of lateral-only mock input and 120 seconds
of enabled-cruise mock input. Actual cameras, modeld, locationd, driver monitoring
and plannerd ran; vehicle control processes and CAN output did not. The manager
remained offroad. Stored PV5 parameters/calibration were used unchanged. Cruise
also injected a changing nonzero 256x256 map and navigation vector through the
normal model packing path. This does NOT test live map downloads/rendering or a
real vehicle's closed-loop cruise behavior.

Cruise interval results, milliseconds (single before/after bench):

| Metric | Official | Experimental |
| --- | ---: | ---: |
| Preprocessing mean | 4.30 | 4.28 |
| Jetson server total mean | 27.33 | 25.53 |
| Client round-trip minus server total mean | 10.85 | 11.10 |
| Preprocess + round-trip + parse mean | 43.46 | 41.90 |
| Same processing path p95 | 46.70 | 44.91 |
| Same processing path p99 | 51.99 | 49.25 |
| Same processing path maximum | 60.20 | 63.26 |
| Same processing path over 50 ms | 42 / 2400 | 21 / 2402 |
| Camera timestamp to publication mean | 52.68 | 50.04 |
| Camera timestamp to publication p99 | 72.53 | 61.55 |
| Camera timestamp to publication maximum | 82.32 | 71.16 |

The residual includes USB, IPC, scheduling and uninstrumented work; it is not a
measurement of USB wire time alone. Camera-to-publication includes input age and
postprocessing, so it is not interchangeable with the 50 ms processing budget.
The server work decreased, but the residual did not improve, and the worst
processing latency increased. These observations do not establish that AIO is
beneficial or that every frame meets 50 ms. Repeated and individually toggled
runs are still required for causal attribution and tail-latency confidence.

Output rate was approximately 20 Hz in both runs. The experimental model's own
profile recorded 2402 distinct consecutive cruise frames, with no duplicate or
missing produced frame. The external conflating subscriber captured 2401: it
missed frame 3793, which the producer profile independently records as published.
All 2402 producer frames, including that 54 ms frame, are included in the timing
statistics above. No sampled model, odometry, pose-input/sensor/posenet or planner
validity failures occurred. No host-health errors occurred during measurement.
Experimental peak temperatures were C3 50.9 C and Jetson 56.031 C.

Both runs completed and restored all touched parameters, saved calibration,
CPU power state (0-3 online), and offroad status. All benchmark children exited.
No road test or actual cruise-control engagement was performed.

### Deployment issues exposed and fixed

- C3's legacy FunctionFS duplicates the completion byte count in `res2`. Strict
  length/token/error checking now accepts both that form and modern zero-res2.
- CUDA external-event flags are now used only inside graph capture; ordinary
  warmup uses ordinary event recording. The real Orin previously rejected the
  external flag outside capture with `cudaErrorIllegalState`.
- Host/device recurrence comparisons run in separate sequential processes.
  Retaining the first TensorRT engine while loading the second exhausted Orin
  memory. Process exit now releases native plans/contexts before the next mode.

Signed USB transfer and activation succeeded for `3225eef6`; the live runtime
reported that source, enabled device state, a ready engine and finite probe
frames. The updater still requires successful candidate checks before activation.
The success probe's raw numeric-difference print was not captured because the
comma reboot had stopped the temporary receiver; no exact max-absolute-difference
value is claimed. Earlier failing probe output was captured on the owner's comma.

Focused follow-up test runs passed: 79 AIO/signature tests, 43 CUDA/state-related
tests, and 20 recurrence/report-related tests (overlapping subsets, not additive).
The desktop native-CUDA test also passed. A broader 171-test attempt had 170
passes and one exported-HUD test failure caused by local export/extraction setup;
it is not claimed as all passing. Actual Orin activation passed the HUD import
and inference gate. Earlier PC-suite counts above describe prior test runs.

Temporary diagnostic receiver and UART capture were stopped. UART COM6 at
115200 8N1 yielded only two unreadable characters across a normal boot, so no
UART login or direct Jetson shell access was established. The work used the
existing comma/USB path. The private-LAN report configuration is removed from
the working branch after testing; already-published immutable diagnostic bundles
retain their original fixed expiry (2026-10-16 13:15:02 KST) and do not gain an
extended reporting window. No inference thresholds or signature checks were
relaxed to activate the candidate.
