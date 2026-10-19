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
