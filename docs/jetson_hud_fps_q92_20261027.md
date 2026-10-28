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
- The saved selection applies onroad at startup, reconnect and preference
  changes, without waiting for Park. The selected index is forwarded to the host. An older USB host
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

## 2026-10-28: live FPS admission regression

The owner reported that changing FPS left the display at10fps. Reproduced on
PC when a fresh onroad Park carState arrives during `SubMaster.update(0)`:
`SnapshotBuilder.packet()` sampled its cadence clock before update, but
SubMaster stamps receive times inside update. The freshness test therefore
compared an older clock with the newer arrival and rejected it as future-dated.
The earlier zero-time fake assigned both timestamps the same value and missed
this ordering. The same skew could also admit a sample just beyond300ms.

Use a new monotonic timestamp after sampling and reading Params for rate
admission. Cadence scheduling, the300ms limit, Park/zero/valid/alive checks,
immediate10fps recovery and model-first USB admission are unchanged. No queue,
worker, allocation or additional vehicle subscription is introduced.

The new replay advances reception time by2ms per update and passes real cereal
events through the production SnapshotBuilder, private publisher framing,
CarrotSession HUD receive handler and DisplayParams. Three-second virtual
trials produce30/45/60/75/90 snapshots for10/15/20/25/30fps. The actual host
restart-loop code selects the corresponding Q92 renderer arguments. Drive to
Park transitions, return to10fps, stale/future/invalid/dead data, codec fallback
and existing USB/update gates are checked. These are virtual cadence and
configuration tests, not physical USB throughput or rendered/optical FPS tests.

Before the fix, six of the13 new tests failed, including all four above10fps
and the freshness boundary. After the fix, all13 and the related regressions
pass:147 PC tests total, plus Ruff and diff checks. This is a comma-only fix;
the already selected Q92/FPS Jetson package and its wire contract are unchanged.
Actual installed version, radio/USB behavior, panel frame rate and inference
P99 still require a matching device test.

## 2026-10-28: saved settings onroad

The owner subsequently requests the saved JPEG/FPS preferences onroad too.
This supersedes the earlier display-only Park admission policy, including the
historical clock-order regression above. Comma production always forwards its
saved FPS selection when the peer supports it; Jetson production starts/reopens
Q92 at that rate, even in Drive. An initial absent snapshot can temporarily use
10fps, then the fresh saved preference takes effect without a vehicle-state gate.
Malformed setting values and unnegotiated old peers still use10fps.

Only HUD resources reopen. No new thread, queue or vehicle subscription is
added; the redundant rate-watch carState decoder is removed. The model IPC
read/deadline, single USB owner, model-first idle budget, stalled-write backoff
and snapshot expiry remain. Vehicle data validity/ages are forwarded unchanged;
this does not make missing/stale observations valid. Battery cell scans and
USB package installation retain their separate fresh-P/zero permission checks.
Q92 encoder failures still visibly recover to H26410fps. A corresponding new
Jetson package is needed to remove the old host-side Park wait.

PC validation for this policy:203 Linux checks pass with one native FFmpeg
skip;156 Windows checks pass with48 platform-only skips;25 Wiki generator
tests and three staged localized setting explanations pass. All five saved
rates survive producer restart in onroad Drive and select Q92 host output
arguments. Physical frame rate and inference impact remain unmeasured.
