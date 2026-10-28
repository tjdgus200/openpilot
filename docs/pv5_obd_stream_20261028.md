# PV5 BLE OBD stream and session changes

> Historical, withdrawn: the stream/session changes below are no longer active.
> The owner requested restoration of the previous optimized collector after
> reporting more frequent connection loss. See the rollback record below.

The owner reports that phone Car Scanner remains connected while the comma
reader reconnects frequently. There is no new time-aligned BlueZ/HCI capture,
so a radio/driver failure or the dominant real-world cause is not established.
Car Scanner is not open source; this change follows the supplied adapter log,
observed GATT services and public protocol guidance, not an identical internal
implementation or a claim of measured connection reliability.

## Evidence and changes

The supplied scanner log contains explicit `ATAT1` and `ATST96`. The collector
now explicitly restores that adaptive-timing mode, keeping the same conservative
maximum timeout. ELM327 normally defaults to adaptive mode1, so the previous
absence of an explicit command does **not** prove that adaptive timing was off.
Only the existing BMS ECU and fixed read-only DID allowlist are polled; the
scanner's unrelated ECU/service operations are not copied.

The exact enrolled JXSL-MIC34LE device exposed two BLE UARTs in the owner's
service discovery: split FF12/FF14/FF15 and the vendor E781/BEF8 UART. Prefer
the split UART if present. Use a confirmed-write operation when supported;
otherwise use the characteristic's write-without-response operation and still
wait for the ELM reply and terminal prompt. A failed session is cleaned up before
trying the other supported UART. There is one BLE connection to the same locally
enrolled address, never concurrent IOS/Android links, MAC-address arithmetic,
name-based arbitrary connections, classic-Bluetooth pairing or new credentials.
Both routes require the original model/manufacturer check and ELM identity reply.

Cached device paths avoid unnecessary discovery. Recovery discovery requests LE
only, suppresses duplicate advertising delivery, and releases its own scan lease
before connecting. It does not stop BlueZ, another client's discovery lease or
unrelated Bluetooth connections. Actual Device1 disconnect/notification loss
wakes the response waiter with a bounded failure category.

The byte-stream parser accepts a terminal prompt followed by ASCII whitespace,
including whitespace arriving separately before the next request. This avoids
software disconnects on valid transport fragmentation. It never discards late
CAN payloads, a second prompt, wrong-source/wrong-DID responses, malformed frames
or overflow to manufacture continuity. Payload assembly remains limited to4096
bytes,512 bytes per notification,32 queued notifications and64 reads/transaction.
Concurrent command attempts are rejected without creating a request backlog.

ATT write acknowledgement has a separate two-second deadline. The complete ELM
response budget starts after that operation and adapts within3–6seconds; after a
prompt timeout, the next clean session receives the longer budget. Late partial
responses still force cleanup before retry. Complete fixed-source negative
replies to read22 back off only that DID without reconnecting Bluetooth. Pending78,
mixed transactions and wrong services remain fail-closed. NO DATA behavior remains
unchanged, and no missing field becomes a valid zero.

## Preserved limits and verification

The one-request/one-response priority scheduler, adaptive quiet gap, optional
fresh-P cell scans,108-channel/15-temperature bounds, original observation times,
power5s/other60s HUD expiry and five-second unlimited reconnects remain. No new
thread, polling history, general diagnostic command interface or driving-control
dependency is introduced. Readiness still requires fresh data, not Bluetooth
pairing alone. Diagnostic metadata contains only bounded route/timing/error counts,
not addresses or raw replies.

262 PC tests pass, including existing scheduler/expiry/HUD regression checks.
Fault injection covers split/delayed padding, slow acknowledgements, disconnects,
concurrent requests, both UARTs, missing/incorrect identities, ECU rejections,
wrong/pending responses and cleanup-before-retry ordering. These tests do not measure physical BLE
stability, radio interference, CPU load or road safety. The existing Jetson package
understands the unchanged battery wire format; only comma collector code changes.

References: [Car Scanner connection guidance](https://www.carscanner.info/bluetooth-troubleshooting/),
[Car Scanner polling guidance](https://www.carscanner.info/optimizing-connection-speed/),
[ELM327 datasheet, communication/adaptive timing](https://www.elmelectronics.com/wp-content/uploads/2016/07/ELM327DS.pdf),
[BlueZ GATT write/notification API](https://bluez.readthedocs.io/en/latest/gatt-api/).

## Rollback to the optimized collector

On2026-10-28 the owner reported worse physical connection stability after the
scanner-style update and asked to return to the optimized version. Restore
`openpilot/selfdrive/carrot/obd_battery.py` byte-for-byte to the pre-change
implementation, whose latest collector change is `e387b811`. The restore
includes the original single vendor UART with GATT request writes, original
ELM setup and response timeout, discovery and reconnect behavior. Remove the
scanner-style dual-UART preference/fallback and its implementation-specific
tests; do not mix selected parts of that experiment into the restored policy.

Retain the optimized one-request/one-response priority scheduler, adaptive
50–500ms quiet interval, NO DATA per-DID backoff, five-second unlimited retries,
allowlisted enrolled target, confirmed fields,108 voltage channels, field ages,
power5s/other60s retention and fresh-P cell gates. No configuration/key reset,
pairing change or Bluetooth-service restart is involved. HUD Q92, saved onroad
FPS, model/USB scheduling and the selected Jetson package are unchanged.

The observed instability is a user report, not a time-aligned HCI diagnosis.
Neither the cause nor post-rollback physical reliability is established by PC
tests. The collector runs on comma; the unchanged HUD wire contract remains
compatible with the current Jetson package, so this rollback needs only a
comma software update and normal parked reboot.

The restored collector's Git blob is identical to `e387b811`:
`2b7542efc366bfe97f61635d37fe3284381e3f9d`. Existing transport, priority,
retry, retention, cell admission, badge, battery HUD and latest FPS regressions
pass274 PC tests. Ruff and diff checks pass. No physical device was changed
or tested by this rollback task.
