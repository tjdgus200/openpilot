# Phone-independent Jetson HUD Wi-Fi

The owner branch keeps comma's optional Internet station on `wlan0` and creates
an independently addressed AP on `wljetson0`. Jetson joins that AP. HUD snapshots
and optional navigation media use authenticated encrypted Wi-Fi; driving-model
inference retains the existing USB path. No inference protocol or validity limit
changes.

`wifi_direct.py` runs at SCHED_OTHER/nice19 on available little cores. It creates
or reactivates an AP only with explicit offroad Params, checks advertised
station/AP concurrency, avoids overlapping IPv4 subnets, and never reconfigures
`wlan0`. NetworkManager handles WPA2, DHCP and optional Internet sharing. One
root-only identity file retains a generated password across restarts; credentials
are never added to Params, HUD, diagnostic status or this repository.

The private USB provisioner exports only the explicitly owned, active AP as
one client profile. It does not read or forward the phone/home network secrets.
An inactive AP exports no client profile. Once the direct profile is installed,
the existing transactional importer removes its previously imported fallback
profiles; manually configured networks remain untouched. Each recreated AP
interface/boot gets a distinct client-profile UUID with the retained SSID/key,
so the legacy importer retries the direct connection after an AP restart.

Peer Wi-Fi data uses **only the direct AP**. The sender requires `wljetson0`,
one of the configured direct subnets, its `.1` gateway address, a same-subnet
Jetson address and an on-link route with no next-hop gateway. It never selects
`wlan0`, a phone/router address or a USB interface. The receiver binds only a
direct-subnet WLAN address, accepts only that subnet's comma gateway, and still
requires the existing authenticated encryption. A missing direct link waits for
reconnection; vehicle data expires at its existing age limit. Phone association
changes do not authorize a fallback. The main driving-model USB path is separate.

Only the owned AP's power-save flag is disabled while it is serving Jetson;
the Internet station's power policy is unchanged. The original AP value is
restored before teardown. Background UI discovery is paused while this AP is
active and the Internet station is disconnected; explicit network-page scans
remain available. The web network viewer reads the station cache instead of
requesting scans on both virtual interfaces. NetworkManager autoconnect is not
disabled. The settings UI also prefers `wlan0` over the private AP device.

The AP supervisor survives model-session reconnects and exits when this helper
is absent from the installed branch. It takes down only the AP/profile it owns;
ordinary phone/home Wi-Fi profiles are preserved. Returning to official software
therefore restores its existing network behavior. A root-only AP identity may
remain dormant for a later reinstall of this owner branch.

## Validation and limits

- PC: route rejection for USB/wrong devices, phone-loss link identity, explicit
  AP provisioning, concurrency limits, onroad activation guard, subnet conflicts
  and cleanup after rejected activation are covered with focused tests.
- Device preflight: this C3-family driver advertises concurrent station/AP
  combinations. A bounded AP trial maintained `wlan0` Internet association and
  the USB model link's `ready` state while `wljetson0` was active.
- Focused PC regression: **66 passed**, covering Wi-Fi, the direct path, UI/web
  discovery, AP power restoration, HUD contracts and USB power policy. Three inherited pytest configuration
  warnings are not device failures. This is not a full openpilot safety suite.
- Jetson joined the comma AP through its existing protected network importer.
  Its signed model runtime/firmware and the immutable recovery worker were not
  replaced. The direct AP controller also survived a USB-owner restart.
- Final installed-source 35-second station disconnect produced 35 one-second
  observations with the phone link down and AP/HUD/USB ready, transferring about
  **2.25 MB** of HUD payload. Both original HUD TCP connections survived the
  disconnect and restoration; this is stronger than checking cached ready flags.
  The phone connection was automatically restored. This disconnects comma's
  station, rather than physically switching off the phone's radio.
- Final-source 30-second observation: all 30 observations retained direct HUD,
  both HUD channels, AP active and USB ready. Controller CPU was **0.133% of one
  core**, publisher 0.133%, existing HUD sender 7.767%. These per-process figures
  exclude driver/IRQ and NetworkManager work, and do not prove zero inference
  impact. The controller was nice19 on CPUs0–3, about13 MiB RSS. Readback confirmed
  AP power-save=0 and Internet station power-save=1.
- A normal comma reboot automatically recovered the AP and USB model link.
  Jetson then rejoined the AP using the new client-profile identity. No protected
  Jetson OS update was used. This did not test booting with the phone radio absent.

