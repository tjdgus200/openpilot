## Parked vehicle automatic access (2026-11-18 candidate)

The owner selected automatic access while the real PV5 remains in Park, with
raw and filtered speed exactly zero and all cruise/control/selfdrive states OFF.
The new `parked_vehicle_capped_v1` profile admits **only** `parked_vehicle`;
it cannot reuse the home qualification, and does not enable detached-home access.
The existing home-only uncapped profile stays separate. Mixed profiles refuse.
No vehicle profile is qualified merely by installing these files.

The new profile keeps source freshness, two-second admission, resource headroom,
CAN identity/validity, emergency/disable rejection, one-second PID1 watchdog,
private owner key and cgroup/host-bus isolation. Missing or non-boolean control
fields refuse. Losing any gate closes all owned remote children; PID1 retries
admission. Qualified operation has no 900-second expiry. A first local test is
still bounded to 900 seconds until real key/isolation/cancellation checks pass.

`--install-park-trial` first refuses kernels without existing CPU and memory
controllers, before changing keys or files. The unit requests CPUQuota10%,
MemoryMax256MiB and TasksMax64. Before any network child starts, the guard reads
actual kernel limits, including the process count. The memory and process
controllers must be available in cgroup2. An existing v1 CPU mount is accepted
only if it places this exact service in the same group with an enforced quota.
The installer leaves a separately enrolled home profile in place.
No hierarchy mounting, host-controller changes or removal of limits is attempted.

`connect.py --verify-park` must verify actual Park data, owner-key access,
namespace isolation, the exact new guard, actual kernel caps, owned job closure
and rejection of a new connection. Its separate, host/code/time-bound
`park-qualification.json` is used by `/v1/remote/park-qualification`.
Local `--enable-park-auto` additionally audits recorded PID/start-time exits,
rechecks actual eligibility and the exact boot hook before removing trial expiry.
After registration, enable Automatic Park SSH in CarrotWeb Settings → PV5 →
Incident Reports. The preference defaults to OFF. OFF withdraws permission from
the existing guard, which closes its own sessions. Registration and the resource,
vehicle-state and owner-key checks still govern admission.
`--park-check` and the client resource-policy selector retain actual Park proof.
The boot supervisor accepts only the exact qualified home or park profile;
it checks the matching unit including vehicle caps. It does not convert modes.

PC-only tests do not qualify the actual comma. Installation, new host enrollment,
real CPU controller support, real cancellation and next-boot reconnect still need
actual device evidence. No old host pin or qualification should be overwritten.

---

# Conditional owner-PC remote access

The owner's 2026-11-15 instruction replaces the earlier manual-only/15-minute
remote policy for this newly qualified service. Existing coded incident sending
is unchanged: normal mode at fresh actual zero speed sends coded incidents only.
It does not authorize automatic SSH or bulk video/log processing.

Full auxiliary access requires either physically detached home state or a
separately qualified parked PV5 passive/dashcam state. Dashcam by itself does not
prove a stop. The vehicle branch retains Park, valid CAN actual zero speed,
standstill, cruise OFF, lateral/longitudinal/selfdrive OFF, no AEB/critical/disable
event, fresh source timestamps and resource headroom. Home requires disconnected
harness/no ignition and live pandad/hardwared producers. Model, control-process
or recorder liveness alone does not block physically detached home access.
When deviceState.started is true, at least one real, known Panda must explicitly
report a disconnected harness and no ignition; empty/unknown boards cannot
qualify this new started-home path. Vehicle gates and separate qualification
are unchanged (owner instruction 2026-11-17).
There is a two-second continuous admission period. Loss of admission terminates
the dedicated service's network daemons and remote jobs; the low-priority gate
can automatically reopen after eligibility returns. Normal operation has no
15-minute expiry. No periodic AI/chat polling is introduced.

## Authentication and isolation

