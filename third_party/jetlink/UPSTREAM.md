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


Isolated optional inference review on official carrot-nav74955c98:
GPU recurrence/reply-event design and native FunctionFS AIO writer are adapted
from zoompilot/jetlink d26001b4161f33bab28d806de6a99b96325a429b under MIT.
Carrot retains full-state finite validation, wire framing and serial inference.
The native uint8-to-binary16/direct compact-map staging path is an owner-side
optional implementation, not a complete Jetlink release upgrade. All three
options default off; no deployment manifest, model or Wi-Fi changes are included.
See docs/jetson_inference_review.md for evidence and remaining validation.
