# Jetson HUD device and inference footer

The Jetson-only HUD footer separates comma CPU/memory from Jetson CPU/memory/GPU.
Comma values reuse fresh `deviceState` received through the existing Wi-Fi HUD
snapshot. They never fall back to Jetson values. Jetson CPU and memory come from
its local Linux sampler; GPU load is read from the verified Orin sysfs node.
CPU percentages are the average of available core percentages, not a sum that
can exceed 100%. Memory is whole-device used memory, not just HUD process RSS.

GPU load follows [NVIDIA's Orin load definition](https://docs.nvidia.com/jetson/archives/r36.4.3/DeveloperGuide/SD/PlatformPowerAndPerformance/JetsonOrinNanoSeriesJetsonOrinNxSeriesAndJetsonAgxOrinSeries.html):
the sysfs 0..1000 value divided by ten is the percentage. The live board exposes
`/sys/class/devfreq/17000000.gpu/device/load`, resolving to the kernel's
`/sys/devices/platform/bus@0/17000000.gpu/load`. Missing/invalid values show `--%`.

The second line shows the last ten seconds of **Jetson infer** mean, p99 and
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

Host collection runs once per second on a SCHED_OTHER/nice-19 background thread.
Socket work has a short deadline, 64 KiB pending-byte limit and bounded event drain;
failed connections back off. The render loop only reads the cached result.
The host Linux sampler is captured before installing the vehicle-only HUD adapter,
so the two rows cannot accidentally display the same machine's counters.
Existing battery reporting remains at 2 Hz and camera previews remain at 10 Hz
when a camera view is selected. The model, firmware and control policies are not
changed by this display feature.

PC checks cover missing/invalid GPU values, host separation, stale statistics,
ten-second expiry, p99, read-only history, engine replacement and actual control
reply framing, alongside the existing battery/HUD/Wi-Fi regression suite.
The owner-signed [runtime-footer package](https://github.com/tjdgus200/openpilot/releases/tag/jetson-runtime-footer-20261019-960eaaf4)
contains committed source `960eaaf4`, with the unchanged model and runtime contract.
The packaged regression suite passed **317 tests**, with one isolated-root
integration skip. The exported HUD entry point also passed outside the checkout.
Dark/light, normal/swapped-panel layout fixtures were checked with the actual
renderer. Actual activation and runtime sampling must also be checked on Jetson.