Use the existing verified Tailscale userspace daemon/node enrollment, plus a
separate OpenSSH server listening only on loopback 2223. Private Tailscale Serve
TCP 2222 forwards to that loopback server. No Funnel/public SSH listener, routes,
TUN, global sshd configuration, account/password lock or tailnet-wide policy is
changed. The dedicated owner-PC Ed25519 private key remains in its owner/SYSTEM
directory; the host private key remains root mode0600 on the comma. A bootstrap
using the existing incident HMAC over verified HTTPS privately supplies the
client public key and pins the host public key. A changed host is rejected;
unknown hosts are never auto-accepted by the SSH client. No key goes in Git,
Discord, process output or shell history. Persistent keys avoid browser check
prompts on ordinary SSH connections; node revocation/expiry can still require
Tailscale account reauthorization. Account/MFA/other-device rules are preserved.

The old Tailscale SSH `RunSSH` preference is saved as a boolean before disabling
that path in the owned node, so the new service does not expose the old
browser-check server. Tools that assumed port22 must use the new connector.
No legacy manual service is stopped or commandeered during installation.

The device's `/data` is owned by UID1000 even when mode0755. Root SSH strict
key-path validation rejects an authorized-key file beneath that ancestor;
mode0600 on the leaf does not repair the parent ownership. Keep the persistent
public key in the protected data store, validate its exact restricted format
and <=512byte size, then copy it once per admitted session into the root-owned
`/run/pv5-conditional-remote/authorized_keys`. The SSH server reads only that
runtime copy. Keep `StrictModes yes`, PAM, the root account lock, passwords,
host private key and `/data` ownership unchanged. This was reproduced with a
UID1000-owned0755 parent and locked-root PAM fixture in the native PC SSH test;
it does not replace actual device authentication/resource/cancellation checks.

Only the registered PC key authenticates to this separate root diagnostic
server. Password/keyboard-interactive auth, agent/TCP/X11 forwarding, user rc
and tunnels are disabled. PAM account policy is retained. A prior real-device
finding showed some login/sudo workers could leave their guard cgroup, so the
new service makes cgroups read-only and hides host systemd/D-Bus sockets from
its namespace. This prevents ordinary PAM sessions from moving work into a
host login scope; remote systemd service creation is intentionally unavailable.
Root ownership is not a security boundary against an intentionally malicious
owner root command. Do not change the guard, escape isolation or run vehicle
service/parameter/model operations as a side effect of incident analysis.

The owner explicitly removed home resource limits on2026-11-15 after actual
device verification found that the service's cgroup2 exposes only memory and
cannot enforce the previously requested CPU10% quota. This does not establish
that the kernel lacks a CPU controller: enabled v1 CPU entries were also found.
Do not change global cgroup hierarchies or kernel configuration to force a quota.

The new `detached_home_uncapped_v1` policy removes this diagnostic service's
CPU quota, memory cap and first-four-core affinity. Home admission no longer
depends on numerical CPU/RAM/free-space headroom. Nice19 and idle-I/O priority,
thermal-green admission, fresh real disconnected/no-ignition
proof and all authentication/isolation/watchdog conditions remain. No SCHED_IDLE
policy or hard10% cap is claimed. Existing OS thermal/memory protections remain;
uncapped diagnostics can consume the device's available resources.

Guard, boot restoration and promotion reject missing/legacy/mixed/vehicle modes
under this policy. This is a home-only exception; it does not qualify or enable
parked-vehicle remote access, incident evidence work or recording uploads. The
existing vehicle eligibility helper retains its original numerical headroom
checks. Extending an uncapped service to that mode is expressly rejected.

The main-only PID1 watchdog is one second; the observer waits500ms while closed
and100ms while serving. Source delay, reconnection or watchdog loss fails closed.
These checks do not guarantee real-time or zero-impact execution. Already
submitted kernel I/O cannot be recalled instantly.

## One-time local setup and qualification

