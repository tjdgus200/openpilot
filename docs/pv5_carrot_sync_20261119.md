# PV5 daily source integration — 2026-11-19

Target: `7c008e585ed99859a6a5037a8097af7bd3ece919`.
Reviewed upstream: `a9811adce075a81f6a719e611f28ae444193f750`.
Merge base: `682ceeb484f93b2017f08548afbd18e452c291d5`.

## Selected changes and admission

* **Partial** `e676f501b2ba6d7b2046a32f779af3410471bae9`: reuse decoded
  vision association fields, paired-front history metrics and projection within
  one detector update. Preserve observations, identity, probability thresholds,
  per-point missing/nonfinite velocity fallback and ordinary release. The
  detector defaults OFF and the controller opts in only through its existing
  `pv5_refinements` admission. Live and complete-consistent-metadata replay
  already restrict that admission to `hyundai/KIA_PV5`. Unknown and other cars
  retain the original association and repeated metric/projection calculations.
  Upstream universal activation, CI edits and remote owner records are excluded.
* **Partial** `a9811adce075a81f6a719e611f28ae444193f750`: exact upstream
  constant Gather implementation and its 32 selection/JIT tests. Consecutive
  normalized indices are coalesced without sorting or removing repeated rows.
  Shared compiler semantics remain identical; this is not a new vehicle policy.
  Additional target-reference tests cover integer/float dtypes, negative axes,
  boundary/negative/repeated indices, nonfinite values, tensor indices and
  existing invalid-input rejection. No model weights, catalog, saved selection,
  compiled artifacts or Jetson packages change. Cheddar evaluation, CI changes
  and remote owner records are excluded; no new model is enabled.

Neither upstream commit is recorded as fully applied. The previous cumulative
44 applied SHAs are retained as history, not reapplied or newly claimed.

The current task explicitly preserves PV5-only coherent vision transition:
default OFF, existing live/replay vehicle admission and cache version 8 remain.
Older all-vehicle wording in the November 9/10 documents and checklist is not
authority to broaden this task. The new admission assertions cover both live
and replay constructors without weakening any existing assertion.

## Preservation boundary

All 13 owner requirements remain: BSD union/freshness/priority; actual Jetson
statistics; battery screen and stationary dwell; registered OBD transport and
bounded reconnect; observation TTL; Q92/FPS/model priority; zero USB input
current; initial parked update admission; verified USB cleanup; optimization
defaults; strict OBD recovery; finite/join/coherent-warp defenses; connected
park-update admission and boot display. No protected file is changed.

All five paired package identities and their distinct source commits, default
N16 and reboot-frozen selection remain. Latest SCC override/warning behavior,
withdrawn cut-in assist, lane-edge/steering/exposure policies, DM/sound/HUD,
OBD settings/preview and parked recording remain outside the diff. No settings
are written. No CAN sender, firmware, vehicle install, restart, push, schedule,
owner validator or operational policy is changed.

## Local PC evidence

* Focused radar/PV5 admission/continuity/stationary/cut-out/MPC preparation and
  Gather regressions: **1,224 passed**, no failures or skips.
* Gather suite including target-reference boundary tests: **64 passed**; 32
  overlap with the focused suite. Python device/JIT only, not QCOM execution.
* Model catalog/runtime/warmup/serialization/output recovery: **114 passed**.
* Independent archived target controller comparison: 6 radar modes, both
  initial sensor preferences, 14 synthetic scenarios, 100 frames each, in both
  PV5 and non-PV5 scopes. **16,800 frames per scope**, zero complete-output or
  trajectory-history differences. Includes paired corner/front slowing targets,
  replacement IDs, missing/unmeasured tracks, repeated/reversed time, motion
  pipeline reset and ordinary release.

Final owner Linux/Windows preservation gates and the full five-model checklist
run against the committed HEAD. Their exact commands, HEAD-bound JSON results,
integrity evidence and logs are in the runner's `20261119-070002-efebf5` run
directory (`apply-report.json`, `validation.json`, `obd-validation.json`,
`model-checklist.json`). These reports determine pass/fail; this note does not
predeclare their result.

Initial local harness errors were corrected: deprecated `PYTHON=1` replaced by
`DEV=PYTHON`, missing sparse-checkout web source materialized from HEAD, and the
paired fixture uses the existing `_reset_motion_pipeline` method. Production
code/tests were not weakened to bypass those errors. An automatic Git GC attempt
reported an unrelated unavailable tree; subsequent commands disable automatic
housekeeping. This is not a successful full-repository object-integrity audit.

The full checklist also exposed an existing fixture error at the pinned target:
the two FOV AST fixtures extracted through `traffic_convention`, thereby taking
the new parked-recording `continue` out of its loop (402 syntax failures).
Both fixtures now stop immediately after the unique production
`calibration_updated or wide_fov_changed` transform block. All FOV poll,
calibration, transform and submission statements and assertions are retained;
no modeld or parked-recording behavior changes. The original target reproducer
and failed checklist log are retained in the run directory. Missing monitoring
sources were also materialized from HEAD before checklist execution.
The two repaired FOV fixture suites pass all 496 cases without skipped tests.

## Deferred scope and limits

Keep the prior detailed deferrals. New indicator commits `2b202df1`, `6af3073e`,
`acb8816f`, `171f80bf` remain tied to deferred Kick/SPAS/native ownership and
require separate CAN/driver/freshness/package validation. `b5df7d4d` adds
per-frame cruise wall/thread CPU measurements and changes the diagnostic peak;
its cost/policy and latest PV5 SCC integration are not validated here. EV4
`693efb88`, `e3a04bc8`, `e48fff20` are outside vehicle scope. Cheddar review
`446fa796` grants no model selection or package authority. USB resident-wait
`dcd3392e`/`b1285593`, unrelated eGPU `fe2ed6ee`, B1/firmware/global cloud/modem,
StopGo, automatic EFI/image changes and official package replacement stay
deferred with their earlier dependencies.

No archive was republished or installed. Synthetic replay and Python CPU
tests cannot establish actual sensor correctness, QCOM shader execution,
TensorRT performance, 20 Hz operation, P99, BLE/USB reliability or road safety.
No native MPC optimizer or physical braking test was performed.
