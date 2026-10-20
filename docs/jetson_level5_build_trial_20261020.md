# TensorRT level 5 trial and timing-cache check, 2026-10-20

## Result

**Level 5 compiled successfully after reclaiming build memory, but the tested
candidate was slower than the installed level 3 engine. It was not deployed
or published. The user cancelled its distribution after seeing the result.**

| Engine, 600 sequential frames each | GPU mean | GPU P99 | Engine run wall mean | Engine run wall P99 |
| --- | ---: | ---: | ---: | ---: |
| Installed level 3, first pass | 23.396 ms | 23.494 ms | 24.210 ms | 24.424 ms |
| Candidate level 5 | 25.862 ms | 25.922 ms | 26.662 ms | 26.746 ms |
| Same installed level 3, return pass | 23.444 ms | 23.489 ms | 24.245 ms | 24.355 ms |

The candidate's mean GPU time was about 10.5% longer than the first baseline.
All sampled GPU clocks were 1,020 MHz. This compares this particular candidate
against the installed engine; it does not establish that every level 5 build
is slower. The original engine's workspace/timing-cache history was not
controlled to match the new candidate.

This was an isolated GPU/engine benchmark, **not full camera preprocessing,
USB/IPC latency, actual navigation/cruise execution, or road validation**.
The 50 ms target interval does not establish full-pipeline 20 Hz operation.

## Conditions and isolation

* Branch: `codex/jetson-nav-runtime-port`; comma source `433acbb5`.
* Installed Jetson source: `a1d723a6`; TensorRT 10.3.0, Orin-sm87,
  approximately 8 GB RAM. N16 ONNX SHA-256:
  `01effc679eb46d6036ce0302673a87fe2dcfcc95a679daeeb929679e53c3647a`.
* The comma remained offroad, with ignition off, disconnected harness,
  `noOutput` and zero transmit counters. No vehicle/control state was forced.
* Original model, plan, metadata and production timing cache were preserved.
  Candidates existed only in a private diagnostics directory on NVMe.
* FP16, device-side recurrent state and CUDA graph execution were retained.
  Native input staging remained off. No power-mode, clock, swap or USB power
  policy changes were made.
* Independent restore timers were armed before optional inference/display
  services stopped. Jobs were bounded and restored services in `finally`.

## Build attempts and memory reclamation

An initial private harness thermal-reader error aborted before useful build
work. Unsupported sensors were then excluded while preserving CPU/GPU checks.

| Procedure | Workspace ceiling | Level | Outcome |
| --- | ---: | ---: | --- |
| In-process ONNX preparation | 2 GiB | 3 | GPU allocation failures; no engine |
| In-process ONNX preparation | 512 MiB | 3 | GPU allocation failure; no engine |
| ONNX preparation in an exiting child process | 512 MiB | 3 | Engine generated in 213.9 s; not used as the final baseline |
| Same separate preparation, after the level 3 build | 512 MiB | 5 | GPU allocation failures, then resource-guard cancellation at 1,048.9 s overall |
| Level 5 only, separate preparation and explicit memory reclamation | 512 MiB | 5 | **Success: builder 814.8 s, supervised child 817.94 s** |

The user requested returning memory and compiling only level 5. The final
build stopped inference, HUD, Xorg and the boot-display service, synchronized
files, reclaimed page cache/reclaimable slab, and discarded file-cache pages
after preparation/parsing. ONNX preparation ran the **same installed patch
function** in a child that exited before the build. This did not change the
model's mathematics or production builder.

Available memory after the initial reclamation was 7,260,140 KiB (6.92 GiB).
The successful build's lowest recorded `MemAvailable` was 294.86 MiB and its
highest sampled CPU/GPU temperature was 50.94 C. Its log contained no GPU
allocation error. The installed OS still needed memory; this was reclamation
of unused allocations/cache, not returning all physical RAM to an empty state.

