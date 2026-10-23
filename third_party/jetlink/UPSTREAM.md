# Jetlink vendored runtime

Source: https://github.com/zoompilot/jetlink
Revision: `194ff6dc71cd27282378fff155e94daa7770152b` (0.3.0a1).
The v2 runtime is retained. `transport/base.py` adds a per-connection
`wire_protocol` selection hook; its default remains v2. See LICENSE.
`jetlink/protocol_v3.py` is an unmodified copy of `jetlink/protocol.py` from
zoompilot/jetlink `24e8917673829cbe1d146735956e9038cc93f227`.
PR #534 extends `protocol.py`, `client.py`, `spec.py` and `transport/base.py`
with strict per-stream v2/v3 selection, HELLO link metadata, queued/stateful
model contracts and validated full-output replies. `transport/ffs.py` reports
reader scheduling failures to the waiting consumer. The vendored server and
default protocol remain v2; these changes do not publish a new Jetson release.
`compat.py` retains the earlier pinned-model adapter for regression coverage;
the production daemon now uses the extended `JetlinkClient`.
Carrot's adapter, device-role policy, process placement and deployment tools are
outside this directory. Updating the protocol requires testing both peers.

`carrot-nav-pv5` selectively ports GPU recurrence/finite scanning from the
owner's `codex/jetson-nav-runtime-port`.
The designs derive from zoompilot/jetlink `d26001b4161f33bab28d806de6a99b96325a429b`
(`JetlinkKit/Sources/JetlinkTRT/TrtEngine.swift`),
under its existing MIT license. Carrot's finite checks, deadlines, framing,
sequence checks, resets and failure containment remain. No held-output policy,
frame skipping or different ONNX is imported. Release choices enable GPU state
on the paired Jetson package. The owner subsequently cancelled the experimental
asynchronous TX port: its transport, native writer and release option are now
removed for all comma models. The daemon directly uses the official serial
`transport/ffs.py`, verified identical to carrot-nav `3a03a39f` on 2026-10-21.
Official userspace placement, write watchdog/cleanup and USB completion-priority
ownership/restoration gates remain; the PV5 adapter subsequently permits C3/C3X
as well as C4 on the same aarch64 Linux4.9.103 kernel, without new C3 timing or
kernel-safety evidence. Existing optional AIO RX diagnostics are unrelated
and unchanged. This is not a complete Jetlink upgrade or proof of 20Hz.
See `docs/carrot_nav_pv5.md`.

On 2026-10-23 PV5 imports ajouatom/openpilot `cd986d7e`'s write-watchdog
wake coalescing. Earlier deadlines and close still wake immediately; an
already scheduled earlier wake rechecks a later deadline or cancellation.
Serial USB writes, timeout budgets and abort ownership are unchanged.
