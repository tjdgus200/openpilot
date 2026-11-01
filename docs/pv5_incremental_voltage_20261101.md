# Incremental PV5 voltage display

The collector publishes each strictly validated voltage DID page as soon as that
transaction completes. The fixed108 slots fill in32,32,32,12-channel groups from
220102,220103,220104,22010A. The adapter does not return individual-cell replies;
no per-cell queries, additional requests or faster BLE polling are introduced.

Missing pages and invalid voltage slots remain None/-- at their original indices.
The39-byte page length, source, DID, ISO-TP sequence/count and ELM prompt checks
are unchanged. A partial BLE notification or malformed response supplies no value.
Fresh Park admission, core-power priority, adaptive pacing, the2.5-second page
assembly bound and minimum four-second optional scan-start spacing remain.
Timeout/resynchronization retires an unfinished assembly; an already validated
page keeps only its original observation age. Leaving Park retires the collector's
optional assembly and values through the existing admission check.

Four page timestamps accompany the108-slot array in the local bounded4096-byte
RAM snapshot. The comma telemetry publisher retains at most four immutable page
slices. A newer page replaces only its own indices; it cannot renew another
page's timestamp. Previously received pages expire independently after60 seconds,
and configuration/vehicle/page changes clear ownership as before. Power keeps its
separate five-second expiry. The existing BLEOBD freshness indicator is unchanged.

The HUD wire remains version1 with108 values/None and a conservative age equal to
the oldest retained page. Local page metadata is not transmitted. Existing Jetson
packages already render incomplete arrays, so this change needs no receiver or
model protocol update. The existing2Hz RAM snapshot read may coalesce pages that
arrive within one interval; it no longer deliberately waits for all108 channels.

PC tests exercise producer -> atomic RAM snapshot -> publisher -> existing HUD
decoder, progression32/64/96/108, next-generation partial replacement, fixed-index
holes, stale/future/invalid timestamps, replay, independent expiry and ownership
reset. Existing parser/recovery tests remain required. These checks do not
establish physical BLE stability, display latency or driving safety.
