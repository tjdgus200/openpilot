# Retire malformed OBD replies before attempting radio recovery

The owner's next passive capture confirms the remaining transaction-boundary
fault. Two0105 failures had the correct46-byte declaration and7 total frames,
but their protocol order was CF6,FIRST,CF1..CF5 and
CF5,CF6,FIRST,CF1..CF4. Each was followed immediately by collector disconnect
and several setup retries. The60-second summary contains106 valid data replies
and4 such0105 failures. This is evidence of the byte stream received by the
application, not proof of where reordering/late delivery originates. A stale
tail cannot be assumed to belong to the new response and must not be reordered
into a usable payload.

The preceding300-second capture had61.5s of fresh receiving state out of299.9s
sampled,89 connecting-state entries,315 valid replies and25 length/start errors.
There were no observed sequence errors of the earlier mixed-response kind.
Connecting-state entries are not HCI radio-disconnect counts. The earlier short
captures are not a controlled baseline for an overall improvement percentage.

## Narrow recovery path

Strict parse_reply remains unchanged. Only an entirely rejected, single-prompt,
fixed-source transaction with the exact expected length/DID and frame count,
whose protocol positions form a nonzero cyclic rotation, may enter recovery.
No data is extracted from it. All other malformed replies, overflow and
transport failures retain their existing fatal path.

Before sending another command, quarantine this retired transaction's late
UART fragments. No new request is outstanding. Discard at most32 notifications
and4096bytes, keep the512-byte notification limit, and require500ms of silence
within2seconds. Overflow, sustained traffic or cancellation cannot create a
successful recovery. These discarded fragments are never fed into a new reply.
Normal command() queue guards are unchanged.

Then issue the existing local ATI identity query and require its complete
identity reply and terminal prompt within the existing transaction deadline.
ATI adds no CAN request and performs no reset, pairing or disconnect. A bad
identity, late data during this check or timeout falls back to existing cleanup.
This handshake checks a usable local command boundary; it does not guarantee
that future packets cannot be delayed or that another client is absent.

The rejected poll returns no value. Its old field timestamps are not refreshed;
the normal scheduler chooses the next query, without a duplicate ECU request
inside the same poll. Existing values age under the original power5s/other60s
HUD retention and missing data does not become zero. Two consecutive recovery
handshakes are allowed without a valid battery response; another rotated reply
then takes the fatal reconnect path. Only a fully validated battery response
resets that streak. Diagnostic counters saturate at65535 for successful
recoveries and2 for the streak; no sample history or worker is added.

An error recovery may wait up to2seconds for silence plus the existing bounded
ATI transaction. These asynchronous waits affect only the opt-in low-priority
OBD reader. The100ms unlimited post-cleanup reconnect policy remains unchanged.
response_ms is the last completed UART transaction latency, which can be ATI
when result is resynchronized; it is not GPU timing or a failed-poll duration.
No HUD wire, Jetson package, driving control or model path is changed.

## Verification and remaining work

PC fault tests cover all existing DID rotations while confirming that the strict
parser rejects them; wrong length/source/DID/sequence, mixed replies and missing
prompts cannot enter this path. Tests exercise real Link.command plus the new
recovery orchestration with fragmented fake notifications, late-tail retirement,
unchanged field ages, next-query independence, bad ATI replies and transport
failures. Continuous input, buffer/chunk limits and the consecutive-recovery cap
are bounded. These are synthetic protocol tests, not physical RF/CPU or road
safety validation. Repeat the same300-second passive capture after a normal
parked update/reboot, comparing fresh receiving time, connecting-state entries,
valid replies and remaining reply errors before claiming improved reliability.

Final validation:361 Windows PC checks pass, including33 focused boundary
recovery cases and the existing OBD, retention, badge, battery HUD/heatmap/
stationary and optimization-default preservation gates. Ruff and Git whitespace
checks pass. No actual comma, adapter or Jetson was accessed by these tests.
