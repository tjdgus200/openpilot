# Direct Wi-Fi/HUD disconnect diagnosis, 2026-10-20

## Finding

The reported disappearing HUD data coincides with **real Jetson Wi-Fi
association losses**, plus HUD TCP reconnects while the interface is still
reported associated. This is not explained solely by the screen brightness
policy or a missing wireless driver. The exact radio/driver/firmware cause is
not established; no speculative driver replacement or persistent network
change was applied.

The inspection used comma `0635429f` and installed Jetson source `a1d723a6`.
Both devices retained their earlier boots. The comma was offroad and the
auxiliary classifier setting `ShareData` was zero. This was not a loaded
camera/20 Hz inference or driving test.

## Read-only evidence

* Jetson PCI identity: `10ec:c822`; loaded driver `rtl88x2ce`, version
  `v5.14.0.4-250-g5d0d248cc.202405`, firmware 9.13, kernel 5.15.148-tegra.
* Comma uses `icnss`. Its phone station and private AP were both on
  5,745 MHz/channel 149; the AP profile was band `a`, channel 149. There was
  no observed fixed phone/AP channel mismatch in these snapshots.
* Jetson's recent supplicant log contained 29 disconnect events, followed by
  repeated association attempts: 24 reported reason 0 and five reason 4.
  Some attempts were rejected with status 1. These codes alone do not identify
  the original trigger, the physical distance, or a defective driver.
* During 60 one-second Jetson samples, 45 reported an association and 15 did
  not. HUD receive status was connected in 19 samples and waiting in 41.
  Reported RSSI while associated ranged from -31 to -94 dBm. This variation
  supports unstable radio reporting/reception; it does not prove bad antennas
  or that physical received power actually changed by that amount.
* The overlapping 60-sample comma trace retained AP active and USB model link
  ready in all 60 samples. HUD sender status was connected in 17, reconnecting
  in 18, and waiting in 25. AP station counters increased by 281 retries and
  109 failed transmissions. Counter units differ from successful packet
  counters, so these are not an application packet-loss percentage.
* About 1,043,977 bytes of HUD snapshot payload were sent over the comma
  trace's 59.02-second sampled span; optional media bytes remained zero.
  A later stable 7.85-second direct-link ICMP sample returned 40/40 replies,
  mean RTT 4.480 ms and maximum 18.250 ms. This short success does not
  contradict the longer intermittent disconnect observation or prove stability.
* No restart of the Jetson inference/HUD/network worker was recorded during
  the inspection. The driver's sampled reset/suspend-error counters were
  zero, and the filtered recent kernel log contained no matching Wi-Fi
  firmware crash/reset or PCIe fault. A driver defect without a crash is
  still possible.

The two device traces overlap but their monotonic clocks are different.
Sample-by-sample cross-device causal alignment was not established. One
private probe mistakenly subtracted a file wall-clock mtime from monotonic
time; that field is explicitly excluded from all conclusions.

## Power saving and scanning

Both comma data interfaces reported nl80211 power-save zero. Jetson reported
`Power save: off`, and its driver LPS enter/leave counts were zero. Therefore
there is no observed LPS sleep transition explaining this trace.

The vendor driver still advertises default LPS MAX/IPS Card Disable modes;
its IPS enter/leave counters advanced from 844 to 847 during the broader
inspection. IPS activity while disconnected may be a consequence of lost
association; these observations do not establish which came first. Generic
nl80211 power-save off must not be described as proof that every vendor power
mechanism is disabled. Effective PCIe ASPM control bits were reported disabled.

Driver roaming flags were `0x07`. One RF snapshot showed current channel 149
but operating channel 116; a later snapshot showed both 149. This is consistent
with an off-channel interval, but no synchronized scan/event trace identifies
the initiator or proves it caused disconnection. The UI scanner's explicit
network-page mode can request scans; its installed background suppression
does not prove that NetworkManager or vendor roaming never scans.

## Why HUD data disappears

`hud_wifi_worker.py` uses a 0.4-second transaction/heartbeat budget and reconnects
on timeout or transport failure. `hud_protocol.py` expires received snapshots
after 0.5 seconds. Thus a real association gap or a sufficiently long radio
stall can both make displayed service data unavailable. An active AP flag
alone does not establish that its client is associated or receiving data.

NetworkManager can continue reporting an activated connection while the
supplicant is disconnected/reassociating. The network importer's `connected`
status was observed alongside `iw link` reporting not connected. Treat that
status as a control-plane hint rather than proof of live Wi-Fi delivery.

Increasing vehicle-data freshness limits would hide loss with old values;
this diagnosis does not justify that change. The primary USB model link
remained ready, but that flag is not evidence of loaded inference deadlines.

## Next discriminating experiments

Useful follow-up comparisons are direct AP alone versus concurrent phone
station, controlled AP channel/band comparisons, and a bounded vendor
roaming/background-scan comparison with an independent restore path. Those
interventions were **not run** in this read-only inspection. A compatible
alternative driver would need the same radio conditions and a measured A/B
before attributing this to a particular driver version.

Linux documents retry/failure counter meanings in its
[cfg80211 station statistics](https://kernel.org/doc/html/next/driver-api/80211/cfg80211.html).
The vendor diagnostics were read from this installed device; code for another
Realtek driver revision was not treated as proof of this revision's behavior.

Own temporary recorder, SSH relay and phone/localhost tunnels were stopped
and removed. Original Wi-Fi profiles, driver, firmware, services and model
were retained. Final comma snapshot: model link ready, no link error, HUD
connected at that instant. This is not a repair or a long-duration pass.
Numerical/log evidence and reproduction scripts remain private.
