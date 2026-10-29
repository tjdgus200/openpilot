# PV5 OBD missed-case audit

The owner requested a review of why the response-boundary faults were missed
and whether related cases were absent from the tests. This audit follows
64d8b90e (ELM response counts) and 82434ff5 (bounded rejection recovery).
There was no access to a comma, adapter, Jetson or vehicle during this audit.

## Why the original coverage was insufficient

- The fixtures fragmented one internally consistent reply. They did not
  independently replay a continuous transaction stream containing a previous
  reply's tail before the next FIRST frame.
- The response-count fake adapter assumed clean completion at the requested
  count. That verifies request formatting, not the physical adapter's boundary
  behavior. Frozen protocol positions from the owner's evidence now supplement
  the synthetic fixtures; all payload bytes in the fixtures are synthetic.
- Strict rejection protected the values, but rejection was usually coupled to
  disconnect/reconnect. A parser error could therefore create connection churn
  even while the physical BLE link was still present.
- The diagnostic failure label combined setup, reply parsing and other errors.
  The green BLEOBD badge measures fresh accepted data, not physical connection.
  Its disappearance and HCI disconnection must not be counted as the same event.
- Cleanup tests sometimes verified an exception rather than verifying that the
  remaining release operations still ran. Fixed-time tests did not simulate an
  atomic snapshot replacement while a reader was performing file IO.

These are coverage/design omissions, not proof that the adapter or RF caused
the failures. Passing the earlier PC suites did not establish physical BLE
reliability. The source of the late stream fragments is still unverified.

## Additional reproduced defects and changes

### Status-file errors could interfere with lifecycle cleanup

A failed disconnected-status write could bypass Link.close(). During manager
cancellation it could also replace CancelledError with OSError, causing the
outer handler to retry rather than propagate the stop. A failed unavailable
status write could terminate the collector instead of reaching its retry wait.

Cleanup now runs in a finally independent of status publication. OSError from
disconnected, unavailable and final stopped status publication is suppressed;
no write failure creates a new owner before the old router exits. An unexpected
status exception still attempts cleanup. Existing per-call and total cleanup
bounds and the user's 100ms asynchronous retry delay remain unchanged.

The fault-injection suite initially failed eight lifecycle cases and passed
the existing cancellation-during-quarantine case. That count is test cases,
not eight distinct production defects.

### A partial release error could prevent later release operations

An exception leaving the notification filter previously skipped StopDiscovery,
Disconnect and Connected verification. Other release exceptions were suppressed
without affecting the reported cleanup success. The old filter test expected
the exception but did not assert the disconnect attempt.

StopNotify, filter exit and StopDiscovery are now attempted separately; a
failure does not skip the remaining phases. Verification requires all attempted
releases and target disconnection to succeed. Failure is reported as unknown,
not as proof that the controller is disconnected. No other target, pairing,
radio policy or BlueZ service is changed.

### Snapshot IO could falsely suppress fresh BLEOBD/HUD data

The UI/HUD passed a timestamp sampled before reading the files. A new atomic
snapshot published during those reads could carry a timestamp newer than that
sample, be rejected as future-dated, and hide valid data until a later read.
The inverse boundary could accept a just-expired value using the earlier time.

Production now samples monotonic time after reading, immediately before
validation. Replay APIs retain explicit time injection. Tests demonstrate a
2ms newer snapshot accepted at the post-read time, while genuinely future and
just-expired records still fail. The 1Hz badge/2Hz HUD read cadence, five-second
power expiry, sixty-second other-field retention and original observation
timestamps are preserved. No additional thread or history is introduced.

## Diagnostics and future regression gates

The legacy failure code remains compatible. A new failure_detail contains only
fixed labels distinguishing reply order/length, setup, device identity, IO,
deadline and dependency categories. Exception text, credentials and payloads
are never included. Error strings outside the bounded exact allowlist become
other; no raw log buffer is added.

The owner-side daily Windows preservation gate now requires response-count,
resync, lifecycle audit and read-clock suites. Their files are protected from
automatic modification. Its source archive also includes ui_state.py for the
production caller check. The existing morning schedule and upstream branch
are unchanged. The runner's push/rejection paths are exercised with mocked
Git/network operations, not a real integration or device update.

## Validation and limits

438 checks pass in the existing Linux PC environment, including OBD lifecycle,
response-boundary, freshness/retention, native-schema HUD rate and resource
checks. The native schema loader needs the existing daily validator's ASCII
temporary-path workaround; source-path loader aborts were environment failures,
not successful test runs. Ruff and whitespace validation are also required
before publication.

Physical BLE reliability, RF/coexistence, actual adapter buffering and recovery
latency remain untested. A later parked 300-second observation should separate
HCI disconnects from collector state transitions, include the fixed failure
detail, compare accepted-data time and check that rejected responses never
renew field ages. Do not claim a reliability percentage or road safety result
from these PC tests, and do not accept/reorder malformed responses to improve
the counters.
