# PV5 Jetson Super startup selection

Port the supported Orin Nano startup selection from upstream `8ca67794ca` and
the update-apply GPU ordering correction from `5a2784cc2d`. Retain the five
model-paired PV5 runtimes, current HUD and default-off performance diagnostics.
The upstream signed N16 bundle is not a substitute for a PV5 package.

At startup, resolve the unique `MAXN_SUPER` ID from NVIDIA's active board
profile, select it when needed, verify the selected mode, run `jetson_clocks`,
and check the GPU ceiling. Missing profiles, failed commands, a rejected mode
change, a requested reboot, and an old firmware ceiling remain failures.
Mode IDs and frequency limits are never invented; thermal protection remains.
The NVIDIA reboot prompt receives `NO`. No firmware flashing or automatic
reboot occurs.

New images install a root oneshot before update-apply candidate GPU validation,
Xorg, HUD and inference. The installed updater also runs its trusted helper
before candidate GPU probes. Existing runtimes attempt setup before GPU backend
imports, using the existing passwordless-sudo startup path. The updater copies
the helper from the validated current runtime for the next boot; old bootstrap
payloads remain compatible. No candidate power helper is executed as root
before ordinary candidate validation.

The result is locked and cached once per boot under `/run`. Existing host health
reads it once per process and carries a small state and failure reason. No new
observer, timer, sampling worker, route logger, model input or inference is added.
Protected runtime migration does not remount or write the immutable OS.

An old image may have opened a GPU context before the runtime's attempt. If
NVIDIA refuses that mode change, the result remains `reboot_required`; merely
restarting a process does not retry this boot. Incompatible image/QSPI firmware
or missing early boot ordering can require separate image repair. This release
does not establish that an existing physical device reaches Super successfully.

PC tests cover profile parsing, supported boards, exact command order, failure
paths, once-per-boot locking, existing updater migration and offline image unit
ordering. Five isolated package checks and the permanent PV5 preservation gate
are required before release. These are not physical power, thermal, latency or
vehicle safety measurements. Published SD image binaries are not rebuilt here.

## Release evidence

The clean source tree at `7cf04a5bf89138f7855326841faa0cd56ca11511` passed
the permanent Linux PC gate: **3,128 passed, 0 failed, 0 skipped**.
All five isolated native archive/server/HUD checks passed. In each
archive, six startup/power integration files matched its exact source
commit byte for byte. All twenty public release assets were downloaded
anonymously and their full SHA-256 hashes and sizes matched.

Each model retains its previous ONNX URL, hash and size. The five
source commits have one identical tree and distinct package identities.
The catalog and legacy N16 USB bootstrap now pin these releases;
previous packages remain available for rollback.

| Model | Package source | Verified public release |
| --- | --- | --- |
| n16 | `37b31958cb30e9c18bd01eb8eef035ea5dcce4a0` | [4 assets](https://github.com/tjdgus200/openpilot/releases/tag/jetson-pv5-auto-super-n16-20261113-37b31958) |
| flip-flop | `84aa4f5b8cf55561b93060f337c5d6dd7c01e2da` | [4 assets](https://github.com/tjdgus200/openpilot/releases/tag/jetson-pv5-auto-super-flip-flop-20261113-84aa4f5b) |
| ctv3m | `f9233b22ac0913def0c815b3cd87d774ee928b31` | [4 assets](https://github.com/tjdgus200/openpilot/releases/tag/jetson-pv5-auto-super-ctv3m-20261113-f9233b22) |
| light-speed | `1507f8863ad66fce1c4a8ba918251ac3c3a8ba7a` | [4 assets](https://github.com/tjdgus200/openpilot/releases/tag/jetson-pv5-auto-super-light-speed-20261113-1507f886) |
| light-speed-2 | `7cf04a5bf89138f7855326841faa0cd56ca11511` | [4 assets](https://github.com/tjdgus200/openpilot/releases/tag/jetson-pv5-auto-super-light-speed-2-20261113-7cf04a5b) |

No physical Jetson activation, GPU timing, image/QSPI repair, vehicle
installation or reboot was performed. Existing devices receive the
package through the normal permission-gated update path. Old images
can still report `reboot_required` or a firmware/profile failure.
Public SD image binaries were not rebuilt.
