# PV5 battery display through a dedicated BLE adapter

> Updated 2026-10-27: retry limits, polling cadence, temperature history and thermal-mode output below are historical. Current behavior and remaining mapping limits are documented in [the OBD/HUD follow-up](pv5_obd_followup_20261027.md).

## Scope and evidence

The owner demonstrated a successful BLE GATT connection, ELM identity reply,
adapter setup and a complete BMS ReadDataByIdentifier response on a
JINXUSOLU JXSL-MIC34LE (hardware 2.1). This establishes transport feasibility,
not calibration of every battery field or continuous-runtime reliability.

The new reader is local opt-in. There is no user's MAC address or credential
in the repository. A configuration created by `configure_obd_battery.py`
persists under `/data/jetlink`; the manager starts the reader once a real
`KIA_PV5` is identified and onroad, including Park. It stops offroad. This is
not a daemon that wakes a parked sleeping vehicle indefinitely.

## Comma BLEOBD indicator

C3 and C4 show a green `BLEOBD` badge below the Jetlink/eGPU badge when the
automatic collector reports a complete, validated three-DID cycle. Manual
Bluetooth pairing/connection alone does not light this badge. It indicates
recent data reception, not independent calibration of battery measurements.
It can therefore be green while unconfirmed battery-screen fields remain blank.

The target-address configuration is a one-time local enrollment, not a switch
the driver must enable on every trip. There is currently no separate GUI toggle.
PV5/onroad detection and the three queries run automatically after enrollment.
This change does not create or overwrite any device's existing enrollment.

The comma UI reads two bounded files (at most 4 KiB each) at most once per second
while onroad in a real PV5. It holds only a timestamp between reads. Missing,
malformed, wrong-identity or non-receiving snapshots hide the badge. Timestamps
expire after five seconds, checked on each UI update without additional reads;
offroad/non-PV5 clears the indicator. No Bluetooth calls, worker thread, diagnostic
requests or history buffer are added to the UI. Physical-device resource use and
display layout still require confirmation; PC tests do not establish road safety.

## Data path and resource bounds

OBD BLE adapter → separate comma reader → atomic RAM snapshot → existing USB
HUD packet → existing Jetson battery screen 8. No new Jetson package, network
listener, Internet upload, model payload, or vehicle-control consumer is added.

* A single configured address is connected. Model, maker, UART service and
  characteristic are also checked. These reduce accidental device selection;
  they are not cryptographic authentication. Other Bluetooth users are not
  disabled or reconfigured. Do not use the OBD adapter as a remote-control input.
* Three fixed read-only diagnostic DIDs (`0101`, `0105`, `0106`) are polled
  sequentially, with a two-second sleep after each complete cycle (at most
  1.5 requests/second over repeated cycles, not a strict per-second burst limit).
  No raw CAN subscription, ECU writes,
  DTC clearing, arbitrary command API or diagnostic-session changes.
* Low-priority process (`nice 19`, ordinary scheduler), not a model/HUD callback.
  The HUD reads at most 4 KiB configuration and 4 KiB snapshot once per second.
* One request at a time, three-second transaction deadline, bounded BLE
  notification queue (32 entries, at most 512 bytes each), at most 4 KiB per
  reply. These are payload bounds, **not total Python process RSS**.
* Failure closes the dedicated connection and waits five seconds before retrying.
  A monotonic 60-second window includes discovery, connection, setup and the first
  complete polling cycle. Slow attempts consume this budget; attempts never overlap.
  After exhaustion, `retry_exhausted` clears the data and the process sleeps without
  reconnecting (not exiting into a manager restart loop). A new manager-controlled
  offroad/onroad start or reboot starts a new window. Configuration edits alone do
  not restart an exhausted worker. Deadline cleanup can take up to two additional
  seconds, without starting more connections or polls.
  A successful complete cycle removes the startup deadline; healthy collection
  continues. A later connection/poll failure gets a fresh 300-second recovery window.
  Every subsequent complete validated poll re-arms that recovery allowance for
  the next disconnect. Connection/setup alone does not replenish it, and failed
  attempts share one deadline. The initial connection window remains 60 seconds;
  the five-second retry sleep and five-second snapshot freshness limit are unchanged.
  Discovery is bounded; the BlueZ service and unrelated devices are untouched.
* Wrong response ID/DID/length/sequence, partial data, extra prompts, unsolicited
  queued data and queue overflow reject the transaction. A timeout never
  continues parsing the same buffer as the next request. Reconnection repeats
  adapter identity and setup. ELM has no request nonce: hardware late-response
  behavior still requires a live fault/reconnect test.
* Failure clears the snapshot; crashed-reader values expire after five seconds.
  Configuration identity prevents reuse of a former device/confirmation snapshot.
  Timestamp the cycle before its first request, so later replies cannot refresh
  older values. Any failed/unsupported DID aborts the entire cycle and backs off;
  it does not leave a partially refreshed mixture of fields.
