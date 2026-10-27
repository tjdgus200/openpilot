# PV5 OBD scheduling and HUD follow-up

## Implemented behavior

- The enrolled, single BLE target retries indefinitely while the manager enables
  the reader for a real PV5 onroad. Each connection/setup/first-poll attempt is
  bounded to 60 seconds until a complete, valid transaction or exact terminal
  `NO DATA` reply proves the ELM session responsive. Cleanup finishes before a
  five-second retry wait. This
  does not run after manager offroad shutdown or enable other Bluetooth targets.
- The reader targets `0101` at 0.5 seconds and `0105`/`0106` at one second.
  Temperature/cell-spread publication also targets one second. Requests stay
  sequential, with no queue or catch-up burst. Slow adapter transactions reduce
  the achievable rate; these are scheduling targets, not measured adapter rates.
  There are four read-only DID requests/second with an instantaneous adapter.
- Each completed DID response updates the bounded RAM snapshot before the next
  request, rather than waiting for the whole group. Existing HUD consumer and
  badge cache rates are unchanged. A complete battery response can establish
  receiving state without all three DIDs succeeding; no-data alone cannot.
- Requests use GATT write-with-response and await exactly one terminal ELM
  prompt before proceeding. A quiet gap adapts between50 and500ms, increasing
  with response time or `NO DATA` and gradually reducing after successful replies.
  These are local pacing heuristics, not Bluetooth-standard timing guarantees.
- Only an exact, bounded, prompt-terminated `NO DATA` reply is recoverable without
  disconnecting. Back off that DID for2/4/8/16/30seconds and continue other reads.
  Incomplete/extra prompts, unsolicited data, overflow, write failures and timeouts
  remain session failures; never discard uncertain bytes into another request.
- Every field retains its own observation time. Invalid fields are removed on
  their next scheduled read. A timeout or malformed transaction clears the whole
  session snapshot; reconnection starts with empty state. Existing configuration
  identity and five-second expiry remain. The RAM HUD consumer reads at 2 Hz;
  the reception badge keeps its 1 Hz cache. No new USB inference frame is sent.
- Removed both BLE and passive five-minute temperature histories. Legacy field
  names remain accepted in saved configurations for compatibility but are no
  longer produced or rendered.
- Negative pack power retains its minus sign and uses green text.
- A fresh same-side BSD warning plus turn request holds HUD brightness at 100%
  throughout the warning, including the pulse's dark frames. Cancellation,
  stale input or missing USB HUD data restores automatic exposure-based
  brightness at the next existing 100 ms check. Afterward, changing the saved
  brightness applies the new setting. This policy is scoped to the PV5 Jetson
  renderer; the base renderer keeps its existing brightness behavior.

The brightness and battery-renderer changes require the corresponding new
Jetson host package. Updating comma's collector alone cannot change an installed
older renderer. No package/model/runtime validation or rollback gate is removed.

## Thermal and compressor findings

The accumulated owner-supplied scanner log was reassembled at explicit command
boundaries, checking response address, payload length, sequence and DID. It
contains 10 complete `7B3/220100 -> 7BB` transactions, 12 `7B3/220102 -> 7BB`
transactions and seven `7E4/220106 -> 7EC` transactions in this strict pass.
These counts deliberately exclude responses without an explicit matching
request context. No raw frames or device identifiers are included here.

The previously used `0106` data-byte-9 thermal mode was zero in all seven
transactions. They are not timestamp-aligned with the owner's later conditioning
event reported as 5.5 kW. Thus neither the mode mapping nor a compressor-power
conversion can be calibrated from this log.

Public implementations disagree in meaning across vehicles:

