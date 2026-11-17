# PV5 SCC override cancellation and following warning refinement

Baseline: c6af7ed5820a3c74853abe05cc7311d65212ee24. Local PC verification only.

## Cancellation provenance

CarControl.CruiseControl appends pv5StockCancelOnly at ordinal 5. controlsd retains
its existing cancel value and tags only eligible PV5 camera-SCC non-PCM cancellation
while enabled, without a cancel/main button event. Old/missing tags remain false.
The consumer rechecks vehicle scope, engagement, override and driver/input gates.
Actual cancel/main edges latch override denial on every card tick until disabled
is observed, including ticks without an SCC transmission. The latch does not alter
steering or ordinary actuation admission. Mode 2 retains zero Raw/Value/StopReq/
acceleration bands. Mixed versions fail closed; update schema/producer/consumer
together through the normal Git release path.

## Warning-only downgrade

The original close-target urgent branch is retained by default. Downgrade requires
one second of coherent same radar ID observations from the already subscribed
radarState, age <=150ms, inter-sample gap <=200ms, position prediction residual
<=1m and relative-speed step <=1m/s. SCC distance must agree within max(1m,15%)
and relative speed within 0.75m/s. Additional conditions: distance >3m, headway
>=1.2s, TTC >=4s, required deceleration <1.5m/s^2 (existing 3m reserve), ordinary
request severity. These are trial calibration limits, not certified safety limits.
Missing, incoherent, new/vision-only tracks preserve the urgent path. Strong raw
requests, additional demand, short TTC, stationary-risk, fresh-input expiry and
unknown SCC metadata behavior remain. No added delay in escalation. Existing
250ms visual hold and episode audio/rearm remain. Radar failure is not a new
control-readiness dependency. Lead identity agreement is supporting evidence,
not proof that two sensor observations are the same physical vehicle.

## Validation

833 PC tests passed; no failures/skips. Real schema transport and compiled
unchanged Panda safety hooks included. Existing mock Params, IPC replacements
and PC native hooks do not qualify the vehicle installation.
Selected ordered log replay: 83/157 override packets, mode transitions 9/17 ->0/0;
all longitudinal demand zero and valid CRC. Assumptions: inactive unrecorded
paddle state and fixed jerk; no ECU feedback simulation. Warning replay: ordinary
case 177 urgent ticks ->0, high-speed/new-target case 181->181 with identical
first warning time. Prior close-cut-in classification evidence 94 rows unchanged;
this is not a replay of full warning activation in that recorded gas override.
No safety/actuation source change beyond the strictly zero-demand override mode
admission. StockDecelAssist, source validation, packet_allowed, warning_active,
CAN generator and stopping source remain unchanged. Initial sparse headers/module
and fixture failures were resolved. An initial storage-order replay was invalid;
results here use logMonoTime-ordered input, not original process delivery order.

No device deployment, push, receiver update or existing job interruption performed.
