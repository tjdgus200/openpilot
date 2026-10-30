# Discard incomplete OBD boundaries without an immediate radio teardown

The owner's latest passive captures at e6063b72 include the fixed sequence
CF6,FIRST,CF1..CF4 for 39-byte cell replies, and the same leading-tail issue
for the 38-byte page. Six frames arrive, but FIRST belongs after the leading
continuation and CF5 is missing. This is not a complete cyclic rotation.
The old rotation-only recovery rejects it, then the collector closes its
connection. Correct declared length does not establish a complete response.

The five-minute observation improved from 61.5 to 227.3 seconds of fresh
receiving state in 299.9 seconds sampled, with 315 to 1084 valid replies and
25 to seven start/length failures. Conditions were not controlled. Collector
failure entries (89 to 19) are not HCI radio-disconnection counts. The source
of the misplaced fragments remains unknown; this change does not establish
an RF, adapter, BlueZ or notification-order root cause.

## Additional recovery admission

Keep the strict data parser and the existing complete-rotation classifier.
A second discard-only classifier accepts exactly the captured six-frame
positions CF6,FIRST,CF1..CF4 for the existing 38/39-byte contracts. Require one
terminal prompt, ASCII, six complete eight-byte CAN lines from 7EC, and the
expected FIRST length and DID prefix. Wrong source, DID, length, sequence,
extra/missing frames, status text, multiple prompts and other contracts remain
fatal. Never trim the leading frame, synthesize CF5 or extract any value.

Both admitted faults use the same existing bounded quarantine: at most 32
notifications/4096 bytes, 512 bytes per notification, 500ms of silence within
two seconds, then the existing local ATI identity/prompt check. No ECU query
is sent during this recovery. Normal command queue guards are unchanged.
Failed identity, overflow, continuous input, timeout or a third consecutive
fault without a valid battery reply retains the cleanup/reconnect path.
Cancellation propagates. Two recovery types share one streak limit.

Discard pending temperature-page/cell-generation assemblies on either
recovery type. Already published good fields keep their original timestamps;
the failed query never refreshes them. An aborted partial cell scan waits at
least two seconds before starting another generation. Existing Park/freshness
admission, power/other-field expiry, request priorities, adaptive pacing,
exact-count suffixes and unlimited 100ms post-cleanup retry remain unchanged.
No history, worker, configuration setting, model path or Jetson package is
added. This is a comma collector change only.

## Verification and limits

Synthetic PC tests freeze the observed positions independently of the
production response table and verify strict rejection, recovery on the same
fake link, exact subsequent payloads, unchanged good-field timestamps,
aborted multi-page assemblies, malformed-neighbor rejection, failed ATI,
cancellation, shared recovery limits and repeated good/fault transactions.
Existing tests continue to cover late fragments, busy/overflow quarantine,
bounded cleanup, status IO faults, badge freshness and HUD retention.

No comma/adapter/Jetson or vehicle was accessed. Physical stability, reconnect
latency, CPU use and driving behavior remain unmeasured. After a parked
update/reboot, repeat the same 300-second passive observation and correlate
resync counters, fixed failure detail, accepted-data time and HCI events.
An increased recovery counter alone is not proof of improved radio quality.
