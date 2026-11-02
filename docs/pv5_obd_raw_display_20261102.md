# PV5 temporary raw OBD display

The owner requested on-device raw candidate pages to compare cabin A/C ON/OFF.
This is a diagnostic experiment, not a compressor/power decoder. No field is
assigned to battery-care power, compressor RPM, heating or cooling by this change.

## Collection and display

The existing single BLE collector temporarily replaces normal metric/heatmap
polling with a fixed sequential six-page cycle. It uses the locally enrolled
adapter and its existing identity, transport, notification, timeout and teardown
checks; there is no second Bluetooth connection, extra service or process.

| Request / response ECU | DID | Complete UDS payload bytes |
| --- | --- | --- |
| 7E4 / 7EC | 0101 | 62 |
| 7E4 / 7EC | 0106 | 38 |
| 7E4 / 7EC | 0114 | 33 |
| 7B3 / 7BB | 0100 | 39 |
| 7B3 / 7BB | 0101 | 39 |
| 7B3 / 7BB | 0102 | 39 |

These exact source/DID/length combinations were observed in the owner's earlier
PV5 exports. They are candidate pages, not validated signal meanings. The three
BMS pages are shown on the left and the three HVAC pages on the right. `ID`
contains response ECU / DID, with the request ECU separately marked `TX`.
`D00` is the first data byte **after** `62` and the two DID bytes; row offsets
are decimal, data bytes are hexadecimal. All 232 data bytes are shown. Yellow
indicates a byte changed between displayed observations within the last three
seconds, not an OEM warning. Compare a screenshot's ID and exact D index.

The temporary page fills the entire onroad content rectangle with an opaque
black background. Data uses 32px text at 1920x1080 and scales to the actual
rectangle. Normal HUD painting is bypassed while this page is admitted;
the parent alert and driver-monitor layers continue to render above it.
Leaving the admission conditions immediately restores the normal HUD.

Normal battery metrics temporarily become unavailable under their existing
expiry rules; no debug bytes are sent to the Jetson HUD or control processes.
Each page displays its own age. Missing, malformed and expired data show
`WAITING`, `NO_DATA`, `TIMEOUT`, `UNAVAILABLE`, `INVALID` or `STALE` rather
than a substituted value. A missing page does not hide the other valid pages.

## Activation

This adds no persistent Carrot Web setting. It is OFF unless a local per-boot
diagnostic lease is explicitly created. On an updated comma with the existing
OBD enrollment, run from `/data/openpilot`:

```sh
PYTHONPATH="$PWD/pydeps:$PWD${PYTHONPATH:+:$PYTHONPATH}" python3 tools/jetlink/obd_diagnostics.py start --seconds 600
```

Stop before the expiry using the same command with `stop` instead of
`start --seconds 600`. The tool changes only the temporary local request file;
it neither opens Bluetooth itself nor restarts/kills a running service. A
completed response boundary precedes the collector's in-session mode handoff.
Mode changes no longer tear down a responsive Bluetooth connection. At the
completed transaction boundary the same owner retires late bytes using bounded
UART silence plus ATI identity verification, restores the BMS header/filter
with checked OK replies, then replaces only the poller/encoder. Both filter
commands are repeated even if permission loss interrupted a prior change before
the cached receive-ECU value was updated.
Lease renewal, stop, expiry or loss of Park proof use the same handoff. No
second link/router/notification subscription is created. Actual transport,
identity or filter-restore errors still close/retry through the existing path.
The display requires the updated comma UI; no Jetson runtime package is needed.

## Bounds and gates

- A lease is bound to the existing adapter configuration hash, a random session
  nonce, and monotonic start/expiry, at most 600 seconds. Reboot clears it.
- Before each diagnostic command and after each response, require fresh
  (<250 ms) valid/alive carState, carControl and selfdriveState, CAN valid and no
  CAN timeout, P, raw and filtered speed within 0.01 m/s of zero, and all
  engagement/lateral/longitudinal/active flags false. Leaving P, motion, stale
  evidence or control activation stops admitting further diagnostic requests.
  Permission is checked again after request pacing and before the BLE write.
  The UI checks the same vehicle gate at every render. An already submitted
  request cannot be recalled; normal collector cleanup follows its bounded end.
- The six fixed read-only DIDs and two known ECU filter/header pairs are the
  only additions. No arbitrary ID/DID scanning, write/control, reset, session
  change, pairing or trust commands are exposed.
- One request at a time, at most two DID requests/s plus fixed header/filter
  switches; wait at least 500 ms after completion, no catch-up burst. Each
  page updates roughly once per 3+ seconds, depending on response latency.
- Require exact response source, DID, declared length, frame count, ISO-TP
  sequence and one terminal ELM prompt. Never rotate, truncate or reuse invalid
  replies. A malformed reply is discarded and uses the existing bounded UART
  silence/ATI resynchronization; at most two consecutive recoveries, otherwise
  the existing close/reconnect path handles it. NO DATA is not a metric.
- A diagnostic DID's three-second ELM prompt timeout immediately clears only
  that row. Before advancing, the same Link must retire late fragments with
  the existing bounded quarantine and prove the local ATI identity. Only
  successful recovery permits the next fixed DID after the completion gap.
  Framed negative responses (including response-pending) similarly display
  UNAVAILABLE and require recovery; they carry no accepted signal payload.
  NO DATA advances directly. No unbounded queue, parallel request or retry
  burst is added. Recovery failure, GATT write failure, overflow and header/
  identity failures still reach existing teardown/reconnect. Successful raw
  response time alone renews diagnostic-receiving freshness, never a timeout.
- One latest row per page (232 binary data bytes / 464 hex characters), a
  <=4096-byte status file, <=512-byte lease and <=232 UI change markers.
  No sample history or persistent raw-log recorder. UI file reads at most 1 Hz;
  no disk reads per pixel/frame. Default-OFF adds no radio request.

This cycle does not guarantee the unknown compressor/power signal is included.
Temporal correlation is only a candidate; distinguish cabin A/C from battery
thermal care and verify scales independently before assigning a HUD label.

The green BLEOBD badge accepts `diagnostic_receiving` as well as normal
`receiving`. Diagnostic status retains the last strictly valid raw response's
original timestamp; NO DATA/invalid pages and mode changes cannot renew it.
The five-second freshness limit is unchanged. A missing reception badge is
not proof that the underlying Bluetooth radio disconnected.

## Validation

Synthetic tests cover all six payload contracts, malformed sources/DIDs/counts/
sequences, denied arbitrary commands, no-data and bounded resynchronization,
mode handoff on a single connection, failed UART/filter restoration,
revocation during pacing, motion/control/freshness gates,
lease/config/nonce/clock expiry, UI byte highlights, and complete page layout.
Existing OBD scheduling, reconnect, per-field expiry, incremental voltage and
comma HUD tests are included. The PC preview contains synthetic bytes only.
The updated complete PC suite passed 491 tests, including full-screen C3/C4
HUD hooks, a single-connection round trip, fresh diagnostic badge expiry,
last-moment permission loss and restoration after a partial header change.
The changed collector/diagnostic/status files and tests pass Ruff; the patch
passes `git diff --check`. PC results do not establish actual BLE stability, ECU/adapter behavior, CPU
cost, screen readability, or road safety. No vehicle installation was performed.

The timeout follow-up passed 511 related PC regressions. After adding three
page-isolation rendering cases, the focused collector/UI suite passed141 tests
(overlaps the regressions). Faults include per-page timeouts, all six pages
unavailable, late old data before ATI, recovery/permission failures, GATT
failures and source-mismatched negative replies. No live adapter was contacted.
