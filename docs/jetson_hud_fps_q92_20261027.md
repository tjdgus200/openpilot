# Jetson HUD Q92 and selectable snapshot/output rates

Production rendering uses the pinned CPU TurboJPEG encoder at quality92 and
the existing resolution, 4:2:0 sampling and USB display protocol. The old eight
codec choices remain private bench tools; they are removed from Carrot Web.
`JetsonHudFps` stores indices0..4, displayed as10/15/20/25/30fps. Default0 means
10fps. Old `JetsonHudQuality` values do not select the new rate.

## Actual path

- The existing low-priority comma snapshot worker builds at the admitted rate.
  It caches unchanged cereal events and retains one atomic RAM snapshot only.
  No queue, additional worker, Wi-Fi path or asynchronous inference USB writer
  is added. Existing camera-preview sampling and model inference stay unchanged;
  increasing HUD rate does not create new model/camera observations.
- Fresh stationary Park permits an onroad increase. Lowering to10fps is always
  possible. The admitted index is forwarded to the host. An older USB host
  without `carrot_hud_fps_v1` continues receiving10fps snapshots.
- Existing GPU-execution windows carry snapshots as before. To exceed the20Hz
  model cadence, the same USB owner also checks for the latest snapshot while
  waiting for the next local model request. Ready requests win. Read readiness
  is checked before the original complete-packet read; IPC buffers, deadlines,
  model inputs, recurrence, output parsing and reply ordering are unchanged.
- Additional idle transmissions omit navigation fragments. They reserve4ms plus
  a measured send allowance before the next predicted request; a HUD write over
  2ms or model USB exchange of45ms or more suspends these extra transmissions
  for2s. Source stalls produce no catch-up bursts, and snapshots expire at300ms.
- A synchronous USB message cannot be preempted safely after it starts. These
  are best-effort admission rules, **not hard real-time isolation**. Device stalls,
  shared CPU/DRAM or prediction error can still affect inference. Keep10fps as
  the conservative default; higher rates require matching live measurements.
- Jetson output uses the same admitted target. Encoder failure visibly falls
  back to H26410fps and never credits that trial as a Q92 result. Only display
  resources reopen, with the existing panel lock and cleanup.

Tools shows three independent one-second counters: comma snapshot generation,
successful USB HUD reception, and successful Jetson display submissions. They
do not measure optical panel refresh. Target and achieved rate may differ.
The existing GPU/server P99 comparison has a separate Q92/FPS trial cache;
previous codec trials cannot contaminate it. Sample limits, warm-up, session
identity and qualification rules remain unchanged. Server timing is not full
comma-to-Jetson round-trip timing. No rate is selected automatically.

## Heatmap and retained behavior

Heatmap colors use the lowest valid displayed value as their reference. Equal
values are green; nonzero differences have a visible minimum tint. Fixed
temperature knots0/1/3/8°C and voltage knots0/20/60/120mV interpolate through
green/yellow/orange/red. Unlike per-frame normalization, a tiny spread is not
painted maximally red. Colors are diagnostic, not validated OEM fault limits.
Invalid channels remain gray; zero°C remains valid. Counts, original timestamps,
108-slot voltage/15-slot temperature bounds and power5s/other60s expiry remain.

BSD uses elapsed time with200ms on/200ms off instead of counting output frames,
preserving nominal2.5Hz at every rate. Steady no-turn warnings, maximum warning
brightness/automatic restoration, gradients, W/temperature, battery screen8,
fresh300ms vehicle gates and auxiliary-USB input-power protection remain.

## Validation limits

PC regressions cover all producer rates, no catch-up, old-host compatibility,
private rate metadata removal, oversized/stale snapshots, partial local IPC
reads, admission backoff, codec fallback, BSD cadence, heatmap distinctions and
fresh installed-server telemetry imports. No physical device was available for
this change. Actual USB30fps, CPU/RAM headroom, model20Hz/P99 and panel quality
are unverified; PC checks do not establish driving safety or zero inference
impact. Host update/model/authentication/activation/rollback checks are retained.
