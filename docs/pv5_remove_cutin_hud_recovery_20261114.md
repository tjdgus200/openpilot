# PV5 staged cut-in removal and HUD budget recovery

The owner reported frequent unwanted activation and alerts from `Pv5CutInAssist`
and requested its removal while retaining `Pv5SccDecelAssist`.

## Removal scope

Remove the staged experiment's setting, runtime detector, planner ceiling,
CarController/final-CAN constraints, output tracking and both new popup events.
An old saved ON value has no consumer. Original lead selection and legacy
`leadCutInRisk` remain unchanged. Stock-deceleration sources, qualification,
selection, jerk, driver admission, tracking and urgent warning are preserved.

Keep only the previously appended wire ordinals dormant so old logs remain
readable. They default false/zero and are not emitted by live feature code.
This is not a removal of stock FCW/AEB, legacy cut-in detection or SCC assistance.

## HUD failure reproduced on PC

The earlier repair recovered only an exited owned snapshot publisher. An alive
publisher and working model link could still leave the HUD waiting indefinitely.

`IdleBudget.observe()` learned a conservative peak cost for snapshot reading and
sending. It reduced that estimate only when more display work was observed. One
slow observation could make `allowed()` reject all later calls; then there were
no observations to reduce the old estimate. A 20Hz model/30ms exchange PC fixture
with one 20ms read and/or write reproduced zero sends for the next 12 seconds.
This is a confirmed source failure path, not confirmation of the device's cause.

The existing idle poll now halves quiet HUD read/write cost estimates at most
once per two seconds, retaining the 2ms write floor. New observations immediately
restore the measured costs. There is no monitor thread, timer, worker, IPC topic,
logging stream, process restart, USB reset or vehicle access added by this fix.
The original model-ready checks before read/send, nominal 4ms model reserve,
overload skip, window deadlines, source expiry and media policy remain intact.

This provides bounded opportunities to retry after transient cost spikes. It is
still an estimate: actual USB or filesystem operations can exceed their estimate,
and synchronous framed writes cannot be interrupted safely. PC scheduling checks
do not prove physical USB latency, CPU cost or real-time safety. Persistent model
load continues to suppress HUD work instead of forcing a display transmission.

The same synthetic schedule resumed after 2.0802s for a single slow read or write,
and after 4.0802s for both; before the change each case sent zero frames in 12s.
The combined removal/SCC/scope/HUD priority group passed 322 checks. A separate
HUD recovery/cache/freshness/FPS/timing group passed 250 checks with three existing
pytest configuration warnings. The groups overlap and are not a unique test total.
Eleven stock-SCC/control/radar source files exactly match the preceding SCC-only
release 686ac31b1. Existing neural/model/HUD package pins and wire formats remain.

## Periodic warning

The user described a roughly five-minute `loadd`-like warning. That name does not
identify a unique configured process in this source. The exact displayed text and
associated current diagnostic record are needed before changing its cause.
Existing process/lag warnings and their disengagement behavior are not suppressed.
The older one-minute drive recording is not proof of this new intermittent issue.

No vehicle installation, restart, activation or device-side verification is part
of this PC change. Existing external simulations and incident work remain intact.