Initial tests could show continuous ready flags despite later TCP reconnects.
Repeated station-loss trials with AP power-save enabled reproduced multi-second
radio stalls while addresses and fresh USB telemetry remained valid. Removing
UI/web background scan requests alone did **not** resolve that stall. A bounded
both-interface awake trial passed; the narrower AP-only trial also passed,
leaving `wlan0` unchanged. The final code uses only that narrower policy. These
observations support the mitigation on this device, not a universal firmware
root-cause claim.

| Stationary ICMP RTT sample | Mean | P95 | Maximum | Replies |
| --- | ---: | ---: | ---: | ---: |
| Earlier phone-relayed path, 56-byte payload | 29.673 ms | 93.050 ms | 267.000 ms | 100/100 |
| Initial direct AP, 56-byte payload | 5.617 ms | 27.020 ms | 62.300 ms | 99/100 |
| Initial direct AP, 1400-byte payload | 5.448 ms | 24.760 ms | 67.000 ms | 100/100 |
| Direct AP before awake policy, phone present | 4.617 ms | 14.210 ms | 46.700 ms | 299/300 |
| Station-loss trial, scans quiet but AP power-save enabled | — | — | 14903 ms | 280/300 |
| Bounded AP-only awake trial including station loss | 5.544 ms | 18.530 ms | 84.200 ms | 300/300 |
| Final installed code including station loss | **6.703 ms** | **33.420 ms** | **110.000 ms** | **300/300** |

These are sequential short desk observations, not controlled simultaneous A/B
trials or one-way/camera-to-HUD latency. Earlier trials had ICMP loss and long
stalls; the final short pass does not make Wi-Fi lossless or deterministic. HUD retains its
authenticated TCP transport and existing stale-data limits. No camera, forced
onroad or cruise-control test was run; `USB ready` does not establish loaded
20 Hz inference or driving safety.

Driver support is conditional on firmware/regulatory/channel and beacon-interval
constraints. AP startup failure leaves the ordinary saved Wi-Fi provisioner and
USB model transport available; it does not justify retrying network activation
onroad. Short stationary tests cannot establish driving safety or universal
hotspot compatibility.

## Reported panel flicker remains unverified

After these connectivity trials the owner reported the physical HUD repeatedly
appearing/disappearing. The phone/ADB path was already disconnected when the
report was investigated, so there is no synchronized panel-service, USB or
brightness trace for that incident. Connected TCP streams and sent-byte counters
do not prove continuously visible frames on the panel.

A PC branch reproduction of the unchanged installed Jetson source `4c243306`
found a plausible automatic-brightness mechanism. With setting 0 (automatic),
fresh offroad `deviceState` and no valid camera exposure yield brightness 0.
If the snapshot becomes unavailable, `RemoteSubMaster` invalidates its services,
`screen_brightness_percent()` returns unavailable, and `resolved_usb_brightness()`
uses its default 80. Fresh offroad data then returns it to 0. A valid camera
exposure can similarly select a nonzero brightness before disappearing. A manual
65 setting remains 65 for all these synthetic availability transitions. The
reproduction executed the exact source method bodies with synthetic service
availability; it did not reproduce the owner's actual screen timing or establish
the incident's root cause.

Next connection should correlate fresh HUD data and brightness inputs with the
renderer PID/restart count, output heartbeat, and USB disconnect/error events.
Black/visible alternation, a blue boot-screen takeover, and partial-image flicker
must be distinguished. No speculative renderer change, stale-data-limit increase,
reboot or brightness override was installed after disconnection. The retained
automatic brightness setting is unchanged. This feature's short network pass
must not be reported as resolution of the physical panel flicker.

## Brightness fallback mitigation

The owner subsequently requested a countermeasure for this demonstrated input-
loss path. `hud.py` now wraps the common brightness resolver only inside the
Jetson renderer. Once a brightness is resolved, missing automatic input retains
that value, including zero/off. A manual change updates the retained value too,
so switching back to automatic during a gap does not resurrect an older automatic
value. Fresh camera/device/offroad values still use the common resolver and can
explicitly turn the panel off. A new renderer retains the original startup
fallback until input exists; no value is persisted to vehicle Params or disk.

