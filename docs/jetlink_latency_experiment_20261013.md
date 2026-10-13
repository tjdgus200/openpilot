# Jetlink state-copy and USB latency experiments (2026-10-13)

## Scope and review base

이 브랜치는 Jetson 상태 복사와 콤마 USB 요청 전송 비용을 줄이는 실험만 담습니다.
모든 실험 옵션은 기본적으로 꺼져 있습니다. 모델, 펌웨어, 업데이트 경로,
온로드 조건, 출력 유효성 기준과 차량 제어 코드는 변경하지 않습니다.
실제 USB 시험에서는 평균 지연이 감소했지만, 최대 왕복 지연은 증가했습니다.
Jetson 상태 버퍼 변경은 실제 CUDA/TensorRT 검증 전입니다.

The review base is official `carrot-nav` commit
`ad4e0c33f637d7791cd1d943c2483e03b1647815`, used during the comparison.
The Jetson server used source commit
`955a6f2d39a8cff5a46214113689e6cfba79533e`.

Upstream subsequently added overlapping optimizations in
[`174dc5a145a916796abaca2ea8387d6fb2de5bdd`](https://github.com/ajouatom/openpilot/commit/174dc5a145a916796abaca2ea8387d6fb2de5bdd),
including recurrent-state buffer reuse, finite checks and a navigation payload codec.
This branch deliberately retains the tested parent as its base. Compare and port
individual changes against newer upstream code; do not replace its files wholesale.
This experiment does not implement the new navigation codec.

## Opt-in changes

| Process | Environment variable | Effect when set to `1` |
| --- | --- | --- |
| Jetson server | `JETLINK_SHARE_STATE_HOST` | Reuse each next-state pinned host output as the following frame's host input. |
| Jetson server | `JETLINK_FAST_HALF_CHECK` | Use reusable scratch arrays for an exact full float16 finite-value scan. |
| Comma Jetlink daemon | `JETLINK_COALESCE_INFER_WRITE` | Raise the write cap from 512 KiB to 1 MiB for inference requests only. |

These are process environment variables, not persistent user settings. No service
unit, host-update manifest or device installation is changed here. Restart the
relevant process without the variable to return to the baseline path.

### State buffers and validation

Buffer sharing removes an extra **CPU-to-CPU** state copy. Device input/output
allocations remain separate; H2D and D2H transfers still occur. Binding names,
shapes, dtypes, sizes and one-to-one pairing are checked before any alias is made.
Sharing must be configured before views are handed out, warmup or graph capture.
The existing single stream enqueues H2D, execution and then D2H, and `run()`
synchronizes before feedback validation. A shared-transfer exception latches a
fault requiring engine reload, rather than permitting partially overwritten state
to be used on another connection.

Every output still undergoes shape and finite-value checking. State dtype checks
remain in place, and rejected outputs reset all recurrent state. `np.copyto` is
skipped only for the exact same array object. The float16 checker scans all
exponent bits without sampling or clipping; other layouts/dtypes use NumPy's
original finite check. Original pinned allocations stay owned until `close()`
and are freed once. This is not a memory-footprint reduction.

### USB writes

The measured RL Nav request occupied 671,744 wire bytes after padding. The
baseline split it into two writes (524,288 and 147,456 bytes); the experiment used
one. Protocol bytes, replies, timeouts, watchdog behavior, other message types
and the existing ENOMEM shrinking path are preserved. A larger contiguous
allocation can fail under memory pressure; sustained fragmentation and tail
latency still need device testing. There is no separate IPC implementation change:
the measured IPC roundtrip includes the USB change's effect.

## Actual comma 3 / Orin USB comparison

This was an offroad, fixed synthetic-input comparison, not a real-camera or
vehicle-control trial. The order was A1, B1, B2, A2, with 15 warmup and 120 measured
requests per phase: 240 measured requests per configuration. Both configurations
used the same measurement instrumentation. Only inference write coalescing was
enabled for B; the Jetson state-buffer candidate was not installed.

The loaded RL Nav model SHA-256 was
`1d18c42ea4cd95c8bffab4635844b78b8e2d4e25b9de44a9c5ac2e0961371a2e`.
Each phase reset and replayed the same input sequence. Corresponding output
hashes matched across phases. No write-cap shrink occurred in these short trials.

| Metric (ms unless noted) | A: baseline | B: coalesced |
| --- | ---: | ---: |
| USB send mean | 7.170 | 5.336 |
| USB send p95 | 9.191 | 6.815 |
| IPC / USB / Jetson roundtrip mean | 50.404 | 48.511 |
| Roundtrip p95 | 55.780 | 53.009 |
| Roundtrip maximum | 62.656 | 75.574 |
| Server mean | 33.609 | 33.539 |
| Writes per request | 2 | 1 |
| Wire bytes per request | 671744 | 671744 |

Mean USB send savings were 1.834 ms; mean roundtrip savings were 1.893 ms.
These measurements overlap and must not be added. The worse maximum prevents
claiming a general tail-latency improvement. Camera capture, image warp,
postprocessing and publication are outside this measurement. It does not establish
a sustainable 20 fps camera pipeline or a fix for `locationd` validity errors.

An anonymized aggregate is in
[`tools/jetlink/validation/latency_usb_ab_20261013.json`](../tools/jetlink/validation/latency_usb_ab_20261013.json).
No credentials, device addresses or raw user logs are included. The temporary
measurement daemon exited, official USB operation was restored, and offroad
state was checked before devices were disconnected.

## PC verification and remaining work

Run from the repository root with Python and NumPy installed:

```sh
python tools/jetlink/test_latency_candidates.py
```

Ten CPU tests passed, covering all 65,536 binary16 patterns, nonfinite outputs,
fallback dtypes/layouts, state resets, reference-versus-shared recurrence,
simulated graph capture/replay, buffer lifetime, transfer failure, invalid/late
pairing, byte-identical USB framing, ENOMEM fallback and disabled defaults.

The CUDA/TensorRT test double checks sequencing and ownership using a small
deterministic recurrent graph. It cannot establish actual GPU numerical parity
or Jetson performance. Before enabling the host options in a release, measure
real TensorRT output parity over resets/reconnections, sustained thermal and
memory-pressure behavior, and complete camera-to-publication latency under the
intended workload. No hardware validation or driving-safety guarantee is claimed
for the host-buffer candidate.
