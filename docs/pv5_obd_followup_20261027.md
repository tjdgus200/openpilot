# PV5 OBD scheduling and HUD follow-up

## Implemented behavior

- The enrolled, single BLE target retries indefinitely while the manager enables
  the reader for a real PV5 onroad. Each connection/setup/first-poll attempt is
  bounded to 60 seconds; cleanup finishes before a five-second retry wait. This
  does not run after manager offroad shutdown or enable other Bluetooth targets.
- The reader targets `0101` at 0.5 seconds and `0105`/`0106` at one second.
  Temperature/cell-spread publication also targets one second. Requests stay
  sequential, with no queue or catch-up burst. Slow adapter transactions reduce
  the achievable rate; these are scheduling targets, not measured adapter rates.
  There are four read-only DID requests/second with an instantaneous adapter.
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