The wrapper samples its input once and adds no thread, timer, network request or
file read. HUD packet expiry, `RemoteSubMaster` alive/valid flags, and vehicle
standby behavior are unchanged. Holding screen illumination must not make stale
vehicle data current or affect inference, controls or a comma-connected panel.
PC regression passed **69 tests**, including the common live-source behavior,
HUD/health/hotplug checks, and new missing-input cases. The new tests cover
zero/off preservation, repeated loss, recovery, manual-to-auto changes,
per-renderer state, and actual 0.5-second HUD packet expiry using cereal messages.
Six additional Windows standard-library source-branch checks also passed; those
isolated checks do not exercise native cereal decoding or graphics. No new
inference benchmark or physical-panel pass is implied.

This is a source-level mitigation for missing-input brightness fallback, not a
confirmed fix for the owner's physical panel incident. Installation requires an
updated Jetson package: changing comma's source alone cannot replace the running
Jetson renderer. The reconnect and bounded receive-gap results below supersede the initial
disconnected-device observation; physical flicker attribution remains open.


### Reconnected-device check and signed deployment (2026-10-19)

The comma was confirmed offroad (`IsOffroad=1`, `IsOnroad=0`) before device
changes. A 30-second baseline collected 60 samples: no expired snapshots,
HUD restarts or classified USB/service errors were observed. Fresh deviceState
reported started=false and screenBrightnessPercent=0 throughout. That baseline
did not reproduce the owner's flicker. Setting 0 means automatic brightness;
its resolved offroad output of zero intentionally turns the panel off.

Published owner-signed source `ad7c0961` and selected it with comma commit
`41075673`. The existing USB boot updater verified and activated the package,
including its isolated model execution check. Inference and HUD returned active,
and comma reported ready with USB model transport and connected Wi-Fi HUD.
The enrolled public key, model identity, runtime ABI and inference implementation
are unchanged. The package also contains the already published direct-Wi-Fi
helpers; comparison with the previous installed bundle found changes confined
to those helpers, the brightness wrapper, its regression and SOURCE_COMMIT.
Expanded native-PC regression passed **112 tests**, with no failures or skips;
the exported HUD entrypoint check passed separately.

A 20-second native Jetson diagnostic suspended only the HUD Wi-Fi receive worker
for two seconds, with independent automatic resume and a finally resume. It
compared the original and new resolvers against the real received HUD data,
without injecting vehicle state or reading the Wi-Fi credential packet. All
18 expired 100ms samples invalidated deviceState alive/valid flags. For these
samples the original resolver calculated 80 while the new resolver retained
zero. Fresh data recovered; the receiver resumed and the actual HUD process
kept PID 35900 with no restart. This sampled resolver comparison did not
instrument actual USB brightness commands or measure physical panel luminance.
The owner subsequently observed a continuously off panel, consistent with the
fresh offroad automatic input, rather than a visible-HUD continuity test.

The previous runtime is retained by the existing single previous-release record.
No firmware, vehicle control, forced-onroad setting, or brightness override was
installed for this verification. These bounded stationary checks establish the
missing-input brightness policy, not the original flicker cause, visible
onroad stability or loaded 20Hz performance.


### Owner-requested offroad visibility (2026-10-19)

This subsequent policy supersedes the zero/off preservation described above.
The Jetson HUD now remains visible offroad, including when the forwarded
ClusterHudDebug mode is zero. A renderer-local gate maps that mode to 1 while
preserving modes 1–3. It does not write Params, fake IsOnroad/deviceState, start
vehicle processes or alter the comma's own HUD output gate.

Automatic brightness has a 10% minimum, including fresh offroad zero. Missing
input holds the last resolved output at no less than 10%, and first-input startup
uses 10% instead of the common default80. Manual positive settings, including
values below10, remain exact; explicit manual zero still turns the panel off.
Service shutdown or removal of panel power also remains effective. No render
rate, inference policy, packet freshness or driving control changes are added.

Vehicle service validity still expires on reception loss; only the display stays
lit. Tests cover both brightness transitions and the actual common offroad gate
reader with unchanged vehicle Params. Deployment and native-device results for
this policy are recorded after the signed package is applied.


## Direct-only policy follow-up (2026-10-19)

The previous version preferred the AP but retained phone profiles/routes as
fallbacks. The owner now explicitly requires direct-only peer data. The new
policy removes both fallbacks, preserves the original authenticated transport
and checks the receiving address and source gateway as well as the sending
interface. It does not promise uninterrupted radio service: before this change,
Jetson NetworkManager recorded `supplicant-timeout` and `ssid-not-found` losses,
while the comma AP supervisor still reported active. A route policy cannot by
itself prove that the underlying radio problem is fixed. PC checks cover phone
and next-hop rejection, isolated-subnet selection, receiver admission and retained
phone-loss behavior. Device follow-up is recorded after deployment.