`conditional_remote.py` is a standalone local CarrotWeb-terminal installer;
no credential-bearing download or interactive browser login is required. It
requires the enrolled binary hashes and device enrollment, existing OpenSSH,
native full cereal/messaging and current detached home proof. It does not
download/install a package, alter recording settings or reboot the device.
The first `--install-trial` is bounded to900seconds, has no boot enable and
qualifies only the home mode for testing. The local print is a preparation
result, not proof of actual PC SSH/resource/cancellation success.

After the owner starts that local trial, `connect.py --policy ... --verify-home`
uses the private PC key and pinned host to check the actual cgroup, read-only
cgroup mount, blocked host control sockets, explicit home-only uncapped policy,
absence of leftover service CPU/memory caps and a short memory sample. Missing
CPU quota files are expected in this owner-approved policy; they are not reported
as a successful hard-cap test. It starts only its own sleep job/child and asks
the trial guard to close; the normal transport must become unavailable. No
CAN, fake incident, control action, video inference or raw-log export is used.
The PC attestation is fresh for30minutes and tied to the host/code/resource-policy
identity. Old capped-policy receipts cannot authorize the new policy.

AGNOS keeps `/etc/systemd/system` read-only. The installer stores the reviewed
unit under root-owned `/data/pv5-remote-access` and links it only into writable
`/run/systemd/system`. It never remounts the root filesystem or edits global
system units. A failed earlier `/etc` link can leave data files and an authenticated
PC host pin; these do not mean the trial service started.

The subsequent local `--enable-home-auto` independently audits the recorded
trial PID/start-time pairs are gone (or reaped zombies), rechecks detached home
state and the exact trial unit, then removes the trial expiry and records
qualified automatic startup/reopening. The updated `launch_chffrplus.sh` must
contain the exact optional restore hook before promotion. Its one bounded,
background, low-priority invocation closes the boot-lock descriptor, reads
the root-owned qualification, recreates the volatile runtime link and asks PID1
to start the guard without waiting. Trial/unqualified/changed-code/runtime or
foreign-service states refuse; already running owned services are retained.
Default installations without these files launch no helper. Networking still
requires the guard's real fresh state and two-second admission period. No
`systemctl enable` write to `/etc` is used. This path has been checked against
PC read-only mount namespaces and simulated runtime-link loss, not a device reboot.
Promotion cannot be run through the SSH service being
reconfigured. A parked-vehicle dashcam mode must be measured separately before
adding that mode; home testing cannot qualify its CAN/control gates.

Startup keeps the foreign-daemon scan before continuous admission and rechecks
real sources afterward. Slow preparation must not consume the already-active
250ms contract. The bridge imports HTTP enrollment dependencies only when
enrollment is used; its owned daemon's asynchronous backend readiness is polled
at most once per second for15seconds with the original permission checks. A
non-running backend is not automatically called an authentication failure.
Only the bounded first trial emits one sanitized exit reason/code/gap when the
guard closes. No added exit diagnostic is emitted in permanent operation.
The PC checks include a220ms assumed scan that fails the earlier ordering,
passes the new ordering and preserves all original freshness/permission limits.
This is not a measurement of the actual device's exit cause or startup load.

Bulk upload is separately qualified and remains trusted nonmetered Wi-Fi only,
without audio/cabin media. Remote-key enrollment does not enable bulk recording
upload or imply the earlier upload work is finished.

`test_remote.py` checks policy/fault/reentry/expiry and HMAC/pin/replay rules.
`test_source.py` uses real full-schema messages and in-memory subscribers.
`native_check.py` uses a PC-only temporary loopback OpenSSH server, fake
namespace-local passwd entries, real PAM, PID1 resource/mount isolation and its
own sleep descendants. The extracted PC test binary is not a vehicle package
or a globally installed/running SSH service. PC timing is not comma timing.

