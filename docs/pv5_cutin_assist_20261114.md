# PV5 staged cut-in assist experiment

`Pv5CutInAssist` defaults OFF and is read at process startup. It is limited to real
Hyundai KIA_PV5, CAN-FD camera SCC with openpilot longitudinal control. An offroad
comma restart is required. It does not enable cruise or replace driver authority.

## Two evidence levels

The existing trajectory detector runs once per fresh model update. The experiment
reuses those estimates; it adds no neural inference, CAN subscription, background
worker, recorder or persistent control-loop I/O. Original leadOne/leadTwo, CUT-IN
confirmation, legacy leadCutInRisk and other vehicles retain their existing rules.

1. Caution constrains the raw acceleration request to at most zero, retaining
   the existing value jerk slew. It requires a continuously
   measured front-radar identity, closing range, coherent inward motion and an
   entry forecast ahead of the ego vehicle. It does not promote a weak candidate
   to a full lead or to braking authority.
2. Existing pre-deceleration evidence or confirmed/control-eligible CUT-IN permits
   a bounded negative request. Strong evidence may enter stage two directly; it
   does not wait for an additional caution dwell. The TTC-dependent request uses
   the existing -0.65..-0.25m/s² formula, with the experiment's tighter source
   admission. Current source TTC<=4s typically selects -0.65..about -0.54m/s².

These are PC trial calibrations, not validated vehicle operating limits:

| Caution admission | Trial condition |
| --- | --- |
| Scope | measured frontRadar; vEgo 0.1..35m/s; no lane-change maneuver |
| Range/closing | 2..30m; vRel -8..-0.5m/s; closing TTC<=4s |
| History | at least 0.5s; caution conditions persist 150ms |
| Motion | inward progress >=0.30m to acquire, >=0.20m to retain |
| Coherence | long direction >=0.75, recent >=0.60; recent progress >=0.05m |
| Rates | supported inward >=0.20m/s and reported inward >=0.10m/s |
| Net lateral motion | >=0.60 to acquire, >=0.30 only for an already held identity |
| Geometry | absolute dPath 1..3.2m; forecast improves by >=0.25m; overlap <=1.5s and still ahead |
| Curve | recent absolute yaw <0.02rad/s; existing curve/alias vetoes retained |

New jitter cannot acquire caution. Mild jitter may retain an already coherent
identity only while all retained motion/forecast conditions remain valid. Missing
observations, identity changes, clock reversal/gap, withdrawal, side pass, rear
pass, stationary alias, unstable motion and maneuver vetoes clear the state.
Primary acquisition retires caution unless stronger existing stage-two evidence
still warrants a constraint. State storage is one identity and timestamps.

## Planner, final CAN and feedback

Append-only RadarState fields carry the selected candidate and stage on the
existing topic. Invalid RadarD input clears them; OFF skips their construction.
The planner applies the request through the existing bounded acceleration-ceiling
path. CarController independently rechecks original model/car timestamps within
150ms, live TCS driver-brake admission, valid CAN, Drive, enabled longitudinal PID,
no pedals/override/paddle/parking/hold and no signal-stop mode.

The final camera-SCC generator receives an optional PV5-only ceiling. Stage one
caps raw at zero. Stage two caps raw to the requested bound. Value retains the
entire existing jerk slew, including the positive-to-zero transition, so it can
remain positive briefly after a zero/negative raw request. Neither stage bypasses
this slew. Existing stronger braking,
SCC urgent assistance, acceleration limits and stationary soft hold remain intact.
Pedal/disengagement admission forces final ACCMode/raw/value/StopReq inactive
independently of global lateral enable. OFF/default argument retains legacy bytes.

Existing CarOutput carries the actually emitted constraint stage/source time and
raw-output tracking. Fresh output can track LongControl's last command without
changing Hyundai PID tuning or its P/I/F diagnostics. No new Panda firmware or
steering behavior is introduced.

## Popups and compatibility

- Stage one: normal small popup, no sound, priority LOWEST.
- Stage two: userPrompt medium popup, amber emphasis and existing single-play
  prompt sound, priority LOWER. Both remain below even the first driver-attention
  warning, SCC urgent warning and emergency alerts.
- Only fresh emitted/admitted metadata creates events. The events are warning
  only and do not change engagement; alert persistence is at most 50ms after the
  last event refresh, separate from immediate control invalidation.

The existing comma AlertRenderer and Jetson generic alert contract render text,
status and size. No HUD geometry, icons, atlas, neural package or model pin needs
replacement. Native PC previews use synthetic inputs and Korean catalog strings;
actual device timing/display/audio remain unverified.

## Validation and limitations

The recorded-input radar replay compares OFF and ON using the same model outputs,
tracks and pose. Lead roles and legacy risk matched over 1,197 frames. Three
startup frames lacked referenced inputs and were excluded. In this one case the
first caution candidate was 0.256s before driver braking; the approximate card
helper replay first sent the constraint at -0.237s. The last pre-brake raw/value
changed from +0.45/+0.45 to 0.00/+0.27m/s² with staged assistance alone. Combined
with the separate SCC assist it was -2.09/-0.53m/s². Each variant checked 1,675
packets, CRCs and counters. The first driver-brake packet cleared both assistance
stages and sent inactive ACCMode/raw/value. These are computed CAN requests, not measured ego
deceleration, stopping distance or avoidance evidence.

Stage-two evidence occurred after driver braking in this case and was correctly
blocked from CAN/popups by driver admission. Separate synthetic inputs exercise
promotion, direct stage-two admission, demotion, fresh clear and emergency/driver
priority. The fixed-input helper replay does not rerun MPC, neural inference or a
closed-loop vehicle plant.

Synthetic parallel, lateral-noise, curve-projection, side-pass and receding probes
did not activate either stage. A nearby aborted-entry probe did activate caution
for 3 frames and stage two for 12 frames; its last active sample was at 1.45s,
about 0.15s after the input began moving outward. This is a concrete false or
unnecessary-deceleration risk. The small probe set cannot establish a false-positive
rate or safe deployment calibration. More real non-entry, abort, occlusion,
reflection and sensor-disagreement data are required.

Installed Panda/full-process driver interaction, ECU raw/value response, physical
braking, real audio, CPU/RAM/I/O timing and road behavior are unverified. Keep OFF
until hardware and braking validation. No device access/install/reboot is included.

The broader host regression had 874 passes and 514 passing subtests, plus one
pre-existing parser-startup validity assertion failure, reproduced unchanged on
the baseline. After preserving the entire value jerk slew, the final focused
stage/CAN/SCC/scope/jerk regression passed 287 tests. No claim of whole-repository,
full-process, bench or vehicle validation follows from these host checks.
