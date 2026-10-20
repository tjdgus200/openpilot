# Jetlink USB-C input-current guard

Port of [zoompilot/jetlink 491cf1ef](https://github.com/zoompilot/jetlink/commit/491cf1ef8d6786ed51cf995e18fc8721389066c6)
onto `codex/jetson-nav-runtime-port`, based on `c18ae58b`.

Upstream reports a comma 3X UVLO/brownout when its charger draws from a connected
laptop. Its fix forces the charger's `USB_ICL` input-current voter to zero while
Jetlink is enabled. A matching cause on this owner's comma 3 has not been
established. No device was connected during the initial port; the later live
probe and rollback are recorded below.

Carrot's `jetlinkd` always runs on TICI. The port therefore applies the vote in
the outer connection loop before checking for a host, independently of usb,
android, ios or auto transport selection. It retains the vote across disconnect,
transport selection, model loading and daemon shutdown. No privileged subprocess
or sysfs write runs in the inference frame loop. After success, the guard is
inactive for the rest of that daemon lifetime. A helper error is logged once;
the existing connection policy continues without a claimed successful apply.

The root helper requires AGNOS and the policy-engine/voter files. By default it checks
`current_pr == none` before changing the voter, and checks again immediately
before the writes. If already attached or unreadable, it defers without cutting
the potential sole power feed. This does not make physical cable insertion and
sysfs writes atomic. The implementation retains upstream's empty-port safety
assumption. It does not infer independent power from a USB host's presence.

An existing nonzero forced vote is preserved and reported, not overwritten.
A zero vote already owned by another component is accepted without claiming it.
Only this helper's immediately previous inactive voter value is recorded in
root-only tmpfs. Explicit `on` restores that value only while the port is empty
and the current forced vote still matches ours. It is never called by daemon
cleanup. A reboot restores the kernel's original policy and clears tmpfs.

## Apply on the comma

This is a comma-side code update. Jetson firmware, runtime, model artifacts and
signed update manifests are unchanged; the Jetson requires no update for it.

After updating and while parked/offroad, power the comma through its independent
normal supply with the comma-to-Jetson USB-C cable disconnected. Start/restart
the comma software and wait for:

`Jetlink USB input current held at zero; independent power remains required`

Then reconnect USB-C to Jetson. If the comma boots with USB-C already connected,
the guard deliberately defers. Merely updating files with the cable attached
does not establish that the new current limit is active. Retain independent
power whenever using the guarded link. Switching back to the official branch
and rebooting clears this per-boot vote.

With shell access, read-only inspection is:

```sh
sudo -n bash tools/jetlink/usb_power.sh status
```

Applied state is `force_active=1 force_val=0`. A fixed input vote is not proof
that all undervoltage sources are solved; the main supply, harness and physical
connectors remain possible causes of a power-off.

## PC validation

Fake-sysfs tests cover initial empty-port apply, replug, explicit safe restore,
USB-powered/unknown-port deferral, foreign forced limits, missing interfaces,
one-shot helper use and failure reporting. The connection-loop placement is
checked, and existing daemon/USB/auto/mobile regression tests are compared with
the unchanged `c18ae58b` baseline. No test changes host power or a real voter.

On the workstation checkout: 180 pass, 5 fail; baseline: 168 pass, the same 5
fail. The additional 12 power tests pass. Existing failures are an official-
bootstrap fixture incompatible with this branch's USB-signed release manifest,
three shell checks affected by Windows CRLF, and a DrvFS chmod assertion. They
are not relaxed or counted as passes. At this initial PC-validation stage, no
physical C3/C3X power, replug, UVLO recurrence or vehicle validation was performed.

The focused power/daemon subset passes all 55 tests. Ruff on the new Python
files, Bash syntax validation, and `git diff --check` pass.

## Explicit independent-power installation (2026-10-20)

The owner requires the main vehicle/OBD-C connection to supply power at all times,
and the Jetson auxiliary USB connection to carry data without supplying operating
current. Inspection found `force_active=0` with the auxiliary port already a sink:
the original empty-port guard had deferred, so a zero `force_val` alone did not
mean that the current limit was active. This is distinct from proving the cause
of any particular reset or transient supply drop.

For an installation that **always retains a separate main supply**, the owner can
explicitly enroll this policy once:

```sh
cd /data/openpilot
sudo -n bash tools/jetlink/usb_power.sh independent
sudo -n bash tools/jetlink/usb_power.sh status
```

`independent` records a root-owned, non-writable-by-others confirmation under
`/data/carrot-jetlink-power/`, then applies the same USB_ICL zero-current vote.
After enrollment, the daemon can apply it even if the USB cable is already
connected at startup. The root helper revalidates the confirmation and preserves
other components' nonzero forced limits. The policy survives reboot; the vote is
still per-boot and is reapplied by this branch's daemon. Without enrollment, the
original empty-port-only behavior remains. Do not enroll a USB-only-powered
installation: blocking its only power feed can turn the device off.

The setting targets the charger's USB input-current limit. It does not force a
USB data role, turn off the Jetson's port regulator, cut ground/CC/data wires or
guarantee galvanic isolation/zero leakage. Normal USB enumeration must remain
available. It cannot repair an inadequate main supply or cable. The host/model
update manifests and Jetson runtime do not change for this comma-side feature.

Explicit `on` still requires an empty auxiliary port. It restores only this
helper's immediately previous vote and removes the owner's confirmation; it does
not release a foreign forced vote. Daemon shutdown and transport changes never
call it. A different branch may not honor the enrollment; reboot clears the
current kernel vote, so inspect that branch's behavior separately.

Fake-sysfs validation: **111 passed**, including attached enrollment, persistent
confirmation/reapply, explicit restore, foreign-limit preservation and rejected
symlink/writable/malformed policy. Three existing pytest configuration warnings
remain. These tests change no physical power input; device verification must be
reported separately from the PC checks.

### Device follow-up and rollback

The inspected device identifies as `comma tizi` (3X). Its vehicle CAN/main-power
connector and auxiliary Jetson USB-C connector are distinct. Upstream's linked
`draw` policy targets the latter's charger input; this change does not write CAN
controls or turn off the main connector. It controls input current, not physical
VBUS disconnection, and does not qualify board isolation.

The initial live state was `force_active=0 force_val=0`, USB power role sink and
data role UFP. A temporary probe, armed with automatic rollback before the write,
reached `force_active=1 force_val=0`; 40 actual N16 USB requests returned finite
outputs with UFP maintained. Main-rail monitor current was about38mA before and
437mA afterward. This demonstrates a working main supply/data link in the short
probe, not measured auxiliary VBUS current or full-load power qualification.
The root helper accepted the persisted policy while attached. Reapplication by
a fresh daemon or after reboot was not physically verified.

Real-camera warm-up subsequently restarted the device before any usable HUD/ONNX
comparison phase. The owner observed the restart and currently has only a tabletop
5V adapter available. Restarts also occurred before enrollment; neither the block
nor a particular supply/cable fault is established as their sole cause. Five volts
alone does not establish insufficient power: adapter current, cabling and the
actual board input path remain relevant.

The live enrollment was **removed**, and only this task's owned vote was restored
to `force_active=0 force_val=0`. Original HUD settings, normal Jetson inference/HUD
services and offroad state were restored. The public opt-in code remains, but this
device is **not currently enrolled**. No full-load/vehicle qualification or
successful HUD/ONNX latency comparison is claimed. The earlier40-request probe
does not qualify the failed warm-up.
