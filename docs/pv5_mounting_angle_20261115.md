# PV5 mounting-angle readout

The comma on-road HUD now shows the existing mounting calibration as
`설치각: ^ 3° | < 2°`. Down/right is `설치각: v 3° | > 2°`. This applies
only to real Hyundai KIA_PV5 profiles. It adds no setting or control behavior.

The source is `liveCalibration.rpyCalib`, already subscribed by UIState.
Pitch greater than zero points down and yaw greater than zero points left,
matching the existing DeviceLayout calibration description. Display magnitudes
are absolute degrees, rounded to one decimal with an integral `.0` omitted.
Rounded zero has no directional chevron. ASCII `^`, `v`, `<`, `>` use existing
font glyphs; no font atlas or image asset is added.

Only a valid, alive, seen, calibrated sample from the current on-road epoch is
shown as numeric. Missing, malformed, nonfinite, old, or uncalibrated data shows
gray `-- | --`. Formatting is cached until the source receive frame or validity
changes. The ordinary UI loop handles updates; there are no new threads,
timers, subscriptions, persistent parameter reads, logging, or inference.

The readout uses a small dark plate beneath the clock/date on the C3X layout.
Shorter viewports use space to the right of the existing speed/device panel.
Views smaller than 1000 by 600 retain their existing layout. Existing HUD
elements are not moved. Every existing alert size, and unknown/unhealthy alert
state, suppresses the informational plate. It returns when fresh alert state
is clear. The overlay key includes that state so cached content cannot retain
the plate over a warning.

This is the device mounting estimate, not road inclination, instantaneous body
pitch, camera yaw trim, or the experimental wide-input downward offset.
The radar, model/FOV transforms, calibration producer, steering, longitudinal
control, SCC helper, warning/audio policy, and paired Jetson packages are unchanged.

## PC validation

- 121 focused direction, malformed/freshness, real-HUD update, HUD/cache/text and
  DM-layout checks pass; no failure or skip. Ruff passes on the four changed
  Python files.
- 12 native scenes in Korean/English at 2160x1080, 1600x900 and 1280x720,
  rendered before/after with the production drawing methods and source font.
  Camera imagery, the speed texture, and message input are synthetic.
- All 24 images were compared. Other-car, notCar, DM and critical scenes are
  pixel-identical. In the other eight scenes all changed pixels are confined
  to the readout; its measured rectangle clears the speed/device panel.
- The pre-change HUD suite had two failures because a partial `__new__` test
  object lacked an existing driving-feedback member. Those test objects now
  initialize required collaborators, including the new mounting member.
  Production initialization is exercised separately.
- The PC renderer initially called an unavailable gradient helper; only its
  synthetic texture generator was changed to the supported solid-color helper.
  Ruff was absent from the Linux test venv; the cached Windows Ruff 0.17.5 was used.

These checks do not establish physical mounting accuracy, display timing or
vehicle CPU/GPU cost. No vehicle connection, installation or restart was made.