References: [SSH over Tailscale](https://tailscale.com/docs/reference/ssh-over-tailscale),
[private Serve TCP and restart behavior](https://tailscale.com/docs/reference/tailscale-cli/serve),
[OpenSSH configuration](https://man.openbsd.org/sshd_config).

## One-shot local diagnosis

Run `python3 -B tools/pv5_remote/diagnose.py` as the normal terminal user.
It reads sources for three seconds and reports only allowed state fields.
A bounded `sudo -n` child reads protected qualification metadata and the local
Tailscale backend, without opening IPC readers as root. There are no service
start/stop actions, config writes, key output or raw journal/boot log output.
The installed guard's eligibility function supplies the gate reason. A
transient startup stale reason is separate from the final sampled reason;
this one-shot observation does not prove two-second continuous admission.
A missing/unreadable source remains an explicit diagnostic failure.

Updating this repository does not replace the separately installed protected
guard. Its code identity and boot attestation must remain matched; never
rewrite the stored digest alone or bypass qualification to activate a change.

## Automatic boot recovery (2026-11-17)

The V2 launcher starts a detached low-priority supervisor before AGNOS/Params
bootstrap. It closes the boot-lock descriptor and all terminal input/output;
model/control startup never waits for it. Each restore attempt has a 25-second
limit, replacing the old single 3-second suppressed attempt. Transient failures
retry with 2/4/8/16/32/60-second delays (60-second cap). Only the attempt's own
process group is cleaned up on timeout. Existing guard sessions are not stopped.

The supervisor requires the existing root-owned home-only code-bound
qualification on every attempt. Disabled/trial/removed enrollment exits; code,
interpreter, foreign-unit and other identity rejections remain blocked. It never
rewrites qualification, keys or enrollment. The installed guard still controls
all network admission and its normal PID1 watchdog/restart behavior. The boot
supervisor exits only after the unit reports active, not just after a queued
start request. This is service availability, not proof that the gate or SSH is
currently admitted. One private overwritten boot_restore_status.json records
the latest safe reason and attempt count, without raw subprocess output. A
single lock prevents duplicate supervisors. No periodic AI invocation is added.

## Release and device synchronization

Owner policy (2026-11-17): validate changes, commit and push the reviewed
release, then apply that published commit with a fast-forward-only pull on the
device. Verify the GitHub ref, device HEAD and changed-file hashes afterward.
Do not deploy unpublished working-tree files directly. Preserve unrelated
local edits and private configuration; never use a blanket reset/clean to
make an update pass. Reconcile any earlier direct deployment against its
verified backup and the published content before pulling.

The protected installed guard and qualified PC connector are separately pinned
artifacts. A repository pull is not proof that those running copies changed.
Report their installed identity and pending qualification separately; never
rewrite an attestation hash merely to make versions appear equal.

### Enrolled Python at boot

The boot supervisor selects the absolute Python executable from the protected, already-qualified service unit, validates the complete unit against the enrolled guard template and verifies the executable metadata. It does not depend on sudo PATH or migrate qualification. Changed guard hashes, unit arguments and untrusted executables remain blocked. Existing active sessions are not restarted.

## PC connection continuity

`python -m tools.pv5_remote.client --policy <policy>` checks the existing enrolled service with bounded retries of that read-only check. `client.command(policy)` supplies the authenticated SSH arguments for future owner operations: three initial TCP connection attempts and encrypted keepalives every 5 seconds, allowing six missed replies. Authentication and pinned host-key failures are not retried by the checker. Arbitrary commands are never replayed after a disconnect. The server's freshness, state gate, permission pipe, cgroup and watchdog are unchanged.

The PC client release is recorded separately as `conditional_remote.client_code_root` / `client_source_commit`; `conditional_remote.code_root` remains the enrolled guard's verification pin. A resilient transport is not permission for access outside the enrolled modes, and no network session can survive power loss. Boot recovery and the guard reopen availability automatically when their conditions return.
