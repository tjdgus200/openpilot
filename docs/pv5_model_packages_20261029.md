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

After selection, normal boot transfer/check/probe/activation remains in place.
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
