# PV5 OEM cluster BSD background

2026-11-04 follow-up. The PV5 native cluster now requests gray (7) for an
active model turn and steady orange (3) for a same-side signal and BSD. The
enabled paddle warning retains flashing-red (6) priority. The separate Jetson
HUD retains the red gradient during both phases and flashes only the red
bracket at 2.5 Hz. The original native-cluster experiment is recorded below.

The owner requests a red flashing OEM cluster background when a turn signal
and a blindspot warning refer to the same side. Reuse the existing CAMERA_SCC
`ADRV_0x161` display path and the defined `BACKGROUND=6` flashing-red request.
On 2026-11-02 the owner observed about one second ON/one second OFF and requested
2.5 Hz. The PV5 BSD request now alternates code 6 and the normal/turn background
every 200 ms using the existing 100 Hz controller frame. At the unchanged 20 Hz
0x161 cadence this is four red-request frames and four restoration frames,
repeated every 400 ms. No extra CAN frame, wall-clock timer, mutable state,
sensor query, process or Jetson inference work is added. The free-running phase
can delay the first red request by up to 200 ms.

This is a 2.5 Hz **request sequence**, not a verified physical flash rate. Code 6
still asks the cluster to animate; the DBC defines no steady-red code or flash
frequency field. Its internal animation/reset behavior may affect visible
brightness and frequency. Do not invent code 5 or claim a software CAN test
proves visible 2.5 Hz. A real-cluster check is required. The separate Jetson HUD
warning brackets retain their own 2.5 Hz timing; phases are not synchronized.

The owner subsequently observed 2.5 Hz on the HUD border and about 0.5 Hz
on the OEM background with the gated request. This is consistent with the
cluster running its own animation for code 6. A verified steady-red request
or a cluster flash-rate control is needed to drive the OEM background at 2.5 Hz.

Admission is limited to `CAR.KIA_PV5`, valid CarState, no CAN timeout, and either
left signal plus left blindspot or right signal plus right blindspot. Use the
retained turn request rather than the lit/dark lamp phase. Cruise/lateral
engagement is not required for this additional display cue within the existing
CAN message admission: the controller must use openpilot longitudinal control,
CAMERA_SCC, and have received the stock `ADRV_0x161` source message. This change
does not widen those gates. Losing admission
restores the existing background selector. The existing signal/BSM producer
and its underlying sensor freshness rules are unchanged.

The owner selected orange for the PV5 model-turn indication. While lateral
control is active and model desire is turnLeft/turnRight, request steady
`BACKGROUND=3`. The BSD ON phase and the existing enabled paddle warning take
flashing-red priority. BSD OFF phases restore the normal/turn background; an
enabled held-paddle warning keeps its original native flashing-red request,
including during BSD OFF phases. Clearing BSD returns to orange when the model turn remains active,
otherwise to the normal cruise/lateral background. Lane-change desire alone
does not request the model-turn color.

All other vehicles retain their previous behavior. Existing paddle warning,
alerts, sounds, steering/brake/acceleration requests,
message admission, cadence, counters and source dictionaries are preserved.
The only added frame difference is BACKGROUND and its recalculated checksum.

The tracked generated DBC defines background 0 hidden, 1 blue, 3 orange,
4 flashing orange, 6 flashing red and 7 gray. No light-green background enum
is verified. Use the owner's selected defined orange code; do not assign an
undocumented enum value based on a gap in this table.

PC validation uses archived current source, actual DBC parsing/CAN packing and
checksum code, synthetic CarState/CarControl, and an inert Params import.
`test_ccnc_cluster.py` and `test_blink_hold.py`: 277 passed, no skips. The
tests cover every left/right signal/warning combination, invalid/timeout data,
engagement-independent warning, other-vehicle preservation, event clearing,
warning/turn/paddle priority, every desire class and identical-frame A/B
payload/counter checks. The 2.5 Hz regression exercises every controller tick
over two seconds, checks four-ON/four-OFF request groups, unchanged transmission
count/counters/CRC/unrelated fields and native paddle/other-vehicle behavior.
Physical cluster color and
animation, CAN delivery and driving behavior have not been tested. No device
update or package publication is part of this verification. On 2026-11-02 the
owner separately authorized source publication; the cluster change is integrated
onto the latest PV5 branch without the local raw OBD diagnostic feature.

No user setting is added or changed. Korean/English lane-change guides note
the PV5-only display difference. The shared upstream Settings Wiki is not
changed to describe a display policy that applies only to this PV5 branch.
