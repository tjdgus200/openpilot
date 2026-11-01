# Immutable Jetson boot-screen refresh, 2026-11-01

The owner requested changing the base image as well as the runtime screen.
The installed R2 SD image starts an immutable copy under
`/usr/local/lib/carrot-jetlink/boot-display`. Updating the DATA runtime and its
volatile service override does not replace that early boot/recovery copy.
That is why an old blue screen can still appear before the runtime takes over.

The refreshed copy uses the existing black CPU-only screen, all nine status
fields, and the same22px temperature | CPU | MEM | PWR line. The existing
one-second local sampler and exclusive panel handoff remain. Unknown readings
show `--`. There is no new comma workload, model inference, GPU work or OBD query.

`tools/jetlink/refresh_boot_image.py` creates a **separate regular image file**
from the exact unprovisioned40GiB R2 base (SHA-256
`b11f5601d3a713ad0de23315ee90daddf5452f8e548f2c87c8eeec28d321e55f`).
It rejects other hashes, physical disks, symlink inputs and changed partition
geometry. It installs a committed diagnostic snapshot through the same
`install_boot_display.configure` used by new image builds.

The R2 factory HUD uses `/dev/shm` for panel ownership, whereas new diagnosis
uses `/run`. Replacing the screen alone would allow competing panel owners.
The refresh therefore also pairs the two factory `display_owner.py` helpers
on APP and DATA with the diagnostic helper. The existing diagnostic service
drop-in creates the shared private `/run` directory for both services. The old
factory model and inference implementation remain unchanged.

The refresh checks every APP/DATA file's content, type, permissions, owner and
symlink target before and after, excluding only diagnosis and those two helpers.
It compares every byte outside APP/DATA, including GPT, firmware, SETUP and gaps.
It audits private identity/setup
state, checks filesystems/GPT and renders the installed screen with PC libraries.
The factory runtime/model source and diagnostic source have separate identities
in candidate metadata; this does not pretend to upgrade the entire runtime.
The four PV5 model package pins remain unchanged.

These checks do **not** validate ARM imports, actual USB display, protected
boot, live model readiness or power loss on a Jetson. A changed full-image hash
cannot reuse the old R2 card/owner boot report. Output remains
`PROTECTED_CANDIDATE_NOT_BOOT_TESTED`; existing public download replacement
requires evidence for that exact new image. The normal Windows installer
publisher retains its validation gate.

Existing SD cards do not change through ordinary comma git pull or USB runtime
updates. Applying an immutable image requires a separately authorized offline
SD rewrite and erases the selected card. This task does not rewrite any card,
remount live APP, change thermal policy or restart a connected device.

## PC artifact and checks

- Final raw image:40GiB; SHA-256
  `ec17a804c9d22d5c2fb2e65f4f656a4866fe3f9b1c0ca0e80ee7ac4aaceee822`.
- Diagnostic snapshot: `cc15e1022eae112329885bcab5d3be38b4bc319d`;
  original factory model/runtime basis: `b4df5489b2307d6f29befc69bf73024e4632a909`.
-190,134 other APP entries and6,301 other DATA entries retained exact logical
  contents, types, modes, owners and symlink targets. GPT/firmware/SETUP/gap
  bytes matched; APP/DATA ext4, SETUP FAT and GPT checks passed.
-66 focused PC tests passed, no failures/skips. The screen was rendered from
  the installed immutable files and inspected. Neither ARM nor hardware boot
  was tested. Public default installer remains the previously validated image.

The PC artifact is stored outside Git under
`jetson-nvme/black-base-image-20261101/image/`. The build used an initial
diagnosis-only staging image followed by an independently hash-verified paired
factory-helper step. That intermediate is not a release. The committed builder
performs both changes together for subsequent builds.