- [evDash eGMP decoder](https://github.com/nickn17/evDash/blob/master/src/CarHyundaiEgmp.cpp)
  maps response byte 12 (data byte 9 after `62 01 06`) to management modes.
- [OBDb/Pelican EV3 parameters](https://pelican.clutch.engineering/cars/kia/ev3/parameters)
  documents that position as a compressor-speed candidate on the 2025 EV3,
  multiplied by 20 rpm. This is an EV3 definition, not a PV5 specification.

The BLE PV5 reader therefore stops publishing that unverified mode. It continues
the separately compared inlet-temperature field. Thermal electrical consumption
and compressor electrical consumption remain unavailable. Pack power includes
other loads and cannot be relabeled as either one; compressor rpm alone cannot
establish electrical input power. The car's conditioning figure may include
heater/pump loads and is not a compressor-only reference measurement.

To establish a PV5 mapping, record timestamped, sequential stationary states:
conditioning off, battery conditioning on, and cabin A/C on. Record the exact
Car Scanner sensor names/units and vehicle display values alongside the replies.
Compare an actual electrical-power field or coherent compressor DC voltage and
current; reject rpm/duty-command substitutes. No additional ECU queries or
guessed CAN decoding are enabled by this change.

## 80% charging estimate

The owner selected a Car Scanner `CALC` estimate rather than a vehicle-reported
remaining-time signal. Its exact charger rating/formula and the PV5 battery
variant still need identification. The
[public Ioniq 5 PID list](https://github.com/Esprit1st/Hyundai-Ioniq-5-Torque-Pro-PIDs/blob/main/TorqueIONIQ5AWD77kWh.csv)
contains charger-specific, other-model estimates; those coefficients are not
PV5 calibration. The replacement HUD row accepts `dc_charge_80_min` from a
validated source, but the BLE reader supplies no guessed estimate. It stays
`--` until the calculation inputs are established.

## Validation limits

PC fault injection checks sequential retries, bounded attempts and cancellation,
per-field age/expiry, cadence targets and absence of growing histories. HUD
tests cover both BSD directions, pulse dark frames, cancellation/expiry/setting
changes, signed power and alert layering. Native Cap'n Proto tests run in a
temporary ASCII path because the local library fails to resolve an include in
this workstation's Unicode checkout path. No production parser path is changed.

These checks do not establish real adapter latency, long-term radio stability,
comma CPU/RSS, vehicle power behavior, inference P99 or road safety. No live
comma/Jetson installation or vehicle restart is performed by this change.

Focused Linux PC validation: 268 tests passed; Windows core reader/status checks: 90 passed. Windows-supported display subset: 100 passed (77 native-schema cases run on Linux). User documentation and two localized Wiki brightness pages pass their validators. These counts overlap and must not be added as distinct tests.

Public host package: source `e3ef8184a0d7df5c72a4c901fa5ecad0d78d21a3`, 64796884 bytes, SHA-256 `c059c1cb0987f9292d318f18eafcc8d60315d4569d5f3f337da396cc144943b4`. All four public assets were anonymously downloaded and verified. [Release](https://github.com/tjdgus200/openpilot/releases/tag/jetson-pv5-obd-hud-20261027-e3ef8184). Source CI [40352105827](https://github.com/tjdgus200/openpilot/actions/runs/40352105827) passed both Linux contracts and Windows installer checks. Device installation was not performed. Korean/English Wiki manual edits pass validation but remain in a local commit because upstream Wiki write authentication was unavailable.

After selecting this exact public package, 337 combined Linux PC checks passed, including USB SHA-256 delivery, interrupted transfers, failed candidate retention and fresh Park/zero update permission.

## Charging detail and bounded heatmaps (follow-up)

The owner subsequently confirmed108 voltage channels on the PV5. The108-slot
display was already enabled; this confirms the channel count, not every voltage
value/scale. A separate collection starvation was reproduced with synthetic
120ms transactions: the pre-poll vehicle snapshot aged beyond300ms during the
three standard reads, so slow polls discarded incomplete cell scans even in P.
The collector now performs its existing nonblocking carState update immediately
before cell admission, after those BLE reads, rather than before the poll. The
300ms limit, Park/zero conditions, four-block scan bounds and onroad lifetime
are unchanged. PC399 focused checks passed, including fresh-P completion,
fresh-D refusal and genuinely stale-P refusal. This is a comma collector fix;
the already published battery-hold Jetson package supports the same wire data.
No actual vehicle voltage reception or Bluetooth fault diagnosis is claimed.

Before the adaptive follow-up below, connection analysis reproduced a complete`NO DATA` coolant response and a
transaction timeout causing StopNotify/Disconnect, without injecting a radio
failure: the then-current outer error handler reset the entire link on either.
Requests are sequential; an idealized20ms mock produces40 standard requests per
10s, plus12 cell-block requests while parked. These counts are scheduling
examples, not actual adapter performance or evidence of bus overload. Single
polls can contain3/4 back-to-back sequential requests without an inter-command
gap. GATT writes have a2s deadline; the ELM prompt uses a3s transaction deadline.
Runtime exceptions then collapsed to an unavailable snapshot with no saved
cause, so physical radio/driver loss cannot be distinguished from application
initiated teardown using the existing status alone. Future diagnostics should
separate those cases using bounded error categories and the BlueZ disconnect
reason ([upstream management protocol](https://github.com/bluez/bluez/blob/master/doc/mgmt-protocol.rst)).
That collector fix did not change Bluetooth reset/retry policy. The subsequent
adaptive follow-up supersedes the complete-NO-DATA behavior and adds local
failure categories; it does not establish the physical disconnect reason.

### Adaptive response publication and recovery

The latest comma-side follow-up publishes after each complete response and adds
the50-500ms quiet gap and per-DID2-30second no-data backoff described above.
The0.5s power and1s other-metric periods remain best-effort targets. The poller
does not queue missed periods, overlap commands or add a worker. Slow hardware
can also fail to complete the optional four-block cell scan within its unchanged
2.5s bound; no mixed or partial array is published in that case.

The local snapshot's optional `collection` object contains the last fixed DID,
result category, bounded no-data counter, last no-data DID, request-gap and
response timing, plus a failure category when unavailable. It contains no raw
frames, private address or exception text. JSON4096bytes, notification queue32,
chunk512bytes, response4096bytes and64-notification bounds remain. Complete
no-data does not renew any field timestamp or create values; existing power5s,
other-fields60s display retention and five-second badge freshness remain.

PC416 focused checks passed, including per-response publication, DID backoff,
no-data-only session retention, pacing/decay, late-byte rejection, fatal timeout
classification, complete cell generations and RAM snapshot bounds. This is
synthetic validation, not a measured reduction in real Bluetooth disconnects,
CPU use or road risk. The selected battery-hold Jetson bundle supports the
unchanged wire data; this follow-up requires a comma update only.

Latest BLE retention follow-up: total pack draw/regeneration expires five seconds
after its own last valid observation; other battery fields, including heatmaps,
expire after60seconds. The comma HUD worker keeps one last-good value and original
timestamp per field for the enrolled configuration. Reconnects, failed attempts
and status writes do not renew these timestamps. Partial reconnection refreshes
only fields actually received. Config/address changes, leaving the battery page,
vehicle changes and backward clock jumps clear retained data. No rolling history,
new worker, BLE request or disk cache is introduced. Held values are labeled after
five seconds; missing/expired values remain unavailable/gray. Fresh USB and
300ms vehicle/motion/Park checks, and the separate five-second BLE badge check,
remain unchanged. PC396 focused checks passed, including expiry boundaries,
partial reconnection, stale replay, provenance and all reset cases. No physical
adapter stability or live CPU/RAM measurement is claimed.

Latest owner follow-up: mode8 shows the same battery detail whenever fresh
Park/stationary state is present, whether charging, not charging or the charge
flag is unavailable. It retains the300ms motion gate and returns to road on
gear/motion/vehicle expiry. Empty battery fields stay gray with no fabricated
data; charge-off/unknown titles say parked battery overview. An enrolled details
collector also reads the existing bounded cell scan while freshly parked without
requiring active AC/DC flags. Request order, scan limits, per-field expiry and
onroad lifetime are unchanged. This supersedes the charging-only view/scan
condition in the earlier implementation record below. No new live-device claim.
PC371 focused checks passed, including charge-off/unknown Park selection,
sequential no-charge scans, motion/gear/expiry fallback and unchanged other modes.
Actual-renderer synthetic previews were inspected for both panel layouts; Ruff
and the KO/EN user-guide validator passed. These are not vehicle measurements.

**Activation update, 2026-10-27:** the owner explicitly requested activation
before real charging comparison. This supersedes the confirmation-only launch
described in the original implementation record below. An existing enrolled
PV5 adapter now enables `charge_mode`, `cell_voltages_v`, `module_temps_c`,
`pack_voltage_v` and `pack_current_a` automatically. Local `confirmed_fields`
is never rewritten; unconfirmed preview provenance is carried in bounded
`unverified_fields` metadata to the HUD, which labels these observations as
vehicle-unverified. Other scalar fields retain their existing confirmation gate.
A changed configuration identity prevents reuse of pre-activation snapshots.
Park/zero and300ms vehicle freshness, individual5s expiry, strict response
parsing, sequential cell scans, existing onroad lifetime and all update/rollback
checks remain. No actual device install or vehicle calibration is claimed.
PC365 focused checks passed for this activation, including old-config automatic
display, unchanged confirmation files, malformed provenance rejection and expiry.

Original confirmation-only implementation record:

The owner requested an AC/DC charging view in the existing road/3D area, retaining
the battery summary, physical BSD overlay, W/temperature badge and alerts. The
new view requires mode8, a separately confirmed charge mode and Park/stationary
vehicle data whose combined sender/receiver age is at most300ms. It immediately
returns to the road view on loss of these conditions. It skips the 3D scene while
visible, without changing controls, model inputs, inference or USB scheduling.
Negative pack power alone never selects charging, and charging power is not
labeled as regeneration when a confirmed charging state is present.

Strict offline reassembly of the private scanner log found cell response lengths
of39 bytes for DIDs0102/0103/0104/010A/010B/010C. The first three contain32 plausible
voltage channels each; all ten complete010A samples contain12 before a field
boundary, while010B/010C contain zero candidates. The implementation uses the
observed108-channel layout, excluding the010A tail even if a later metadata byte
happens to look like voltage. This is an observed diagnostic topology, not an
independent physical-cell-count certification. The public other-model starting
offsets/scales are cross-referenced in the
[Ioniq5 PID source](https://github.com/Esprit1st/Hyundai-Ioniq-5-Torque-Pro-PIDs/blob/main/TorqueIONIQ5AWD77kWh.csv).

Five temperature candidates in0101 and seven plus three in0105 produce15 sensors.
The next0105 slot was zero in all seven complete samples and is excluded as the
observed tail. Temperature sensors are not individual cell thermometers or known
physical locations. Interior0°C/negative temperatures remain valid, whereas
invalid voltage readings become gray holes at their original channel indices.
Never truncate an already identified array at an interior fault or shift cells.

Arrays have fixed108/15 bounds, finite/range checks and independent five-second
expiry. Colors use absolute deviation from the current valid median on fixed
scales: voltage20/40/80/120mV and temperature1/3/5/8°C map through green, yellow,
orange and red. These are diagnostic presentation choices, not OEM fault limits;
absolute temperature/voltage and min/max spread stay visible. Unavailable values
are gray, never green zeros. No histories, image buffers or new threads are added.

Once charge-mode bits have been independently compared and confirmed, optional
cell candidates are collected only with fresh Park/zero state and an active AC/DC
candidate. At most one of the four fixed read-only blocks follows each existing
power poll. No queue/catch-up is introduced; a complete scan must finish within
2.5s and the next scan waits2s. Slow adapters can lower all actual update rates;
this is not a measured 2Hz guarantee. Config/session changes reset scan ownership.
The full array and the15-temperature array each still require separate field
confirmation before being forwarded to the HUD. Existing scalar configurations
are not automatically calibrated or switched to extra cell requests.

The log contains14 complete0101 responses with the candidate charging flag zero.
It cannot validate the AC/DC active bits. The owner says the vehicle is currently
not charging, so this change does not enable guessed charging flags or extend the
manager's offroad OBD lifetime. Ignition-off charging and actual charging/display
transitions remain pending a powered stationary vehicle comparison. Compressor
state/RPM also remains unavailable without a confirmed PV5 mapping. No live
device installation, latency/resource measurement or road-safety claim is made.

PC verification for this follow-up:359 focused tests passed, including real native
schema paths, scalar/array confirmation, partial-scan generation resets, Park
recheck, AC/DC/regen discrimination, stale-view fallback, existing BSD, USB
candidate retention and update permission. Actual renderer PNGs were inspected
for both panel layouts with explicitly synthetic data. Ruff and the user-guide
validator passed. These are not measurements of comma RSS/CPU, real BLE scan
latency, charger transitions, long-term connection stability or physical display
readability. This follow-up requires a new host package; the previous source
e3ef8184 release listed above does not contain the charging heatmap.

Published charging/heatmap host: source`3576cf2cdf579851b376f9627b14f41fff394998`,
64803729 bytes, SHA-256`8e4d42a0a02672e9d093dbacd931e5ff8b127ecee3eaf1f3af1752ed060d3aea`.
[Source CI40365431515](https://github.com/tjdgus200/openpilot/actions/runs/40365431515)
passed Linux contracts and Windows installer checks. All four public
[release assets](https://github.com/tjdgus200/openpilot/releases/tag/jetson-pv5-charging-heatmap-20261027-3576cf2c)
were anonymously downloaded and hash-verified. Export-only server/HUD imports,
native image/direct-map defaults and exact N16 pins passed. Device installation
and field confirmation were not performed. The selected package preserves
existing SHA/model/runtime validation and previous-release retention.

Owner-enabled observation package: source`b624a1c2e184579f6f6dc58f7e9edb42c1eb1aa1`,
64805205 bytes, SHA-256`83c0e0217694945b7f294ab2ed5aa07a745a131a29fece38724499219849ee4a`.
All four public
[prerelease assets](https://github.com/tjdgus200/openpilot/releases/tag/jetson-pv5-heatmap-active-20261027-b624a1c2)
were anonymously downloaded and hash-verified. This selection includes the default
activation and explicit unverified-field labels above. PC365 focused tests and
isolated package imports passed. At publication,
[source CI40368662835](https://github.com/tjdgus200/openpilot/actions/runs/40368662835)
was still in progress in the software HUD encoder stage; CI success is not claimed.
The owner requested activation now and vehicle comparison later. No comma/Jetson
installation or real charging/resource/safety validation was performed.

Parked-detail update: source`fef7e6b602472c9f6e944437702a6054c881e20f`,
64805316 bytes, SHA-256`344c647a1ae40461a14968fc23b2bea8071dcaf70234d7e8e389c25e6bf835f2`.
All four public
[parked-detail prerelease assets](https://github.com/tjdgus200/openpilot/releases/tag/jetson-pv5-parked-heatmap-20261027-fef7e6b6)
were anonymously downloaded and hash-verified; the selected USB manifest uses
this package. Isolated server/HUD imports and exact model/native-input defaults
passed. PC371 focused checks passed after integration on the morning-sync head.
Source GitHub CI had not started at publication; CI success is not claimed.
Device installation and real-vehicle comparison remain unperformed.

Battery-retention package: source`20dc029e1d0fdb0be4e19d8371c4cbe3cef3598b`,
64807590 bytes, SHA-256`5149db6e68c77a3fef24126c0d710b3a5fc9766600d92ebf88b155cc5ed87a3e`.
The selected USB manifest points to this
[public prerelease](https://github.com/tjdgus200/openpilot/releases/tag/jetson-pv5-battery-hold-20261027-20dc029e).
All four assets were anonymously downloaded and hash-verified. Isolated bundle
imports, five/sixty-second display expiry and existing N16/native-input defaults
passed. PC396 focused checks, Ruff, KO/EN docs and synthetic renderer previews
passed. Source GitHub CI had not started at publication; CI success is not claimed.
No actual comma/Jetson install or live Bluetooth stability/resource test was done.
