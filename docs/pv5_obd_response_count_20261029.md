# PV5 OBD: bound fixed replies at the ELM

Two owner-provided passive observations on2026-10-29 show the same failing
`220101` transaction:16 CAN frames, two first frames and322 ASCII bytes. The
first9 frames reconstruct a valid62-byte response, followed by7 frames of
another response before the terminal prompt. Those extra frames are not
byte-identical duplicates. The next collector transition is disconnected.
This establishes one software-triggered reconnect path; it does not establish
why the second response arrives or rule out independent radio/driver failures.

Keep the strict parser. Taking only the first valid payload would hide mixed
transactions or late replies. Instead, append the ELM327 optional response-line
count to each already-allowlisted fixed DID request:

| Logical request | Complete payload bytes | CAN frames | Wire request |
| --- | ---: | ---: | --- |
|220101|62|9|2201019|
|220105|46|7|2201057|
|220106|38|6|2201066|
|220102/220103/220104/22010A|39|6|same DID followed by6|

These forms also occur in the owner's supplied Car Scanner log. They follow
the [ELM327 datasheet](https://www.elmelectronics.com/wp-content/uploads/2016/07/ELM327DS.pdf),
printed pages32 and56: the optional single hexadecimal suffix limits the number
of CAN response lines and returns the prompt after that count. The count must
cover the full response; underestimating can stop CAN acknowledgements early
and cause retransmission. It is not a BLE retry count or a new ECU command.

Derive counts only from the existing exact response contracts:6 payload bytes
in the first frame, then7 in each consecutive frame. Reject contracts that
cannot be represented by one hexadecimal count instead of truncating the count.
ATI and all AT setup bytes stay unchanged. Logical request IDs, exact length,
source, DID, sequence and prompt validation stay unchanged. An adapter ignoring
the suffix still causes mixed replies to be rejected. Queued late data still
blocks the next write; nothing is flushed into another request or session.

No new query, timer, worker or history is introduced. The one-request scheduler,
adaptive gap, timeouts, target identity, bounded cleanup,100ms unlimited retries,
fresh-P cell admission, confirmation and per-field expiry remain. This is a
comma-only change; HUD packets, selected Jetson package and model/control code
are unchanged.

The synthetic adapter reproduces the322-byte rejection for the old bare request
and emulates the documented count limit for the candidate. Tests compare exact
payloads for all7 DIDs under BLE fragmentation and cover ignored counts, short
or wrong responses, NO DATA and late data crossing a request/session boundary.
This is PC protocol/fault testing, not a measurement of physical reliability,
CPU load or road safety. After a normal parked software update, repeat the
passive observer and a longer capture to check sequence failures, receiving
uptime and reconnect counts before calling the physical issue resolved.

Validation:328 Windows PC tests pass across OBD transport, response counts,
reconnect, adaptive scheduling, retention, badge, battery HUD/heatmaps/stationary
view and the owner-protected model optimization-default gate. Ruff and Git
whitespace checks pass. Test collection initially needed the local Jetlink
library added to PYTHONPATH; that environment error was corrected before this
passing run. No device was accessed or updated by these checks.