## Shared-radio channel correction (2026-10-19)

The actual comma cache returned `*:5745 MHz` (station channel149), but the old
parser accepted digits only and silently chose AP channel36. This was a concrete
configuration error during the reported disconnects. Parse both unit-bearing and
numeric frequencies. A malformed or unsupported active channel now refuses AP
activation rather than silently choosing a different channel. Without an active
station, initial AP setup may use36; losing a phone leaves an already active AP
on its current channel. Offroad supervision realigns an AP when a returning
station uses another supported channel. It does not restart an AP onroad.
Changing the Internet station's band/channel while driving remains unvalidated.

An isolated125s Jetson-station awake trial retained radio connection at118/120
samples but HUD at only30/120, with about1.88MB payload. Its original power-save
setting was restored. This did not resolve the fault and is not made permanent.
The channel parser/recovery correction passed153 focused PC checks. Actual
same-channel connectivity is recorded after installation; the configuration
error is proven, its complete attribution of every radio loss is not.

## Owner-requested Wi-Fi awake policy (2026-10-19)

The owner subsequently moved comma and Orin directly beside each other and
reported the connection recovered. The owner separately requests Wi-Fi power
saving disabled on both devices, superseding the temporary trial's restoration
policy above. Before applying this policy, comma AP power saving was off, but
comma's station and Orin's station were on. Set NetworkManager powersave=2 on
saved Wi-Fi profiles and in the new-connection default, and immediately disable
kernel power saving on station/AP interfaces. P2P discovery interfaces are not
connected data links and are excluded.

The direct AP profile and future imported Jetson profiles explicitly disable
power saving. Comma's existing low-priority AP supervisor keeps the Internet
station awake too, without reconnecting it. The selected signed Jetson runtime
reapplies the volatile default at startup on the protected image. This adds no
per-frame work or persistent polling thread. An unavailable startup policy logs
a concise error rather than preventing model startup. NetworkManager is only
asked to reload configuration, not restart or reactivate links. Persistent
profile writes are skipped when already disabled. Radio power consumption may
increase; no measured wattage, latency reduction or driving stability guarantee
is inferred from the setting alone.

NetworkManager documents 2 as disable, 3 as enable, 1 as ignore and 0 as the global
default: https://networkmanager.dev/docs/api/latest/settings-802-11-wireless.html

## HUD handoff on first data (2026-10-19)

The always-visible renderer adapter was previously behind an older startup
condition requiring IsOnroad or ClusterHudDebug>=1. A fresh offroad snapshot with
debug0 therefore left the blue status screen running despite connected transport.
Remove that extra condition: after the unchanged signed-runtime readiness gate,
wait for the first read_snapshot() result and initialize the existing HUD. The
snapshot reader still rejects missing, expired (500ms) and future-dated input.
An IP address or an active USB service is not sufficient for handoff.

The existing panel ownership lock still releases boot diagnosis only when the
renderer has loaded and requests the USB panel. No global Params, ignition,
driving process, CAN control, frame rate or networking priority is changed. No
network restart or onroad AP-start policy change is part of this handoff fix.
Initialization is not zero-time, and data expiry/brightness-loss handling remain
unchanged. This fixes a demonstrated source-level startup gate; it does not
establish the cause of the owner's reported Wi-Fi retrying state or claim a
physical-panel pass on the disconnected device.

