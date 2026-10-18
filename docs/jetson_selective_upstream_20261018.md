# Selective official updates for the Orin branch

Base: `codex/jetson-nav-runtime-port` at `4b1557c8`.

## Included

- From official carrot-nav `2d71d318a129702f3931f5536426300468424a06`:
  `openpilot/system/proclogd.py` and its existing regression test file. The
  implementation matches that source exactly. Process metadata is keyed by
  PID plus process start time; exited/reused entries are pruned. Startup freezes
  the manager's inherited heap before daemon resources are created, retaining
  normal garbage collection for new runtime objects.
- From official carrot-nav `49fb36a54517035fbd49028a6f9c9e7a807fa440`:
  only `next_delay` and `copy_nv12_rgb` plus their corresponding regression
  checks. Both functions match upstream's AST. NV12 padded planes go directly
  to OpenCV without first building the packed intermediate image. Scheduling
  credits elapsed preemption/I/O time instead of adding redundant CPU rest.

The observer's camera geometry, duplicate-frame checks, age thresholds, signal
identities, detection/color/green confirmation and control consumers retain
their previous behavior. The neural observer imports these shared helpers, so
its conversion/delay logic uses the same implementation without another port.
The change does not extend camera support or establish 20 Hz recognition.

## Preserved scope

No brake-lamp observer, new setting, signal departure/lead-follow policy, GPS
feature, S3 firmware, Jetson inference change or model artifact is imported.
Owner-signed USB update manifests, Wi-Fi HUD routing and the USB input-current
guard from `4b1557c8` are preserved. This update installs no Jetson package.

## Validation

- Official/selected process-memory and signal tests: **63 passed, zero failed,
  zero skipped**. Checks include PID reuse, cache bounds, inherited/new GC
  object lifetimes, exact NV12 pixel equality, output ownership after source
  mutation, timing budget and existing worker publication behavior.
- Existing Jetlink/Wi-Fi HUD regression gate: **115 passed, zero failed,
  zero skipped**.
- Ruff on all four implementation/test files and diff checks pass.
- Source parity and the implementation/test path scope are checked explicitly.

Initial PC attempts exposed missing OpenCV, sparse test dependencies and Windows
schema-path loading. Existing tracked dependencies were materialized unchanged;
OpenCV 5.0.0.93 was installed only in an isolated PC dependency directory. The
runner copies unchanged cereal/opendbc schemas into Linux temporary paths. It
does not mock conversion, tracking or process-memory implementations. Existing
test doubles remain confined to the tests that already use them. Four inherited
style warnings in the signal test were cleaned up without changing assertions.
Three pre-existing pytest configuration warnings remain.

These are PC tests. No device installation/restart, native conversion timing,
long-duration memory or driving validation was performed. A regular comma
update followed by a fresh manager start/reboot is required, since manager
preimports process modules. Replacing only the already-imported child process
is not evidence that the new `proclogd` source is active. Actual memory reduction
and signal rate must be measured with the same device workload after activation.
