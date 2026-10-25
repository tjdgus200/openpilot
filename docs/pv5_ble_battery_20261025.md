# PV5 battery display through a dedicated BLE adapter

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

## Data path and resource bounds

OBD BLE adapter → separate comma reader → atomic RAM snapshot → existing USB
HUD packet → existing Jetson battery screen 8. No new Jetson package, network
listener, Internet upload, model payload, or vehicle-control consumer is added.

* A single configured address is connected. Model, maker, UART service and
  characteristic are also checked. These reduce accidental device selection;
  they are not cryptographic authentication. Other Bluetooth users are not
  disabled or reconfigured. Do not use the OBD adapter as a remote-control input.
* One fixed read-only diagnostic DID (`0101`) is polled, with a two-second
  sleep after each completed response. No raw CAN subscription, ECU writes,
  DTC clearing, arbitrary command API or diagnostic-session changes.
* Low-priority process (`nice 19`, ordinary scheduler), not a model/HUD callback.
  The HUD reads at most 4 KiB configuration and 4 KiB snapshot once per second.
* One request at a time, three-second transaction deadline, bounded BLE
  notification queue (32 entries, at most 512 bytes each), at most 4 KiB per
  reply. These are payload bounds, **not total Python process RSS**.
* Failure closes the dedicated connection and retries with 5–60 second backoff.
  Discovery is bounded; the BlueZ service and unrelated devices are untouched.
* Wrong response ID/DID/length/sequence, partial data, extra prompts, unsolicited
  queued data and queue overflow reject the transaction. A timeout never
  continues parsing the same buffer as the next request. Reconnection repeats
  adapter identity and setup. ELM has no request nonce: hardware late-response
  behavior still requires a live fault/reconnect test.
* Failure clears the snapshot; crashed-reader values expire after five seconds.
  Configuration identity prevents reuse of a former device/confirmation snapshot.

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

Displayed SOC, coolant inlet, thermal management and five-minute temperature
trend are not synthesized by this reader. Battery inlet is not assumed to be
coolant inlet, and BMS SOC is not substituted for dashboard SOC. Existing
passive-profile operation remains available when BLE is disabled.

## Validation (PC only)

* 78 focused tests pass on Windows and WSL Linux: protocol, malformed replies,
  queue limits, timeout/reconnect, identity/setup rejection, cleanup failure,
  manager activation gate, opt-in/calibration CLI and existing HUD behavior.
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
