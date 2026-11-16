# PV5 angle steering: optional torque-estimator readiness

## Problem and change

A model-input gap can invalidate calibration and downstream services. In two
stored examples, livePose input validity recovered after about 317/324 ms, while
liveTorqueParameters remained invalid until about 622/730 ms. Its useParams flag
was false, but selfdrived still treated it as a required readiness dependency.

At selfdrived startup only, exempt liveTorqueParameters from aggregate validity,
aliveness and frequency checks when all of these static CarParams fields hold:
real car, hyundai brand, exact KIA_PV5 fingerprint, angle steering, and pid lateral
tuning. The actual Hyundai interface produces angle/pid with the LFA_ALT frame;
without that detection it produces torque/torque and retains the checks.
The runtime useParams/valid flags never select this exemption. Unknown cars,
deprecated tuning, notCar and inconsistent angle/torque configurations retain
baseline checks. The service stays subscribed and torqued remains running and
subject to existing manager process checks.

No model, pose, calibration, liveParameters, camera, driver monitoring, Panda,
CAN, process, sensor, actuation, alert minimum-duration or timeout policy changes.
The 200-cycle controlsMismatch threshold and three-second soft-disable timeout
are unchanged. No settings, new subscribers, threads, timers or logging.

## PC verification

142 focused tests passed, zero failed/skipped in the final run. These execute
production subscription setup, SubMaster health methods, event predicates and
state-machine code using socket-free fixtures, real cereal schemas, and the
actual Hyundai CarInterface with in-memory Params. They cover exact scope,
required-service validity/aliveness/frequency failures, blocked engagement,
persistent-fault disabling, recovery, Panda/RX checks and sensor timeout.
Native device IPC, full process scheduling and vehicle operation were not tested.
Three pytest plugin configuration warnings remain in the PC environment.
An initial six test failures used nonexistent legacy tuning schema names; the
fixtures were corrected to indiDEPRECATED/lqrDEPRECATED before the final run.

Historical source: be8c40120650fa234bfc0ecbd85682d6647eb0be.
Patch baseline: 12d62d3b556b7d96bba106be46dba62fa16fc5c0.
Selected existing logs were replayed at recorded selfdrive publication times:

| Example | Recorded softDisabling | Baseline replay | Patched replay |
| --- | ---: | ---: | ---: |
| A | 620.579 ms | 615.902 ms | 318.539 ms |
| B | 721.593 ms | 713.794 ms | 313.497 ms |

Both replays retain the initial fault and wait until required inputs recover;
enabled/active remain true in these short episodes. Baseline boundary differences
from recording are 10.090/7.800 ms. Publication times approximate subscriber
arrival; unobserved services are assumed healthy, consistent with the recorded
communication snapshots. An angle/pid PV5 CarParams fixture is assumed because
CarParams was not recovered from the selected historical headers. These are
conditional counterfactual results, not exact deployed behavior or ECU feedback.

This does not repair the initial model-frame gap. It does not promise the visible
warning disappears after 319 ms: existing alert hold times are unchanged.
No vehicle connection, installation, reboot or safety qualification was performed.
Static checks: git diff --check passed; Ruff could not run because no installed executable was available.
