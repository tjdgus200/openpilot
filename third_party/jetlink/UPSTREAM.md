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

`carrot-nav-pv5` selectively ports GPU recurrence/finite scanning and bounded
FunctionFS asynchronous writes from the owner's `codex/jetson-nav-runtime-port`.
The designs derive from zoompilot/jetlink `d26001b4161f33bab28d806de6a99b96325a429b`
(`JetlinkKit/Sources/JetlinkTRT/TrtEngine.swift` and `jetlink/transport/aio.py`),
under its existing MIT license. Carrot's finite checks, deadlines, framing,
sequence checks, resets and failure containment remain. No held-output policy,
frame skipping or different ONNX is imported. Release choices enable GPU state
on the paired Jetson package and AIO TX by default only on the tested
`comma tici`/aarch64 Linux4.9.103 family. Other hardware keeps serial writes and
the official USB completion-priority applicability gates. This is not a complete
Jetlink version upgrade or proof of 20Hz. See `docs/carrot_nav_pv5.md`.
