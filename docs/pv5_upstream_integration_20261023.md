# Selective carrot-nav integration into PV5

Base: PV5 `59fdb551`. Upstream reference: carrot-nav `819ce67c`.

## Warp and USB watchdog

Import the invalid-destination guard from `f1a9626c` and its tests: reject
read-only, wrongly shaped/typed or non-contiguous destinations after draining
the already submitted GPU operation. Valid inputs retain the existing direct
copyout completion fence. Import `cd986d7e`'s watchdog wake coalescing with its
deadline, rearm, cancellation, concurrency and close tests. No asynchronous
USB write, timeout extension or model input/output change is introduced.

## Existing signal observations on the PV5 USB HUD

Import `54e706fa`'s allowlisted metadata, remote signal reader and real camera
capture metadata. Use the existing USB HUD cadence: at most 4 KiB, 20 tracks,
no extra image, detector, inference worker or data channel. Drop optional
signal metadata first when the existing 96 KiB snapshot budget is exceeded.
Setting initialization failures disable this optional field instead of
interrupting the existing battery/BSD/vehicle packet.

Keep `HealthRenderer`, screen 8, automatic minimum brightness, power/temperature
badge, resource footer and physical BSD walls unchanged. Camera signal boxes
are drawn in the world pass before the HUD; the existing signal status ring
reuses the speed-block location. BSD overlays retain the outer-third gradient,
full-height bars, freshness and OEM OR local-ONNX source; vehicle alerts draw
last. No signal observation is fed into vehicle control by this transport.
No observer setting is enabled automatically. With the existing display mode
off or observation input absent, this feature does not invent a red/green result.
The normal road-camera preview is required for aligned boxes; 3D-only mode
can display the status ring without starting the preview encoder.

Do not select the upstream signed HUD bundle: it lacks PV5 customizations.
Publish a new committed PV5 bundle and preserve SHA/size/model/runtime checks,
Park/zero or offroad installation permission, activation probe and rollback.
Source-only comma updates cannot update the already installed Jetson renderer.

## Separate Hyundai CAN-FD trial

Import only code/tests from `25ac1af7`. Camera SCC with an existing stop
controller changes HUD_LEAD_INFO (OEM SCC_ObjSta) from 0 to 2 only after final
ACCMode=1 and StopReq=1. Preserve real lead information, absent-lead distance
and speed defaults, received CAN, departure clearing and interlocks. The
non-camera builder is unchanged. This is a real outgoing CAN behavior trial,
not a proven ESC acceptance, rollback cure or vehicle safety result.

## Validation

Focused Linux PC suite: 228 passing checks for warp/shared input/watchdog and
signal/BSD/power display, including read-only buffers, exact bytes, reconnect,
stale/duplicate camera observations and packet budgets. CAN packet suite:
148 passing checks with vehicle Params access prohibited and test values
injected. The actual renderer produced three synthetic screen-8 previews
(left/red, both/green, clear) with the power/temperature badge and BSD walls.
These synthetic values and tests are not measurements from a driving vehicle.

The PC Python omits some Linux memfd constants; the test harness reads those
constants from the distro Python and uses the real fcntl calls, seals and mmap.
Initial broad-test failures were missing PC dependencies/Git metadata, not
device evidence. Final broad-suite and deployment results are recorded below.

Final broad Linux PC regression: 1598 passed, 4 skipped (17.87 s).
