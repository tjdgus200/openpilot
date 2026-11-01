# PV5 OEM cluster BSD background

The owner requests a red flashing OEM cluster background when a turn signal
and a blindspot warning refer to the same side. Reuse the existing CAMERA_SCC
`ADRV_0x161` display path and `BACKGROUND=6`; the cluster animates the flash.
There is no software flash timer, additional CAN frame, sensor query, process,
or Jetson inference work. This does not synchronize the OEM flash period with
the separate Jetson HUD's 2.5 Hz warning brackets.

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
`BACKGROUND=3`. BSD and the existing enabled paddle warning take flashing-red
priority. Clearing BSD returns to orange when the model turn remains active,
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
`test_ccnc_cluster.py` and `test_blink_hold.py`: 261 passed, no skips. The new
tests cover every left/right signal/warning combination, invalid/timeout data,
engagement-independent warning, other-vehicle preservation, event clearing,
warning/turn/paddle priority, every desire class and identical-frame A/B
payload/counter checks. Physical cluster color and
animation, CAN delivery and driving behavior have not been tested. No device
update or package publication is part of this verification. On 2026-11-02 the
owner separately authorized source publication; the cluster change is integrated
onto the latest PV5 branch without the local raw OBD diagnostic feature.

No user setting is added or changed. Korean/English lane-change guides note
the PV5-only display difference. The shared upstream Settings Wiki is not
changed to describe a display policy that applies only to this PV5 branch.
