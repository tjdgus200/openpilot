# Jetlink Park update handoff and dark startup diagnosis

The boot diagnostic now uses a black background with the existing nine status
rows: storage protection, boot stage, Wi-Fi, IP, SSH, USB service, update progress,
selected model, and free/total DATA space. The ordinary HUD waits for fresh,
valid vehicle telemetry before requesting the shared display lock. If that data
disappears, it closes only HUD resources and returns ownership to the diagnostic.
BSD, battery8, W/temperature, Q92/FPS and the shared USB reset prohibition remain.
The immutable early recovery screen on an already installed image is unchanged;
the existing version-aware runtime diagnostic override takes over after mounting
the selected runtime. A newly built image includes the black recovery screen too.

## Running connection to update mode

Only a host advertising `carrot_park_update_v1`, with its existing locally enabled
SHA-256 USB receiver and boot-update policy, supports this handoff. Both the
capability and request handler require the actual protocol2 USB transport;
TCP, application peers and older protocols cannot schedule this action. Older hosts
retain the ordinary reboot update procedure until this package is installed.

1. Between model requests, the comma checks admission at most5Hz. It requires
   unambiguous onroad state, fresh(<250ms), valid/alive CAN Park, raw speed exactly
   zero, CAN validity and inactive carControl/selfdriveState. Only then is the
   selected same-boot-model manifest read, at most1Hz. Identical source commits
   and different model hashes/sizes do nothing. Model selection remains frozen
   for the boot; this is a runtime package update, not live model switching.
2. A bounded, sequenced request contains only the selected USB manifest and a
   Park assertion. The host rechecks size, age, local policy, model identity and
   storage contract. It acknowledges before scheduling one fixed local root
   action. No packet can supply a command, path, service name or network login.
3. The root worker checks its private, bounded, non-symlink request file, current
   boot identity and age. Immediately before stopping services it independently
   decodes fresh forwarded carState/carControl/selfdriveState and repeats the
   Park/zero/inactive checks. A lost approval leaves the old runtime running.
4. The worker records a root-only per-boot recovery marker, stops inference and
   HUD, and confirms their retirement. Failure/unknown state cannot open the
   USB gate. It reuses the single updater lock and normal USB teardown; no forced
   FunctionFS rebind, power change, reboot or Bluetooth operation is added.
5. The now empty boot gate receives and verifies the package through the existing
   receiver. The comma renews approval every200ms only while fresh Park/zero/
   inactive control or unambiguous offroad permission holds. Leaving Park sends
   an explicit revocation. The host lease expires after1s without renewal;
   disconnect invalidates it. Remembered handoff files never authorize activation.
6. Activation still requires stopped services, existing integrity/storage/model
   checks and a separate synthetic TensorRT probe. Park permission is checked
   before and throughout the probe (200ms waits), after the probe and immediately
   before replacing the runtime link. Permission loss kills/reaps the entire
   probe group, restores the prior cache/runtime, and retains the staged package
   for a later Park approval. Probe failure retains the previous release as
   before. Final USB context closure precedes restarting inference/HUD.

The admission and lease are bounded freshness checks, not instantaneous vehicle
state proofs. During the deliberate handoff the external inference link is
unavailable and the comma follows its existing joining/fallback behavior. This
change does not alter control outputs or certify road safety. Test first while
parked: check P->D during transfer/probe, disconnect/reconnect, process interruption,
rollback and the first valid model output after reconnection on actual hardware.

## Resource bounds and verification

Normal inference never performs package downloads, hashes, TensorRT probes or
extra GPU execution. Non-supporting peers are a no-op. The supported watcher
polls three existing cereal services5Hz, reads a small manifest at most1Hz in P,
and rate-limits failed handoff attempts to5s. The separate updater uses Nice19
and CPU affinity0/1. The diagnostic remains CPU-only,1fps, without TensorRT/Xorg.

`test_park_update.py` covers fresh admission, unchanged/mismatched pins, request
ordering/replay/denial, root command restriction, host vehicle decoding, revocation
and probe-group cancellation. Linux-specific lifecycle cases cover private file
identity/size/permissions, stopped-service proof, recovery, and permission loss
before/during/just before activation with pending/cache preservation.
Existing USB/update/rollback, frame-order, HUD and PV5 preservation suites must
also pass. PC fault injection does not measure Orin/USB timing or physical safety.
