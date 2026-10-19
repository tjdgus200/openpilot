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
- Final Jetson migration, phone-loss continuity and latency measurements are
  recorded after deployment below; the AP-only preflight is not their substitute.

Driver support is conditional on firmware/regulatory/channel and beacon-interval
constraints. AP startup failure leaves the ordinary saved Wi-Fi provisioner and
USB model transport available; it does not justify retrying network activation
onroad. Short stationary tests cannot establish driving safety or universal
hotspot compatibility.
