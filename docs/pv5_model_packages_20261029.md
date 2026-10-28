# PV5 per-model Jetson packages and boot diagnostics

Models are saved by the official reboot-frozen catalog; no running engine is
changed by selecting a preference. The PV5 catalog exposes a model only when a
matching `pv5-usb-<model>.json` exists, passes the USB release contract and matches
the exact model hash/size. The original N16 USB manifest remains a fallback for
older checkouts. Official NAS signatures/keys and local owner USB policy remain.

N16 Nav and Flip Flop packages retain the same PV5 HUD and host source changes.
They use distinct source commits because existing installed bootstrap code keys
release directories and readiness by source commit. The Flip Flop packaging
commit changes only the bundled model pin from the shared N16 source. Both
bootstrap pin filenames resolve to that package's selected model. The catalog
rejects different model packages sharing a source commit. Retired MD v3 is not
exposed. These packages do not imply that Flip Flop gains navigation inputs.

After selection, reboot both comma and Jetson while parked with ignition off;
the Jetson boot gate must run, not only the comma's selection freeze. Normal
boot transfer/check/probe/activation remains in place.
The previous release and model state survive failed probes; no automatic reboot
or weakened validation is added. Park/zero initial update permission and the
prohibition on replacing an active onroad inference engine remain unchanged.

The CPU-only blue diagnostic screen again displays nine fields: storage mode,
boot stage, Wi-Fi, addresses, SSH, comma USB service, update phase/progress,
host model and free/total storage. Update state expires after ten seconds;
USB approval is labelled separately from model readiness. Arbitrary updater
exceptions are reduced to allowlisted failure categories. A runtime-version
tag refreshes only the boot diagnostic service after a package activation;
subsequent reconnects do not restart it. The immutable early recovery screen
and exclusive panel handoff remain in place. No extra comma rendering work is
introduced. Initial boot before writable DATA mounts may use the older
immutable recovery screen until runtime migration starts.

Verification covers catalog identity/reboot binding, separate release IDs,
USB first-to-second-to-first transfers, second-model probe failure rollback,
boot-screen freshness/redaction/rendering and diagnostic migration. Additional
existing BSD, W/temperature, battery8, BLE100ms, Q92/FPS, power, permission and
cleanup regressions remain required. PC fixtures and ONNX contract checks do
not establish physical TensorRT output, P99 or driving safety on the new model.

Published PC-tested runtimes:

- [N16 Nav package](https://github.com/tjdgus200/openpilot/releases/tag/jetson-pv5-model-n16-20261029-88aa2548),
  source `88aa2548282b8338dec5bb574e6f4f0097804f6d`.
- [Flip Flop package](https://github.com/tjdgus200/openpilot/releases/tag/jetson-pv5-model-flip-flop-20261029-c0ebdfaa),
  source `c0ebdfaaaff5236837074420d7e7d48ae39e2551`.

All four public assets per release were anonymously downloaded and hashed.
Shared-source Linux regression569 and Windows preservation368 checks pass.
Both exported archives independently import the server/HUD and exercise boot
rendering, overlays and model input-queue contracts without checkout imports.
N16-only staging optimizations remain restricted to its exact tensor contract;
Flip Flop retains the default non-N16 input path. Model endpoints return the
pinned byte lengths; device installation still verifies full model hashes and
executes the existing TensorRT probe before activation. No device was installed
or rebooted during this publication.
