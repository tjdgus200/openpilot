# Jetson HUD device and inference footer

The Jetson-only HUD footer separates comma CPU/memory from Jetson CPU/memory/GPU.
Comma values reuse fresh `deviceState` received through the existing Wi-Fi HUD
snapshot. They never fall back to Jetson values. Jetson CPU and memory come from
its local Linux sampler; GPU load is read from the verified Orin sysfs node.
CPU percentages average the reported core percentages, not a sum that can exceed
100%. Comma's forwarded list may include zero entries for offline cores, and an
average can hide saturation of an individual core; it is not a scheduling-margin
guarantee. Memory is whole-device used memory, not just HUD process RSS.

GPU load follows [NVIDIA's Orin load definition](https://docs.nvidia.com/jetson/archives/r36.4.3/DeveloperGuide/SD/PlatformPowerAndPerformance/JetsonOrinNanoSeriesJetsonOrinNxSeriesAndJetsonAgxOrinSeries.html):
the sysfs 0..1000 value divided by ten is the percentage. The live board exposes
`/sys/class/devfreq/17000000.gpu/device/load`, resolving to the kernel's
`/sys/devices/platform/bus@0/17000000.gpu/load`. Missing/invalid values show `--%`.

The large bottom row shows device resources; the battery page places inference
statistics above maximum battery temperature. The latter shows ten seconds of **Jetson infer** mean, p99 and
maximum in milliseconds, plus the sample count. The source is the existing
`TrtEngine.last_gpu_us` timing: CUDA launch through the synchronized reply point,
including required device input/output and state-validity work. It is not a
CUDA-kernel-only duration, camera-to-publication latency or USB/IPC round trip.
The p99 is the interpolated 99th percentile, the boundary of the slowest 1%, not
the mean of that 1%. The maximum is the worst individual recorded sample.

No per-frame profiling or new inference-message field is added. A fixed-window,
read-only `display_stats` command on the existing local owner-only control socket
summarizes the bounded FrameStats deque on its client thread. It does not load,
unload, reset, configure or inspect GPU buffers. Engine replacement starts a new
display window; old-model samples are excluded. Idle/inference data at least two
seconds old is shown as unavailable, and cached footer snapshots expire after
three seconds. The ordinary control `stats` event and USB framing are unchanged.

Only numeric values receive display colors, with a dark contrast strip in both
themes. These are UI headroom cues, not certified health classifications or
vehicle alerts: CPU orange/red starts at 80/95%, memory at 85/95%, and each infer
statistic at 35/50 ms. GPU utilization stays white because high utilization alone
is not a fault. Missing values are gray. The infer 35 ms cue reserves a nominal
15 ms of a 50 ms frame budget, without claiming USB/preprocessing always fits it.
Text layout reuses a bounded two-entry cache until the values change.

With a turn signal and an existing same-side vehicle BSD flag, the driving panel
also shows a solid red edge strip. It uses existing forwarded carState flags,
not a new detector or radar-distance inference. The snapshot and sender-relative
carState receive ages must be valid and sum to at most 300 ms; invalid, missing
or expired data clears the cue. Pre-receipt network transit is not measurable
from these clocks. Existing vehicle alerts are drawn last over this display cue.
No CAN transmission, steering/longitudinal command or additional thread is added.

Host collection runs once per second on a SCHED_OTHER/nice-19 background thread.
Socket work has a short deadline, 64 KiB pending-byte limit and bounded event drain;
failed connections back off. The render loop only reads the cached result.
The host Linux sampler is captured before installing the vehicle-only HUD adapter,
so the two rows cannot accidentally display the same machine's counters.
Battery reporting now caches slow values at 1 Hz and refreshes instantaneous
pack consumption/regeneration power at up to 10 Hz in the existing comma HUD
worker. No additional CAN requests, worker, or transport is created. Cached
values keep their source timestamps; resending them does not make them fresh.
The source ECU's update rate remains an upper bound on new readings. There is
still no validated enabled PV5 receive profile, so these rates do not establish
actual vehicle CAN availability or diagnostic performance. Camera previews remain
at 10 Hz when a camera view is selected. The model, firmware and control policies are not
changed by this display feature.

PC checks cover missing/invalid GPU values, host separation, stale statistics,
ten-second expiry, p99, read-only history, engine replacement and actual control
reply framing, alongside the existing battery/HUD/Wi-Fi regression suite.
The initial owner-signed [runtime-footer package](https://github.com/tjdgus200/openpilot/releases/tag/jetson-runtime-footer-20261019-960eaaf4)
contains committed source `960eaaf4`, with the unchanged model and runtime contract.
The packaged regression suite passed **317 tests**, with one isolated-root
integration skip. The exported HUD entry point also passed outside the checkout.
Dark/light, normal/swapped-panel layout fixtures were checked with the actual
renderer. This version was activated on the board and the owner confirmed the
visible footer. A finite offroad test served 600 synthetic frames in 30.002 s
without an error. The live sampler returned mean/p99/max 24.7/26.7/26.8 ms for
one ten-second window and correctly returned unavailable after inference stopped.
An independent 40-second sampler process used 0.070 CPU seconds (about 0.175% of
one core); this is sampler overhead, not total HUD rendering or CAN decoding cost.
These figures do not validate vehicle control, camera latency or all-frame 20 Hz.
The subsequent resource-row relocation, display colors and BSD cue require
activation of a newer signed package; the initial package cannot display them.

The split battery-rate candidate was compared on the desk comma using synthetic
receive-only CAN objects (80 unrelated frames plus one configured reply), decoder,
report creation and JSON encoding. Three alternating 3,000-call batches per
variant measured old/new mean wall cost **0.254/0.314 ms per call**; candidate
batch p99 values were **0.424–0.508 ms**. Candidate process CPU was about
**0.326 ms per call**, or **0.326% of one core at 10 Hz**. This excludes real socket
delivery/Cap'n Proto decoding, the rest of the shared HUD snapshot and rendering.
It does not measure PV5 reception or prove a worst-case deadline. No CAN socket,
profile file, transmit call or HUD publication existed in that probe.
