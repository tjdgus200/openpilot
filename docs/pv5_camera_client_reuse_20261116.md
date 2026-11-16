# Reuse camera subscriptions during display switching

The PV5 FOV preview alternates road and wide views. Previously each switch constructed a new `VisionIpcClient`. The current msgq reader count is cumulative: closing a subscriber does not reclaim its slot. Crossing 40 slots evicts all subscribers, and rejoining at the write pointer can discard a pending model input packet.

Both C3X and mici CameraView implementations now own a bounded, lazy pool with at most one client per defined stream. Switching changes the selected client; it does not allocate a new socket. An inactive stream's conflated queue is drained once and the view waits for a subsequent frame before switching. This adds a frame wait at selection/reconnection, without a timestamp comparison across BOOTTIME/MONOTONIC. Normal steady display does not perform additional reads or background work.

Each stream retains its buffer mappings until the view closes or reconnects. These are shared mappings, not a copied image cache; their device footprint has not been measured. Offroad transitions cancel pending switches and clear displayed frames/EGL imports. The next onroad connection reimports buffers using the same socket. Reconnect attempts are throttled to 0.2 seconds per stream. EGL imports are released before a current client replaces its buffers. Closing clears primary, pending and cached references and is idempotent.

## Validation

Baseline: `620674bb795178a05b52279c3fc9aa7470405bd6`. Changes are limited to the UI pool and two camera views, tests, and this note. No model, pose, calibration, sensor, Panda/CAN, msgq, controlsMismatch threshold, disengagement timer or warning minimum duration changes.

291 PC tests passed, 0 failures/errors/skips. Includes both actual CameraView classes with GL doubles, real unchanged msgq/VisionIPC C++ and Cython buffers/sockets on Linux PC, restart with a different buffer width, inactive queue wrap across 40,000 publications, pending/cancelled switches, onroad transitions, frame pairing/gap diagnostics, FOV preview and alert-layout regressions. The isolated native build keeps C++ assertions enabled. Shader rendering and the device's ION/EGL/DMABUF path are not validated by these CPU-buffer tests.

Identical native IPC before/after scenario, for EACH C3X and mici implementation:

| Metric | Before | After |
|---|---:|---:|
| Display switches | 1,000 | 1,000 |
| UI clients constructed | 1,001 | 2 |
| Missed model-role reads / 12,000 checks | 78 | 0 |
| Final subscribers, road / wide | 23 / 22 (after repeated eviction) | 4 / 4 |
| File-descriptor delta after closing | 0 | 0 |

The fixture uses three persistent model-role receivers per stream plus the UI, two streams, 16x16 CPU buffers and deliberately queues a packet immediately before switching. The 78 counts are per-receiver read misses, not 78 independent camera captures. It reproduces the mechanism; it is not full model inference, vehicle timing/load, or a measured real-drive failure rate. Frame-pairing/gap and all safety logic remain unchanged.

Early test harness issues (WSL FD capture, AST locations and a missing clock handle) were corrected. One initial native invocation selected the unbuilt package and skipped; the final isolated import ran all native cases with no skips. Remaining warnings are three unavailable pytest-plugin configuration entries and 398 existing NumPy shape deprecations.

No vehicle access, installation, restart, model changes, additional telemetry, or changes to the operating PC receiver/policy were performed. Existing logs support this cause strongly, but did not record the exact device reader eviction. Hardware validation of the fix remains separate.