* Five-minute maximum-temperature change uses two fixed arrays of 153 doubles:
  2,448 bytes of numeric storage, plus fixed Python object overhead. No CAN
  history is retained. Samples are at least two seconds apart; use the oldest
  sample only when its age is 298–302 seconds. Invalid values, reversed time,
  gaps over five seconds, reconnects and configuration changes discard history.
  Before a complete window exists the trend remains unavailable. The collector's
  total RSS and BlueZ overhead are separate from this small history allocation.

## Calibration gate

The initial candidate map follows the author's
[Ioniq 5 PID definitions](https://github.com/Esprit1st/Hyundai-Ioniq-5-Torque-Pro-PIDs/blob/main/TorqueIONIQ5AWD77kWh.csv).
This other-model map is **not a validated PV5 specification**. Candidates are
maximum/minimum battery temperature, signed pack power and cell-voltage spread.
Each field must be independently compared on the owner's PV5 before it is
selected with `confirm --fields ... --compared-on-pv5`.

Enabling alone collects candidate values; it does not publish unconfirmed
numbers to the driving HUD. `status` exposes a compact candidate summary for
comparison. Compare power sign under known charge/discharge conditions and
cell spread under more than one sample; plausible ranges alone are insufficient.
Do not connect Car Scanner and this collector to the adapter simultaneously.
Use sequential stable-condition comparisons with timestamps.

The owner approved the inlet/management/display-SOC meanings, but that approval
does not validate byte offsets. Additional candidates are:

| HUD field | DID and zero-based data offset (excluding `62 xx xx`) | Candidate conversion |
| --- | --- | --- |
| Display SOC | `0105`, D31 (AF) | unsigned byte / 2; reject above 100% |
| Coolant inlet | `0106`, D4 | signed byte in degrees Celsius; accept -30 to 120 |
| Thermal management | `0106`, D9 | 0 idle, 185 cooling, 125 heating; omit unknown codes |
| Maximum-temperature change | calibrated maximum-temperature history | current minus approximately five minutes earlier |

The `0106` map follows
[evDash's eGMP decoder](https://github.com/nickn17/evDash/blob/master/src/CarHyundaiEgmp.cpp).
It is an other-model reference, not a PV5 specification. Exact accepted payload
lengths including the three-byte DID prefix are 62/46/38 bytes respectively.
Seven matching `0106` transactions in the supplied accumulated scanner log
gave inlet candidates from 17 to 29 degrees and management code zero. They were
not simultaneous independent temperature measurements. The older Kona/Niro
`0101` D22 inlet interpretation is deliberately not used. Neither heater
temperature nor available discharge power is substituted for thermal-system
electrical consumption; that field remains unavailable. Internal BMS SOC is not
substituted for display SOC. Trend confirmation also requires maximum-temperature
confirmation. Existing passive-profile operation remains available when BLE is
disabled.

## Validation (PC only)

* 107 focused tests pass on Windows and WSL Linux: protocol, malformed replies,
  queue limits, timeout/reconnect, identity/setup rejection, cleanup failure,
  manager activation gate, opt-in/calibration CLI and existing HUD behavior.
  These include cross-DID rejection, signed/range conversion, unknown management
  codes, bounded history through 5,000 samples, history resets and cycle freshness.
* Ruff passes for the new reader, configuration tool and tests.
* Local replay of the supplied accumulated scanner log: 15 matching transactions,
  14 accepted, one rejected. Logged command boundaries reconstruct prompts;
  this is not an actual BLE timing test. No raw payload is shipped in Git.
* Windows native cereal import was unavailable; the existing bounded-consumer
  test now installs its intended messaging stub without loading native schemas.
  The same tests also pass on Linux. This does not test native CAN transport.

Still required: actual comma RSS/CPU and model/HUD latency under simultaneous
load, ignition/offroad lifecycle, adapter disappearance/reconnection and
independent PV5 field calibration. PC tests do not establish on-road safety.
No device was installed/rebooted during this implementation.

### 2026-10-26 bounded retry update

Follow-up: recovery allowance is now 300 seconds after a complete validated poll,
while initial startup remains 60 seconds. Windows and WSL Linux each pass 114
focused tests, including repeated successful recovery, stuck connection/first
poll, immediate failures without deadline extension, cleanup before the next
attempt, exhaustion without manager restart, and existing stale HUD rejection.
No extra history buffer or concurrent connection is introduced. No live adapter
reconnection or device installation was performed for this follow-up.

111 focused tests pass on Windows and WSL Linux, including cancellation of a stuck connection
and first poll, five-second production retry delay, no new attempt after budget
exhaustion, a healthy session lasting beyond startup budget and a fresh bounded
recovery window after failure. Shortened wall-clock windows in synthetic tests
exercise the same deadline logic; they do not measure live adapter timing.

The manufacturer's "JINXUSOLU OBD-II converter and bluetooth chips comparison
table V5" lists wireless wake support for MIC34LE.
That does not establish the wake trigger or deep-sleep behavior of this assembled
adapter/firmware. Existing BLE discovery/connect and `ATI` identity verification
remain unchanged; no guessed wake command, ECU write or permanent sleep-disable
setting is added. A real sleep-to-connect test is still required.
