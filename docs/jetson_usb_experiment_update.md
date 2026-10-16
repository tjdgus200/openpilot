# Comma-to-Jetson USB experiment delivery

This branch adds a separate owner-signed USB release channel. The official
Carrot NAS host allowlist, official signing public key, model selection,
activation probe and rollback remain in place. The owner's enrolled public key
is included; no private key is included. This owner test branch now includes a
signed `usb_host_release.json` for source `3225eef6`, published as
[jetson-usb-20261016-3225eef6](https://github.com/tjdgus200/openpilot/releases/tag/jetson-usb-20261016-3225eef6).
The 2026-10-16 owner test release explicitly enables both experiments through
`tools/jetlink/runtime_port_options.json`. This file is part of the signed source
bundle, not a persistent system environment override. Removing it leaves both
options off; an explicit process environment value of `0` overrides it. Official
branch/release restoration therefore does not inherit these experiment settings.
Before activation, the candidate compares host and device state feedback using
the same TensorRT engine over 32 deterministic frames, including a reset halfway
through, and rejects nonfinite or differing driving outputs (rtol/atol 1e-4).
Each mode runs in a separate, sequential process so the first TensorRT engine
and CUDA context are released before loading the second. This is a
bounded numerical check, not road validation.

## Flow

1. Enroll the owner's **public** Ed25519 key and receiver on Jetson once, using
   a trusted local root console or recovery-mounted DATA. Wi-Fi/USB cannot enroll
   a replacement trust root. Keep the private key on the release author's PC.
2. Publish an immutable committed host bundle and the matching ONNX, sign their
   hashes, sizes, URLs, source commit and unique profile. Put the signed manifest
   and public key in `openpilot/selfdrive/modeld/jetlink/` on the custom branch.
3. After comma updates that branch, the next Jetson boot advertises its USB
   receiver. With comma offroad, comma downloads/caches the signed assets and
   sends 256 KiB acknowledged chunks over the existing USB boot-gate transport.
   Only comma needs Internet (or a complete cache); Jetson does not.
4. Jetson independently verifies against its locally enrolled key, checks exact
   lengths and SHA256, and registers an immutable root-owned local release.
   Partial transfers are never activated. A disconnect restarts the transfer;
   fully registered packages are reused. There is no chunk-resume protocol.
5. The existing stopped-service stage/probe/transaction/rollback activates the
   candidate before inference. Failure retains the old files and withholds
   model readiness rather than silently using a mismatched model.
6. Returning comma to an official branch removes the USB selection. On Jetson's
   next boot the normal official signed NAS selection applies. Internet may be
   necessary for official restoration. No official signing key is changed.

Updates are not applied to an already-running inference session. Restart Jetson
normally after updating comma, with the car parked/offroad. No recovery jumper
is needed after initial enrollment. Existing current experiments can boot
without retransmission. Missing receiver support fails with an explicit message.

## One-time enrollment (Jetson/recovery Linux)

After verifying the checkout and public key locally, with services stopped:

```sh
sudo python3 tools/jetlink/install_usb_receiver.py \
  --runtime /opt/carrot-jetlink --public-key /path/to/owner-public.pem
```

For recovery use the mounted DATA runtime path instead of `/opt/carrot-jetlink`.
The installer preserves the current release, official key and original updater
backup; it refuses silent key rotation. Do not run this against a live runtime.
Enrollment enables only the transfer capability, not either performance flag.

### Enrollment recorded 2026-10-16

The owner's Orin received the USB receiver based on `227d0267` and the public
key in `openpilot/selfdrive/modeld/jetlink/usb_host_public.pem`. Its Ed25519 public
key DER SHA256 fingerprint is
`231b3468f502fcaa66994220f885a8e9e0993135a42b0491e2ebb7aa9dd66316`.
The private signing key stays on the owner's PC, outside this repository.

Installation preserved the official `989386eb` runtime, official trust key,
boot policy and L4T 36.4.7. Only DATA updater files/public key were changed;
APP and QSPI were not written. Fresh read-only readback verified the installed
files. On the device's ARM64 Python environment, a signed non-installable test
manifest was accepted, a modified one rejected, and a simulated boot HELLO
advertised `carrot_usb_release_v1`. At the enrollment stage no experimental bundle was activated. Subsequent
physical USB delivery on 2026-10-16 activated source `3225eef6`; the running
Orin reported that source and `carrot_device_state_enabled=true`. No APP/QSPI
flash was needed. The signed acceptance checks were retained.

## Preparing a release (author PC, Linux Python + pycryptodome)

Use a private Ed25519 key outside the Git checkout. Export sources using
`python3 tools/jetlink/build_host_bundle.py /path/host.tar.gz` from a clean commit.
Publish assets at immutable HTTPS URLs and run:

```sh
python3 tools/jetlink/prepare_usb_release.py \
  --bundle /path/host.tar.gz --model /path/model.onnx \
  --private-key /private/owner-ed25519.pem --profile runtime-port-001 \
  --bundle-url https://example.org/releases/runtime-port-001/host.tar.gz \
  --model-url https://example.org/releases/runtime-port-001/model.onnx \
  --output openpilot/selfdrive/modeld/jetlink
```

Commit `usb_host_release.json` and `usb_host_public.pem` only on the custom
branch. Never publish the private key. The manifest references the earlier
source bundle commit; the subsequent manifest commit need not equal it. Reuse
neither profile nor source identity for different bundle contents. Model changes
must also update and validate the comma-side model contract.

## Validation status

The initial implementation (`1390d3c8`) was pushed before tests as requested.
After corrections, the combined Linux/WSL regression suite reports **376 passed,
1 skipped**. The skipped test needs an external upstream v3 reference checkout;
it is not counted as a successful compatibility test. Three configuration
warnings come from pytest plugins disabled in the standalone test environment.

USB tests use real Ed25519 signatures, root-owned temporary files and a simulated
USB transport. They cover multi-chunk delivery, wrong signatures/keys, malformed
or replayed chunks, download corruption, onroad/disconnect interruption, cache
reuse, disk budget, candidate rejection and official NAS restoration. Service
commands and TensorRT probes are mocked; no installed device is modified.

Combining AIO and signature tests exposed a real process-wide ctypes prototype
collision: the AIO module modified cached CPython buffer-function declarations
also used by PyCryptodome. It now uses private PYFUNCTYPE bindings. Two subprocess
regression tests verify both import orders with actual signing/verification and
buffer access. The boot HELLO also tolerates missing current-version metadata;
large registered assets are not rehashed on every 500 ms selection heartbeat.
They are still verified during transfer completion, staging and activation.

Focused lint and `git diff --check` pass. Physical USB delivery and the Orin
TensorRT runtime were exercised on 2026-10-16; see the linked runtime report
for bench measurements. Power-loss behavior and actual vehicle operation
still require validation.
Do not interpret a signed package or finite synthetic probe as road validation.
