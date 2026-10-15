# Jetson runtime optimization port — experimental, 2026-10-15

Base: official carrot-nav `6f8aedb85357a8066d1df8451fb60c35902a7d7a`.
Reference: zoompilot/jetlink `d26001b4161f33bab28d806de6a99b96325a429b` (v0.8.7).
Branch: `codex/jetson-nav-runtime-port` in the owner's fork.

## Scope and activation

Two independently opt-in paths are ported. Defaults remain the existing runtime.

| Environment variable | Process | Effect when exactly `1` |
| --- | --- | --- |
| `CARROT_TRT_DEVICE_STATE` | Jetson Python inference service | GPU state feedback, GPU finite checks, reply event before trailing feedback |
| `CARROT_JETLINK_AIO_WRITE` | comma `jetlinkd` | bounded asynchronous FunctionFS writes in 32 KiB requests |

These are laboratory process options, not new user settings. They are read at
engine/transport creation; changing an environment after startup does nothing.
Use only in a controlled bench/parked validation session. Enabling the comma
option does not install the host change. Cloning this branch alone does not
update the signed Jetson package or enable either experiment.

The pinned Jetson host remains `989386eb`, the RL Nav ONNX remains unchanged,
and official network update/signature handling remains unchanged. This task
does not publish a signed runtime, modify the installed devices, alter QSPI or
JetPack, or replace the official update channel. Model artifacts, camera pairing,
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
otherwise invalid completions. Partial submissions advance only by the accepted
request count. Errors latch and disable the link; a partially sent message is
never resent through the synchronous path. An independent completion deadline
also runs when no later send/receive occurs. Existing submit watchdog and probe
deadlines are retained. Shutdown disables endpoints before draining/destroying
AIO; pending kernel ownership blocks cleanup/rebind rather than using an
unbounded io_destroy as cancellation.

The existing optional AIO reader is independent. No different reader scheduling
or read-pipeline activation is implied by enabling the writer. No change to
Carrot's v2 server, Wi-Fi bootstrap, HUD messages, signed updates or model contract
is required by the on-wire byte stream. Actual dwc3 behavior with the smaller
requests remains a hardware validation requirement.

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
