# PV5 SCC warning policy: display and audio only

The owner approved separating normal SCC deceleration information from urgent
red/audio warnings. `Pv5SccDecelAssist` remains default OFF with its existing
startup setting and full driver/safety admission. No staged cut-in assist returns.

## Control boundary

`StockDecelAssist`, its original receiver qualification, raw selection, release,
jerk, final SCC_CONTROL generator and LongControl/output tracking are unchanged.
CarController copies a few display fields only after an admitted active SCC packet
has been emitted. These fields cannot feed the acceleration or steering command.
The same accepted original camera observation supplies request/distance/relative
speed and a source timestamp; outgoing lead-display fields are never used.

CarOutput gains append-only versioned metadata: original raw, prior comma request,
candidate distance/relative speed and validity. Missing/expired/version-mismatched
metadata keeps a qualified urgent warning rather than silently suppressing it.
No extra CAN subscription, inference, timer, worker or persistent logging is added.

## Trial warning evidence

These values are PC trial calibrations, not vehicle-validated warning limits.
Every cue first requires the existing fresh/admitted SCC braking-output warning
gate. It cannot engage control independently or survive driver override/expiry.

| Cue | Evidence while SCC assistance is active |
| --- | --- |
| Small, silent | Qualified ordinary deceleration without urgent evidence |
| Urgent | Candidate distance <=10m and closing speed >=0.5m/s |
| Urgent beyond 10m | Constant-relative-speed TTC <=2s |
| Urgent stationary estimate | Own speed >=1m/s, estimated target speed within +/-1m/s, coherent fresh observations >=150ms and estimated braking demand >=1.5m/s² |
| Urgent without relying on target association | Original raw <=-3m/s², or added request magnitude compared with comma >=2m/s² |
| Increased urgency | TTC <=1s or original raw <=-3.5m/s² |
| Unknown metadata | Retain urgent warning, with episode-based audio |

Target fields require the existing display presence values 1/2, finite distance
0.5..150m and relative speed -16.4..34.7m/s. A stationary candidate must retain
coherent timestamps/distance/speed; discontinuity clears its confirmation. Braking
demand is closing_speed² / (2 * max(0.5, distance-3m)). The 3m clearance is only an
alert heuristic and never changes vehicle control or the existing virtual object.

The SCC message does not establish which object caused the deceleration request.
Distance and estimated zero target speed do not prove an in-path stationary car.
No camera-confirmation dependency is added: that could hide the SCC warning when
comma misses a hazard. Severe request/additional-deceleration fallbacks retain
warnings even if the distance belongs to a different displayed object. Conversely,
these fallbacks can still warn without a real collision threat. False warnings and
missed/late warnings remain possible and require broader recorded/hardware tests.

## Audio and visibility

One risk episode uses the existing single-play prompt WAV. Its audible event is
advertised for 150ms so the existing 20Hz sound loop can receive it; the sound
engine completes one play. Continuing red display then uses a strictly named
silent warning variant. Urgency above the already-announced episode peak, or renewed risk after at least 1s clear,
may start another sound. This identifies risk episodes, not unique physical cars.
There is no global cooldown that mutes unrelated FCW/AEB or driver/system alerts.

Urgent display may bridge 250ms of risk-threshold jitter only while SCC braking
output remains fresh/admitted. Pedals, disabled control and stale data clear it
immediately. All these events are WARNING only. Small information is below driver
warnings; urgent priority remains the previous SCC priority, below existing FCW.

Soundd normally adds a fallback sound to any critical silent alert. Only the exact
`pv5SccDecelSilent/warning` variant is exempted. Other critical/driver/timeout
fallbacks and steering attention remain. Navigation countdown does not substitute
its sound for this intentional silent red state. No WAV or global gain is changed.

## Validation scope

Fixed recorded plans, ego and perception are replayed through the existing actual
CarController SCC helper, LongControl, HyundaiJerk and final pack/decode functions.
This approximates card timing and does not rerun full processes, neural inference,
MPC or a vehicle plant. Before/after full packet hashes match for all 1,675 packets
in each ON/OFF variant. CRC/counters, driver-brake clear and PID tracking match.
This packet comparison covers SCC_CONTROL with recorded HUD inputs; separate
vehicle warning-display bits may change with the approved alert presentation.

The prior cut-in case remains urgent before driver braking even though the SCC
displayed distance is about19.4m: its added deceleration request supplies the
fallback. A strict 10m gate would have suppressed this early cue. This one case
does not measure nuisance-warning reduction across real driving.

PC tests cover ordinary/near/receding/far-TTC/stationary/conflicting/unknown data,
source identity/freshness, episode jitter/rearm/escalation, native alert metadata,
single-play PCM, silent red persistence, other critical and timeout fallback.
Actual speakers, ECU/Panda response, CPU/USB latency, road warning timing and
false-positive rate are unverified. No vehicle access/install/restart/activation.

The final focused warning/SCC/jerk/scope/removal group passes 274 checks. Another
51 existing pure PCM, DM, steering-attention and navigation tests pass against the
exact current sound methods; their hardware/module import setup is isolated.
Asset-loading, speaker and live messaging integration tests are not part of that
51-check group. All five pinned HUD bundles retain compatible generic renderers;
native comma/HUD previews use synthetic data and do not test real display/audio.