The handoff source `05c749539332e4220c336f6b74b3af20f5d6dd20` is published as
[`jetson-hud-autostart-20261019-05c74953`](https://github.com/tjdgus200/openpilot/releases/tag/jetson-hud-autostart-20261019-05c74953).
The owner-signed package is81,644,263bytes, SHA-256
`84c12dd2f25534545d79d833ae852d2bd624e6056e2776b2e1403cdb5d85c011`.
Compared with the previous93788f61 package, only hud.py, its hotplug regression
and SOURCE_COMMIT differ. The same owner public key verifies the selection.
PC164 focused checks passed; one unchanged isolated boot-display fixture was
excluded because WSL system Python lacks PIL. The exported entrypoint passed.
The branch selector prepares installation through the existing offroad USB
boot receiver on the next normal Jetson boot. No device installation or physical
screen confirmation was possible after the owner disconnected. The reported
Wi-Fi retrying/waiting condition remains separate and unconfirmed.

## Dashboard before network data (2026-10-19)

The owner subsequently requested immediate dashboard startup, superseding the
first-fresh-snapshot wait above. After the unchanged signed-runtime approval,
initialize the renderer and acquire the USB panel without waiting for network,
ignition, debug mode or a snapshot. The early status display remains available
for runtime/storage failures and before renderer initialization completes.

The Jetson-only renderer shows `--` instead of a default/stale measured speed
when carState is absent, not alive, invalid or missing its event. It also shows
a connection-waiting strip; existing vehicle alerts retain precedence. The
normal speed block returns when current vehicle data arrives. RemoteSubMaster
validity, snapshot expiry, model execution, network recovery and frame rate are
unchanged. Visibility is not an indication that the model or Wi-Fi is ready.

Immediate-dashboard source `5cad3319ab6164677e6fde5e1766c689d5843d5d` is published
as [`jetson-hud-immediate-20261019-5cad3319`](https://github.com/tjdgus200/openpilot/releases/tag/jetson-hud-immediate-20261019-5cad3319).
Bundle81,645,046bytes; SHA-256
`f7153c5a9872702ef5ed592917f3c0ca53582047294ff32225f642fdcdddf6a7`.
Only HUD startup/presentation, their regressions and source identity differ from
05c74953. PC169 focused checks passed, with the same unrelated isolated PIL
fixture excluded. Exported entrypoint and existing-key client selection passed.
The owner install branch selects this package for its existing offroad USB
boot receiver. Actual installation/display and Wi-Fi association verification
remain pending after device disconnection; no recovery jumper or key change is
needed on the previously prepared device.


## AP recovery correction (2026-10-19)

The installed712ff224 and then-current3f54355f control flow were reproduced
on PC with a synthetic power-query exception: the working AP was deleted and
the next onroad pass waited for offroad. This proves the code failure path,
not that it caused the reported vehicle incident.

Maintenance query/power errors now preserve the owned AP, address lease and
TCP sessions. Only a confirmed inactive AP/partial failed activation or a
validated offroad channel realignment can enter destructive recovery. Startup
power-default failures do not prevent AP control. AP preparation begins in the
normal-priority USB daemon before waiting for a Jetson HELLO; it does not wait
for NetworkManager and never delays driving startup. Check offroad again before
vdev creation and activation; genuine missing-AP recovery remains offroad-only.

A tracked versioned policy marker distinguishes compatible updates from return
to software without this direct-only feature. A changed controller defers
onroad; offroad it releases its lock/socket without deleting the AP. The next
controller validates the reserved interface, unique connection identity and AP
mode before adopting it without activation/deactivation. Removing/changing the
policy marker retires the owned AP; unrelated interfaces are never adopted.
The pre-fix controller cannot gain this handover retroactively: install the
first corrected version parked/offroad.

Empty publisher results retry after2s, AP-generation changes trigger refresh,
and AP control restarts are checked every5s. Steady credentials remain cached
for30s. Jetson processes a newly received owner/profile identity immediately
through an old retry deadline, but identical failed packets retain30s backoff.
The protected image's immutable worker is not silently assumed to update: the
signed runtime copies the four networking helpers to independent hash-versioned
RAM storage and installs a volatile service override. It restarts only that
importer, skips an outstanding profile transaction and retains the base recovery
worker for boot/failure. NetworkManager and active WLAN links are not restarted
by this migration. Model/USB inference, authentication and data expiry stay
unchanged.

Channel parse/unsupported-channel errors preserve an existing AP and receive
secret-free reason codes. The shared radio still needs a supported common
channel: cold startup on an unsupported/DFS phone channel is not made safe by
forcing an arbitrary different channel. No general DFS/6GHz or onroad roaming
support is claimed. The dashboard distinguishes importer and comma-AP reasons;
no nmcli output, SSID, credentials or vehicle payload is recorded.

Validation: focused PC regressions cover transient query failures, update
handover/adoption, official return, channel refusal, activation/road-transition
races, one-time immediate retry, repeated-failure backoff, RAM migration and
transaction protection. Devices are disconnected: actual installation, optical
HUD continuity and long-duration vehicle reconnection remain unverified.

Final PC result:279 passed, one unchanged isolated boot-display fixture
excluded because WSL system Python lacks PIL. Existing USB signing/update,
USB power, runtime-option and HUD regressions are included. Three inherited
pytest configuration warnings remain. Localized user-doc validation passed.


## Parked onroad USB update permission (2026-10-19)

Comma no longer equates the onroad screen with a moving vehicle for the
owner's USB boot-gate update. Offroad permission remains unchanged. Onroad
requires an explicitly idle bootstrap peer and fresh (<250ms), valid/alive
carState, selfdriveState and carControl: healthy CAN, Park, standstill,
abs(vEgo)<0.01m/s, disabled stock ACC, selfdrive enabled/active false, and
carControl enabled/latActive/longActive false. No messages/Params are forged.
Unknown or contradictory Params, missing data and runtime peers fail closed.
The receiver still requires a normal Jetson reboot, existing enrollment and
comma Internet for uncached files; no Jetson WLAN or receiver migration is
needed for this client-side permission change. The selected host package
remains source15a80f65.

Permission is resampled at most20Hz and age/vehicle conditions are checked
again per callback, including each asset hash/download block and USB exchange.
The boot gate remains alive and displays waiting_park until approval. Bulk
transfer/cache work temporarily leaves FIFO/core7 for SCHED_OTHER/CPUs0–3 and
restores the prior scheduling in finally. No new realtime priorities are used.
Leaving Park, enabling assistance, losing fresh data or unplugging stops an
unregistered transfer without selecting/activating it. Once a signed release
has been explicitly selected, the unused host's existing atomic installation
may complete; this change cannot cancel a legacy activation already running.
Normal runtime peers cannot enter the boot-gate upload path. New Jetson engine
preparation and warmup also recheck offroad/parked permission before READY;
normal preloaded reconnect behavior and the existing model-join policy remain
unchanged. Other Mac/phone setup stays offroad-only.

PC tests simulate parked upload with WLAN/Internet unavailable (cached assets),
movement/ACC/assistance/stale/invalid/unknown CAN denial, shift-out mid-transfer,
waiting-to-approved progression, idle-peer admission, model setup and scheduling
restoration. Actual car/Jetson installation and vehicle validation are pending.

Final parked-update PC result:358 passed, one unchanged isolated boot-display
fixture excluded for missing WSL system-Python PIL, three inherited pytest
configuration warnings. Localized user-doc validation passed. No vehicle,
phone, Jetson service or active simulation was accessed or changed.


## Optional cable DHCP cleanup and Park recovery (2026-10-19)

The owner's subsequent status showed an optional MobileCable DHCP termination
timeout, followed by the daemon's permanent cleanup block. This happens before
USB bootstrap discovery, so changing the Park permission check cannot resolve
that particular block. The reason SIGTERM failed on the vehicle is not known.

The helper now waits two seconds for TERM, then escalates to KILL only for its
verified dnsmasq executable, dedicated arguments and saved PID/start identity.
Pidfds pin the target when available; the AGNOS 4.9 fallback revalidates identity
before each signal. Exit must be observed after either signal. A still-live task
retains its ownership records and refuses rebind. A matching already-exited
zombie can release its records without signaling or requiring a vanished exe.
No name-based kill, AP DHCP operation, NCM function destruction or foreign
interface/process cleanup is introduced.

The daemon retains failed cleanup objects. Only failures confined to the
optional MobileCable network may retry, at most once per five seconds, after
the same offroad/fresh Park/inactive Permission approves. Confirmed prior USB
owner cleanup remains mandatory; native AIO/reader/watchdog failures and unsafe
constructor cleanup keep their hard block. Verified network cleanup lets auto
mode continue through plain bulk USB without recreating optional DHCP. Retry
work runs with normal scheduling on CPUs0–3. Update transfer, signature checks,
continuous parked permission, runtime/model joining and AP policy are unchanged.

Boot discovery and waiting-Park progression already existed. This correction
unblocks their optional-network failure path, rather than adding hot replacement
of a running Jetson model. After updating comma, restart it parked to load the
new daemon, keep USB attached, and normally reboot Jetson for its boot receiver.
Actual vehicle installation and the original incident remain unverified.

Final PC checks:572 passed, including real private child processes with blocked
TERM on both pidfd and legacy signal paths, PID/argument-change refusal,
unreaped-zombie recovery, retained state for simulated unkillable tasks,
Park-denied/approved daemon recovery, five-second retry spacing and hard native
cleanup blocking. One unchanged boot-display fixture still requires PIL absent
from WSL system Python and is excluded; three inherited pytest configuration
warnings remain. Korean/English user-doc validation passed. No device, AP,
external simulation or bridge process was accessed or stopped.
