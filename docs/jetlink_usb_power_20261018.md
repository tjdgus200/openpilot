# Jetlink USB-C input-current guard

Port of [zoompilot/jetlink 491cf1ef](https://github.com/zoompilot/jetlink/commit/491cf1ef8d6786ed51cf995e18fc8721389066c6)
onto `codex/jetson-nav-runtime-port`, based on `c18ae58b`.

Upstream reports a comma 3X UVLO/brownout when its charger draws from a connected
laptop. Its fix forces the charger's `USB_ICL` input-current voter to zero while
Jetlink is enabled. A matching cause on this owner's comma 3 has not been
established: there is no device connected for voter or PMIC-log verification.

Carrot's `jetlinkd` always runs on TICI. The port therefore applies the vote in
the outer connection loop before checking for a host, independently of usb,
android, ios or auto transport selection. It retains the vote across disconnect,
transport selection, model loading and daemon shutdown. No privileged subprocess
or sysfs write runs in the inference frame loop. After success, the guard is
inactive for the rest of that daemon lifetime. A helper error is logged once;
the existing connection policy continues without a claimed successful apply.

The root helper requires AGNOS and the policy-engine/voter files. It checks
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
are not relaxed or counted as passes. No physical C3/C3X power, replug, UVLO
recurrence or vehicle validation has been performed.

The focused power/daemon subset passes all 55 tests. Ruff on the new Python
files, Bash syntax validation, and `git diff --check` pass.
