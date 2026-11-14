# PV5 confirmed cut-in notice and comma DM layout

This replaces the PV5 continuous cut-in risk/tracking/braking **display** notices.
It does not restore the removed `Pv5CutInAssist` experiment or change radar,
vision, lead selection, planner, CAN, driver admission or SCC actuation.

## Notifications

The existing `radarCutin` event still requires a detected radar cut-in candidate
to match the selected `leadTwo`, including track ID and position/velocity match.
The existing physical-object re-ID deduplication is unchanged. In real PV5 only,
the event now shows **차량 끼어듦 감지됨 / Vehicle cut-in detected** for 1.5 seconds,
small, normal status, low priority and no audible or visual vehicle command.
DM, FCW, SCC urgent braking and takeover warnings retain their higher priorities
and sounds. Other vehicles keep the prior `radarCutin` engagement chime.
The popup reports an algorithmic detection, not certainty that a vehicle is
actually cutting in. The original detector can still miss or misclassify objects.

The PV5 comma and live Jetson HUD adapters omit only the old `cutin`,
`cutinTrack`, `cutinBrake` notices. Lane-change, ordinary lead braking, navigation,
steering and camera notices retain their existing predicates and lifetimes.
The confirmed event is delivered through the existing `selfdriveState` alert
contract; no new enum, subscriber, inference, worker, timer or logger is added.
Existing generic atlas/legacy notice rendering remains compatible with old data.

## DM layout

Native C3X rendering reproduced a 600px right inset on PV5 DM stages 1 and 2,
followed by a center/fullscreen change at stage 3. The PV5 C3X noncritical DM
panel is now horizontally centered, at most 1660px wide, above the existing
largest lower-left device/speed box with a 20px gap. It retains its height,
icon, text, text fitting, trigger conditions and audio. A dark nearly opaque
panel preserves text contrast over bright camera frames. Fullscreen critical
alerts, other vehicles and the separate compact renderer retain their layouts.

## PC validation and limits

Compare with `cf1703617110b3d1de7fe215782c9be6eefa7230` at identical inputs,
2160×1080 native C3X rendering and existing 1920×480 packaged HUD rendering.
**152 focused tests and 157 existing SCC control tests passed, 0 failed/skipped.**
Focused tests cover the PV5-only alert callback, other-car/notCar fallback,
existing promotion/re-ID behavior, both popup consumers, 6 DM geometry cases,
and existing SCC warning/audio/UI policies. Native Korean and English renders
cover both DM families at all 3 stages. Five model-paired packages retain model
hashes, updater handoff, automatic Super setup, HUD recovery and existing guards;
published package pins are updated only after isolated native checks and public
asset SHA-256/size verification.

No device connection, installation, restart or activation is performed. PC
renders do not establish on-device placement under every configuration, alert
false-positive/miss rates, speaker behavior, CPU timing or road safety. The
separate approximately five-minute warning still needs its exact text/photo/time;
this change does not suppress it.
