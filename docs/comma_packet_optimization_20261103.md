# Additional comma packet work, 2026-11-03

## Scope and retained behavior

Owner approved additional candidates1-3, then requested cruise, camera and model
input/output simulation before publication. Baseline is
120566c473fd117a558d216b2dd525eb0d2bdcac. These changes reduce auxiliary comma
work; they do not change model tensors, inference engines, cruise algorithms,
CAN messages, BLE polling, saved Q92/FPS, input-fusion qualification or update
permissions. Published model-specific Jetson package pins remain compatible and
unchanged; the old host accepts the same byte-identical display wire format.
No device installation or settings change was performed.

1. HUD packet building loads LINK/MODEL status once each and passes that pair to
   badge/diagnostic/camera consumers. There is no cross-packet status cache or
   worker. Each use rechecks its own monotonic freshness. Ordinary3s, connecting
   handshake8s and downloading/checking-files35s leases remain; camera admission
   still uses strict3s link and panel heartbeat. Missing/invalid files, future
   timestamps, model errors and health/address expiry retain their gates.
2. CarrotMan route selection and CarrotServ control share an immutable decoded
   CarrotNavi value for one subscriber/recv_frame/logMonoTime generation.
   Every access checks alive/valid; disconnect clears the cached generation.
   A subscriber without reliable generation metadata uses the original uncached
   parser. Existing no-update heartbeat handling, sequence/session/off-route
   transitions, distance dead reckoning and GPS age checks remain in CarrotServ.
3. Small HUD packets retain one full JSON encode. Oversized packets account for
   only optional-field byte sizes instead of repeatedly encoding retained core
   events. Large/complex signal metadata uses a direct encode after removal to
   avoid spending time encoding its discarded value. This heuristic chooses a
   cost path only: all sizing remains exact. Original insertion order, escaping,
   equality-at96KiB, optional-removal priority, record mutations and final failure
   limit remain. Removing navRoute never mutates the cached service dictionary.

Production profiling/comparison UI stays off. Earlier auxiliary1-4, BSD/cluster,
108-channel battery, stale-value expiry, black boot diagnostics, USB input-power
cutoff, model identities and Park/update/rollback protections remain preserved
in AGENTS.md and their regression suites.

## PC comparison

The committed manual tool `tools/benchmark_comma_packet.py` loads trusted source
exports for status/hud/navi from the baseline above. Export those three files as
baseline_status.py, baseline_hud.py and baseline_navi.py, then pass their directory
with --baseline and a JSON destination with --output. It uses synthetic values,
tmpfs files and15 alternating batches per variant; these are median batch means
on Linux x86, not on a comma or Orin. The artifact contains exact values.

| Workload | Before | After | Interpretation |
| --- | ---: | ---: | --- |
| Status reads per HUD packet | 6 /32.95us | 2 /12.50us | About62% less in this helper |
| Navi:1 new message over20 ticks | 1105us | 59.7us | 21 parses become1 |
| Navi:5 new messages over20 ticks | 1310us | 273us | 25 parses become5 |
| Navi:20 new messages over20 ticks | 2089us | 1095us | 40 parses become20 |
| Normal small JSON | 7.65us | 7.67us | Essentially unchanged; no claimed gain |
| Drop large signal metadata only | 151.36us | 151.57us | Essentially unchanged |
| Oversized JSON dropping camera/map/route | 574-1053us | 470-537us | About18-49% less in these cases |

The Navi fixture contains256 synthetic route points. Smaller real messages can
save less. Assuming20 HUD packets and20 service ticks per second, these status
and Navi fixtures together save only about1.4ms of PC work per second (roughly
0.14% of one CPU core). This is an illustration, not a comma CPU prediction or
an inference-time improvement. Oversized JSON savings occur only when pruning
is needed; ordinary packets gain essentially nothing from that optimization.

## Verification

Final combined PC run: **1622 passed,3 subtests passed,5 skipped,1 deselected**.
The result artifact records the scope and baseline. Coverage includes:

- Real cereal byte envelopes through the HUD producer at10/20/30fps: P/D/R,
  cruise enable/cancel/brake transitions, invalid camera data, packet overflow,
  and retained timestamps/alive/valid fields. Core carState/carControl/modelV2/
  road/wide-camera bytes match the input events exactly.
- Actual cruise button/common-event/state-machine logic: door-open,
  seatbelt-unlatched and ACC-fault no-entry; user cancellation/disengagement;
  the existing full state-machine and coasting regression tests.
- Four curated model identities/contracts/parsers, selected matching packages,
  exact camera/scalar request contents, recurrent feedback/reset, projected/full
  outputs, nonfinite/shape/frame/generation rejection, camera pairing/gaps,
  late replies, fallback, reconnect and resource ownership.
- HUD/BSD/brightness, battery expiry/heatmaps, Q92/FPS, status hotplug/leases,
  boot/update permission and input-fusion qualification regressions.
- Differential JSON byte and mutation comparisons over220 seeded mixed-size
  packets plus exact96KiB boundary cases. Shared status and Navi tests check
  expiry, camera teardown, new subscriber/generation, immutable ownership,
  disconnect/reconnect and heartbeat distance preservation.

The full run also exposed an old bootstrap test passing None where the current
runtime requires a Gate; only that test fixture was updated to construct Gate.
Three existing HUD tests' doubles now accept the optional status snapshot.
Runtime boot permission logic was not changed to accommodate a test.

The PC checkout is sparse. Missing unchanged selfdrived/CAN/DBC dependencies were
exported from current HEAD into a separate test directory. Hardware identity,
Params storage, logging and native msgq imports use explicit PC doubles; attempts
to access native IPC/devices fail. Cereal schemas and the control/state-machine
implementations are real. Existing native/optional tests account for5 skips;
one loggerd SConscript source check unavailable in this checkout was excluded.

This does **not** execute a TensorRT engine on Orin, camera hardware/VisionIPC,
native AGNOS Params/msgq, physical USB/RF, live CAN or a closed-loop vehicle.
CPU engine/transport and camera fixtures validate contracts and recovery, not
physical inference accuracy, end-to-end deadlines or road safety. No such claim
is made. Parked hardware verification remains necessary before relying on the
new build in a vehicle.
