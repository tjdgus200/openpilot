# Jetson session SOC use

Diagonally below and to the right of the SOC value, the Jetson HUD shows a28px
line such as `사용2.5%` / `Used2.5%`, right-aligned within the SOC column.
The owner requests the percent suffix without p. It subtracts current validated SOC from
the first validated SOC received during this HUD process session. This is the
change in displayed SOC percentage points, not an energy measurement or a
percentage of the initially available energy. An increase is labeled separately
(`증가1.0%` / `Gain1.0%`) rather than clamped or called consumption.

The baseline is one scalar shared across renderer recreation in the same
process. Bluetooth loss/reconnect, page switching and USB/FPS reopening do not
reset it. A Jetson reboot, HUD service restart or runtime update that starts a new
HUD process resets it to that process's first valid SOC. There is no file write,
history, timer, extra telemetry field or extra OBD request. Only the Jetson renderer
does the subtraction. Invalid/missing/expired SOC or invalid vehicle telemetry
shows --, preserving the baseline for a subsequent valid observation.

The line fits in the existing SOC column at baseline169; the temperature
detail in the right column, inference/resource information and overlays retain
their positions. Both English and Korean are supported. All four PV5 model
runtime packages include this display-only code; no model/input/output/engine
contract changes. PC calculation, reconnect, freshness, renderer replacement and
layout checks do not establish physical display readability or driving safety.

## Distinct package source records

- N16: combined connection-resource, incremental-cell and trip-SOC implementation.
