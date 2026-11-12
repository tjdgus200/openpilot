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
