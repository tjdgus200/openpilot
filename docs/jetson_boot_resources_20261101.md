# Jetson connection-screen resources

The CPU-only black connection/boot diagnosis screen shows one top-right line:

`65.0 C | CPU 12.3% | MEM 28.4% | PWR 14.7 W`

All four fields use the previous temperature font (KaiGenGothicKR-Bold, 22 px),
color and baseline, separated by ` | `. BOOT uptime moves immediately to the
left of the line. The nine diagnostic rows and HUD panel-ownership handoff remain.

CPU and used RAM percentages come from the existing Jetson-local `/proc` sampler,
one instance at a one-second refresh interval across display/USB retries.
Power reads the existing INA3221 `VDD_IN` module rail helper once per boot frame.
It is instantaneous module input power in watts, not energy or vehicle power;
overlapping rails are never summed. Unavailable or invalid metrics display `--`,
including CPU before its first interval measurement. No new comma processing,
inference, GPU work, model timing socket, tegrastats process or network request
is introduced for these metrics.

The immutable diagnostic installer includes both small sampler/helper modules,
so diagnosis remains available without a model, comma, Xorg, raylib or TensorRT.
The current runtime's per-release diagnostic refresh retains existing safeguards.
Light speed is identified by its exact model SHA on this screen as well.

Each model receives a distinct committed bundle identity for legacy bootstrap
directory/readiness compatibility. The exporter selects its model pin without
changing the checkout's N16 default. Existing model, transport, optimization and
rollback contracts remain unchanged. PC rendering/import checks do not measure
actual Jetson display CPU usage, sensor availability or driving behavior.

## Model-specific bundle source records

- N16: base boot-resource implementation; export `model_catalog/n16.json`.
