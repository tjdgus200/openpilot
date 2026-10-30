# PV5 selective Jetlink v0.8.12 adaptations

The owner requested all three reviewed candidates and preservation in every daily
`carrot-wip` integration. This is a selective adaptation of official Jetlink
`a863c3801da6fd925d14eb1f5b1e250f8efcc649`, not a full runtime upgrade.
Existing PV5 BSD, W/temperature, battery8/BLE, Q92/FPS, USB input-current cutoff,
Park update permission, model selection/packages and optimizations remain.

1. Every Jetlink parser branch validates all final numeric arrays with
   elementwise `isfinite`, including derived standard deviations and hypotheses.
   It does not sum finite arrays (which can overflow), repair predictions, or
   reuse the previous output. A failure follows the existing connection close,
   commIssue/native reset and local-model fallback policy.
2. Device-free regression gates cover parsed faults, first reset after reconnect,
   disabled external inference, preparation order/cancellation and GPU completion
   before fallback. Existing protocol/ownership tests remain required.
3. QCOM coherent write-back output is attempted by default. A private unlinked
   serialized capture is migrated only after native/fused warp validation, in the
   existing low-priority, bounded preparation process. It checks actual kernel
   flags and 27 changing synthetic frame/transform pairs against the original
   warp. The camera buffers are reused; no per-frame allocation or migration is
   added to the inference loop. GPU completion fences and the one-copy destination
   in connection-owned shared RAM remain unchanged.

On prepared capture restoration, install the coherent output before the first
JIT link, and only if preparation validated it. Reject linked captures, unknown
capture APIs, views, shape/byte-count mismatches and non-QCOM devices. On failed
restore, deserialize an untouched capture rather than using a partially replaced
buffer. Unsupported cache flags/allocation/validation failures keep the original
warp; inability to drain GPU work fails preparation and retains native inference.
`JETLINK_COHERENT_WARP=0` remains an explicit diagnostic opt-out. The model status
file records `execution.coherent_warp` as actual installation, not requested mode.

This changes comma code only; it requires no new Jetson engine/package or protocol.
It does not change model bytes, USB traffic, held-frame policy, join permissions,
realtime scheduling or onroad engine replacement. Existing per-model pins remain.

## Evidence and limits

PC mocks check ownership, flags, allocation failure, restore, stale pixels and
fences; they do not emulate the Adreno cache or prove road safety. The earlier PC
simulation found conditional savings of about 0.46/0.71/1.09ms if copying were
1.5/2/4 times faster with 0.05ms overhead, using old N16 traces. These are neither
measured target speedups nor current-model P99 results. Startup parity is an
admission check, not long-duration cache/thermal/resource validation.

The owner-side daily policy adds preservation item12. Both Linux and Windows
gates require `test_parsed_output_guard.py`, `test_joining_boundary.py`,
`test_coherent_warp.py`, `test_warp_startup.py` and `test_input_overlap.py`;
the runner protects these tests from accidental replacement/deletion.

Sources: [official warp](https://github.com/zoompilot/jetlink/blob/a863c3801da6fd925d14eb1f5b1e250f8efcc649/jetlink/openpilot/warp.py),
[tinygrad capture lifetime](https://github.com/tinygrad/tinygrad/blob/d3f09c9bbd542fbfbe68c8569a1173550c408969/tinygrad/engine/jit.py).
