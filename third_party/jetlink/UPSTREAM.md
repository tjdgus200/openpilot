# Jetlink vendored runtime

Source: https://github.com/zoompilot/jetlink

Local input-staging experiment (2026-10-19): `input_cast.c`/`input_cast.py` and
`direct_inputs.py` provide a default-OFF native uint8 conversion and direct
compact-map staging into fixed TensorRT host inputs. The manual
`CARROT_TRT_INPUT_FASTPATH=1` gate is not added to owner release defaults or
deployment manifests. This is a local implementation, not an upstream version
upgrade. See `docs/jetson_input_optimization_20261019.md` for PC-only evidence.

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

Experimental port on `codex/jetson-nav-runtime-port`, based on carrot-nav
`6f8aedb85357a8066d1df8451fb60c35902a7d7a`:
- Device recurrence/reply-event design from zoompilot/jetlink
  `d26001b4161f33bab28d806de6a99b96325a429b`,
  `JetlinkKit/Sources/JetlinkTRT/TrtEngine.swift`, adapted to the existing Python
  TensorRT backend. Carrot's every-state finite checks are retained through an
  added NVRTC exponent-bit scan before the reply event.
- `transport/aio_writer_native.py` derives from that revision's
  `jetlink/transport/aio.py` under the existing MIT license. Completion tokens
  and secondary results were added; `ffs_aio.py` supplies a bounded reaper and
  keeps Carrot's existing v2/v3 framing, timeout and cleanup policy.
Both paths are OFF by default. This is not an upgrade to Jetlink v0.8.7, a new
signed host release, or a port of its held-output policy. See
`docs/jetson_runtime_port_20261015.md` for validation and remaining limitations.

USB input-current policy port: zoompilot/jetlink
`491cf1ef8d6786ed51cf995e18fc8721389066c6` (2026-10-16),
`jetlink/comma/owner.py:steer_draw` and `scripts/comma/jetlink-root.sh:cmd_draw`.
Carrot's always-running daemon applies the zero-current voter only with a
verified empty USB-C port, across its configured transport modes. Its dedicated
helper verifies the vote and preserves foreign forced votes. It does not import
upstream's role switching, VM tuning, runtime or model catalog. See
`docs/jetlink_usb_power_20261018.md` for power sequencing and validation limits.
