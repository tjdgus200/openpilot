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

The existing private USB provisioner exports only this explicitly owned, active
AP as a client profile. Other AP/tethering profiles remain excluded. Jetson's
existing network importer handles the client connection and persisted fallback
profiles; this feature does not replace its signed model runtime. The direct
profile stays first and active regardless of the phone's association state.
Each recreated AP interface/boot gets a distinct client-profile UUID, retaining
its SSID/password. This lets the immutable legacy importer reconsider its
preferred connection after an AP restart instead of indefinitely accepting a
connected phone fallback. It does not rotate credentials or churn profiles when
the phone disconnects/rejoins. No protected Jetson OS patch is required.

The HUD sender selects an actual WLAN route to a fresh USB-advertised Jetson
address, preferring the direct interface. It keys connection lifetime to the
selected link rather than the entire interface list, so losing the unrelated
Internet interface does not force a HUD reconnect. There is no USB HUD fallback.

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