The successful build still had the earlier private 256 MiB preemptive guard,
but never reached it and was **not interrupted**. The user objected to that
guard near completion. The later benchmark-only run omitted memory-based
preemptive cancellation and completed normally. No second level 3 build was
performed after the user's level-5-only instruction.

Prepared ONNX hashes matched across the level 3 and level 5 preparations:
`b68573e1c10d72850c227266070d5f0c82b5a1d937ab5392b8180c988deb9b6a`.
The successful level 5 plan was 794,720,076 bytes, SHA-256
`406fab2cb96c68b0b53996e8de47ba23cd254685d76ef41c5d8779ec9b7c3d1d`.
The device's build wall-clock metadata was incorrect; durations and ordering
were taken from monotonic measurements instead.

## Benchmark and numerical checks

The sequence was installed level 3 -> level 5 -> installed level 3, 600 frames
per phase at a 50 ms target interval, with identical seeded image patterns
and bounded synthetic map/navigation/desire inputs. Recurrent state was reset
at each phase. The same private CUDA event instrumentation separated TRT
compute from input copies. Engine warmup and CUDA graph capture were retained.

The first sequence completed inference but its final reporting code treated
an output `slice` as a tuple and failed. It was excluded from timing results.
After correcting only that reporting error, the complete 1,800-frame sequence
was repeated without rebuilding either engine. Timings above come from that
completed repeat.

All outputs were finite. The return level 3 sequence was bit-identical to its
first pass. Level 5 was not bit-identical: overall mean absolute difference
0.004679, maximum raw difference 0.826172; `plan` mean/max 0.006209/0.826172,
`lead` 0.017592/0.8125. These tensors have different meanings/scales; aggregate
error is not a physical-unit error or a road-safety verdict. No reference-
framework or real-camera accuracy acceptance gate was completed. Finite
output alone does not approve a candidate for driving.

## Timing cache is already enabled

Both the checkout and installed backend pass a persistent timing-cache path
into `build_engine()`. The builder calls `create_timing_cache()`, attaches it
with `ignore_mismatch=False`, and serializes it atomically after a build.
Production `timing.trt10.3.0.Orin-sm87.cache` was 3,478,711 bytes; final SHA-256:
`ccfd0d77a492aea2a34ba847f2e1d5bb27e62ccbb4d71003c2669ea8e380bfa7`.
It was not replaced by the experimental level 5 cache.

Timing Cache reuses tactic profiling during **engine construction**. It can
reduce rebuild time and improve repeatability of tactic selection under
matching conditions. It does not change an already serialized, running
engine. There is no missing activation switch that would directly accelerate
the current level 3 engine. See NVIDIA's [TensorRT optimization guide,
Timing Cache and deterministic tactic selection](https://docs.nvidia.com/deeplearning/tensorrt/10.x.x/performance/optimization.html).

Further level 3 tuning needs measured layer bottlenecks and separate candidate
builds/accuracy comparisons, for example testing workspace/tactic selection
and timing-noise control. No such speedup was established here.

## Restoration and cleanup

Original installed level 3 plan SHA-256 remained
`a9075a6525b78e94f169ad687c8a45e1a7f68df0ab04bee522b556c7315ca085`.
Inference, HUD, Xorg and boot-display services were active after restoration.
Jetson source/boot and comma boot were unchanged. The original engine was
never overwritten by a candidate.

After the final restoration, 200 zero-input frames through the original
USB/IPC/TensorRT path all returned finite values: mean RTT 40.84 ms, P99
49.52 ms. These are health-check timings, not a full-camera deadline guarantee.
The final comma link was `ready`, with no reported error.

Private numerical/log evidence and reproduction scripts were archived locally.
Own candidate plans, patched ONNX scratch and diagnostic files (about 2.55 GB)
were removed from Jetson. Own jobs/timers were inactive; the comma receiver,
relay and local/phone diagnostic tunnels were stopped. The owner's persistent
SSH key remains. No engine, weights, credential, raw device log or release
asset is published by this change.
