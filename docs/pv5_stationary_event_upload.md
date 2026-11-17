# Stopped-vehicle event recording transfer

This optional mode is separate from the existing detached-home bulk upload.
It reuses the existing archive observer services; no new recorded signal,
control output, recorder change, SSH or automatic vehicle installation is added. Existing archive module fingerprints are unchanged.

## Selection and preparation

Only after admission, a low-priority worker scans existing **closed** rlogs,
newest route/segment name first. The scan uses bounded native framing, no AI and
no video decoding. It selects:

- Measured acceleration <= -3 m/s² for >= 0.2 s, with successive valid CAN
  observations <= 0.3 s apart and speed >= 2 m/s.
- Original SCC requested acceleration <= -3 m/s², valid version-1 metadata,
  matching source timestamps <= 150 ms old, and a recent valid car state.
  This is a request, not proof that the requested deceleration occurred.
- Recorded stock AEB, or the existing allowlisted serious fault events.

The matching whole segment and its immediate predecessor/successor are selected
for boundary context. This is not an exact video clip. Only existing rlog and
road video are transferred; cabin/audio are excluded by the existing privacy
filter. Missing/locked/still-recording segments are not forced closed or pinned.
A deleted or unavailable recording cannot be recovered. The ordinary home
uploader still receives all recordings independently of this scan cursor.

## Admission and interruption

The separately qualified `incident_stationary_v1` mode requires the real PV5,
valid CAN, exact raw zero speed, standstill, filtered speed within 0.01 m/s,
cruise off, enabled/active/lateral/longitudinal control all off, no current
emergency and fresh device/panda/manager/car/control/event data. Being merely
stopped in active adaptive cruise does not qualify. The conditions must hold
continuously for 2 s. The device must be on a preapproved unmetered Wi-Fi profile,
thermally green and within the existing vehicle CPU/memory/free-space gates.

The existing 400-ms permission expiry, owned process-group cancellation, nice 19,
idle I/O, 192-MiB address-space ceiling, CPU pacing/budget and 256-KiB/s transfer
rate are retained. Movement, cruise/control activation, stale state or loss of
permission cancels preparation and transmission. In-flight kernel I/O cannot be
retroactively recalled. Actual device cancellation latency/load is unmeasured.
A short traffic-light stop may be insufficient. Completed scan results and
64-KiB transfer acknowledgments survive restarts; an interrupted decompression
scan or privacy-copy preparation starts that file again. No claim of exact
resume during parsing is made.

The event worker and bulk worker share the existing single-worker lock and
archive completion IDs. Home upload has priority. New event settings never
inherit home qualification or home resource exceptions.

## PC review and tokens

PC dispatch now also accepts **risk-first-v1** `hard_deceleration` and
`stock_scc_hard_request` candidates with selected review windows, as well as
existing faults/AEB. Ordinary SCC assist, radar transitions and normal cruise
release alone stay quiet. Existing 30-minute same-cause cooldown, up-to-three
results per five minutes, receipt deduplication and uncertainty holds remain.
Only selected windows are eligible for AI. These are candidate situations,
not software-defect findings or vehicle safety validation.

## Activation boundary

Default OFF. No private settings are created by this change. A dedicated
`/data/pv5-pc-events.json` requires the six existing-style fields `version`,
`vehicle`, `enabled`, `qualified_modes`, `wifi_profiles`, `module_identity`,
with vehicle KIA_PV5 and modes exactly `["incident_stationary_v1"]`.
The private identity must match the new event modules and their dependencies.
It must only be installed after a separate actual-device qualification;
PC tests do not provide that qualification. Never print/copy authentication
secrets. The existing incident HTTPS registration and private key are reused.

The PC receiver uses a pinned checkout, so pulling vehicle code alone does not
activate the new PC dispatch policy. A verified PC release refresh and the
separate vehicle mode validation/activation remain operational steps.
