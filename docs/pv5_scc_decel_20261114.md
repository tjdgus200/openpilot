# PV5 stock-deceleration experiment

`Pv5SccDecelAssist` is default off and read once when CarController starts.
It is scoped to Hyundai `KIA_PV5`, CAN-FD camera SCC, real-car operation and
openpilot longitudinal control. Changing it requires an offroad restart.

## Input and selection

The controller reads the existing camera parser's accepted `SCC_CONTROL` payload,
timestamp and counter. It does not subscribe to another CAN stream, use the TX
echo, infer a collision cause or mutate the OEM RX template. CRC, an explicitly
consecutive counter, mode/fault checks and a 100ms age limit qualify input. Two
accepted consecutive observations prime a new source. A new rejected packet
withdraws the cached observation through the parser's observation-only
`last_invalid_nanos`; parser acceptance and other vehicles' validity behavior do
not change. Multiple accepted frames within one existing parser batch are checked
as a sequence, so receiving two frames in one card tick is not treated as loss.

During admitted PID driving, raw at or below −2m/s² enters priority without vision
confirmation or a negative-value gate. Select the stronger of comma and stock
deceleration. After entry, negative raw governs; if raw is nonnegative while value
is negative, value becomes the release target. Both fresh requests must remain
nonnegative for 200ms before normal handover. Increasing the selected acceleration
is limited to 1m/s³; stronger negative requests are not delayed at the raw target.
Commands retain −4..+2.5m/s² bounds. The existing CAN value slew remains separate;
assist uses a deceleration-jerk limit of 5 and release ceiling of 1m/s³.

Ordinary moving admission requires active longitudinal control, fresh accepted
TCS driver-brake input, valid CAN, Drive and no pedals, hold/parking interlock,
paddle request or cruise override. Disengagement forces final ACCMode, StopReq,
raw and value inactive/zero independently of global lateral enabled. Existing
stationary soft hold and normal stop controller behavior are retained; this
experiment does not replace them. Expired or invalid stock input drops priority
and returns to the ordinary comma request rather than holding old braking.

## Feedback and warning

Append-only actuator telemetry carries enabled/active and original source time
through existing CarOutput. Only a fresh, in-scope active output can update the
LongControl last-output tracking state. Fixed Hyundai Kp=1, Ki=0, Kf=1 and P/F
diagnostics stay unchanged; no perception/MPC feedback is fabricated.

The same fresh negative output creates a warning-only event: critical/full red
alert, explicit exclamation and existing sound. It has lower priority than
highest-priority collision/driver/system alerts, does not change engagement and
expires promptly when refreshed evidence stops. Existing comma and paired HUD
critical rendering is reused; model packages, assets and neural interfaces do not
change. Actual device display and audio remain hardware validation items.

## Validation boundary

The PC candidate was exercised with recorded inputs and synthetic fault/driver
conditions. Production-path checks cover source corruption and counter continuity,
arbitration, invalidation, packet admission and disabled/other-vehicle parity.
PC code and CAN checks are not vehicle braking or road-safety validation.

Installed Panda firmware and the complete receive-hook/driver override chain are
unverified. The existing source safety hook can enable `controls_allowed` from
outgoing active ACCMode; this change gates the opt-in final packet but does not
rewrite or flash that safety firmware. ECU raw/value selection and pedal blending
are also unknown. The earlier raw-following response curve is an unvalidated
model assumption, not evidence of ECU behavior, stopping distance or avoidance.
Keep the feature off until hardware and braking-response validation.

## PC release checks (2026-11-14)

- 606 tests and 514 subtests passed. One `test_parser_can_valid` failure is also
  present on the unchanged a07972357 baseline: its initial-validity expectation
  conflicts with this fork's existing startup grace. That policy was not changed.
- 96 synthetic CAN golden cases generated from a07972357 matched byte for byte
  with the feature disabled, across speed/enable/override/stopping/brake/hold.
- Recorded-plan PC replay exercised the actual SCC helper, LongControl tracking,
  jerk helper and final CAN generator/decoder: 1,675 packets per variant, with CRC
  and counter continuity verified. It approximates card timing with recorded CC
  and TX ticks and does not run full card, perception/MPC or an ECU.
- In this one fixed-input case, priority began 0.199s before the recorded driver
  brake. Last pre-brake raw/value changed from +0.45/+0.45 to -2.09/-0.51 m/s²;
  the first brake-observed packet at +2.8ms had raw/value/ACCMode all zero.
  These are computed CAN requests, not measured vehicle deceleration.
- Source/driver fault cases, default-off/other-vehicle exclusion, output schema,
  warning priority and settings contracts are covered by PC tests. Whole-system
  device startup, installed Panda, ECU response, display/audio and road behavior
  are unverified. No device was accessed, installed, restarted or auto-enabled.
