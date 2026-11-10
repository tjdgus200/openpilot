# PV5-only coherent vision handover — 2026-11-09

**Superseded scope,2026-11-10:** the owner explicitly requested the common
Carrot policy for every vehicle using this DPath path. The new implementation
removes the PV5 admission switch and matches upstream. See
[current policy and verification](carrot_vision_transition_20261110.md).
The PV5-only scope and disabled/default parity results below are historical.

## Scope and provenance

The owner explicitly requests item1, the0.20s coherent-vision lead handover,
and its required dependencies, restricted to KIA_PV5. This supersedes that
item's deferral in the2026-11-08 WIP integration. It does not authorize other
vehicle changes or the unrelated eGPU recovery commit `fe2ed6ee`.

Reference: `44306cae4e62fb76fac683c911113cdb2a72d4dc`, originally investigated
on an ID.4. The entire `radar_motion` directory at PV5 baseline `27a48e11`
is identical to that reference's parent. Its matcher/predictor/selection,
stationary confirmation, kinematic association and timing dependencies are
therefore already present. Reviewing later commits through the fetched WIP
head `0df83618f196357165a29218c9a588b0131c7d20` found no subsequent
modifications to `radar_motion`. There are no further
missing dependencies to import for this policy. ID.4 validation labels and
other-platform changes are not imported or reported as PV5 evidence.

## Runtime and replay

* The generic controller defaults to the existing policy. The live RadarD
  constructor enables confirmation only for brand `hyundai`, fingerprint
  `KIA_PV5`. No setting, new worker, additional inference or camera warp changes.
* A currently accepted, measured central front-radar lead supplies its **current**
  distance, speed and acceleration while its proposed visual replacement is
  confirmed for0.20s. This is not a frozen previous output or a timer added to
  every brake request. Modes1/2/3 use this path; SCC-only/vision-only modes keep
  their existing behavior.
* Confirmation uses camera measurement time, not wall-clock or radar wake count.
  Visual longitudinal/lateral/speed discontinuities restart it. Observation
  during normal matching is included so existing confirmation is not followed
  by another unconditional0.20s wait.
* A normal match to another radar target transfers immediately. No accepted
  healthy previous front lead means no new visual confirmation hold. Current
  independent radar-pair cut-in and downstream leadTwo admission are preserved.
* Missing/unmeasured/changed/discontinuous/off-path old radar, invalid model
  path, stale or reversed time revoke retention. The original0.15s observation
  gap,0.5–180m range,1m path bound and2.5m/0.75m/3m/s continuity checks remain.
  Stopped radar additionally requires the matcher's confirmed stationary identity.
* Offline replay carries the log's brand/fingerprint into each frame, including
  frames before a late carParams message. Unknown/non-PV5/mixed metadata do not
  opt in. Cache version5 and existing source fingerprints invalidate old results.

All five model choices and internal/Jetson inference use the same comma RadarD
policy. Jetson model packages, model tensors, recurrent state, HUD, steering
limits, BLE and stored vehicle options are unchanged.

## Validation and remaining limits

`test_pv5_vision_transition.py` preserves all upstream transition regressions,
including the changed moving/stationary range expectations, and adds vehicle
scope, late carParams loader, replay and full-schema radar-to-MPC preparation.
Existing non-PV5 regression expectations are kept unchanged.

The MPC tests execute production lead extrapolation and update/preparation with
a recording solver. They check schema compatibility and finite optimization
inputs; they do **not** run the native optimizer or model physical braking/EPS.
Paired synthetic replay compares complete output structures with the previous
PV5 code when disabled and with the entire upstream algorithm when enabled.

The actual PV5 NAS route share was unavailable during this task. Recorded-road
replay, a deployed NAS result page, actual control response and road safety are
not established by the PC tests. The matching replay adapter is in this source
change; no other maintained branch or shared NAS deployment is altered merely
to deliver this PV5-only vehicle change.

**Trade-off:** a new nearby car visible only to vision can be selected later.
At a15m/s closing speed,0.20s corresponds to3m of additional relative travel
before that selection, excluding other pipeline delays. This arithmetic is not
a measured braking distance. Repeated inconsistent visual estimates can keep
restarting confirmation while the old radar remains admissible. This port does
not claim to solve close cut-ins, and has no new blanket emergency bypass.

Final PC validation:

| Scope | Result |
| --- | --- |
| Mandatory preservation gate, all five models, with new radar/MPC suites |1,538 passed,0 failed,0 skipped |
| New transition/scope/loader/MPC-preparation cases, included above |116 passed |
| Existing corner/corroboration/cut-in/cut-out/SCC regressions forced to PV5 ON |429 passed |
| Cruise/lateral admission and fast-radar/preview control regressions |402 passed |
| Complete output equality, default/other-vehicle path vs27a48e11 |7,200 synthetic frames,0 differences |
| Complete output equality, PV5 path vs original44306cae |7,200 synthetic frames,0 differences |
| Ruff for changed runtime modules and new tests; Python compilation |Passed |

Counts overlap; they are not added into a single independent-test total.
Source/model-bound JSON and paired-replay evidence are retained in the local
`workspace/pv5-vision-transition-20261109` task directory. The committed clean
source is rechecked before push. No vehicle installation, option write, reboot
or shared-service update is performed. Local tests do not establish remote CI.
