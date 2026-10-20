# N16 ONNX compatibility with the Jetson runtime (2026-10-20)

Subsequent activation and actual-camera/USB/map/cruise bench measurements are
recorded in [the deployment follow-up](jetson_n16_bench_20261020.md).

The N16 ONNX builds and executes on the existing Orin Nano Super and TensorRT
10.3.0. The current Jetlink input/output contract, parser and recurrent-state
implementation support it. This investigation does not select or deploy N16:
the existing `80eedcdd` Jetson model and normal inference service were restored.
Firmware, JetPack and the production update manifests were not changed.

## Artifact and source identity

- [Official model folder](https://huggingface.co/commaai/openpilot_driving_models/tree/eed9e515dccd5aa9be969a2d6c57eb469174a97d/96244b1f-6c4e-4adc-848d-eccd0b44b182/12800).
- File: `big_driving_supercombo.onnx`, 814,591,107 bytes.
- SHA-256: `01effc679eb46d6036ce0302673a87fe2dcfcc95a679daeeb929679e53c3647a`.
- Hugging Face revision: `eed9e515dccd5aa9be969a2d6c57eb469174a97d`.
- Fork source examined: `57f146575ff131bbf207b292431f1369927b1797` on
  `codex/jetson-nav-runtime-port`, with no tracked changes during testing.
- Installed Jetson runtime: `bd06baf124164499ec52b4a7688e1532f773928b`.
  The six shared Jetlink source files used in the checks matched the fork by
  SHA-256 after newline normalization.

The embedded ONNX `model_checkpoint` is
`2931d3d9-7c90-4988-a6af-654ed71e0af5/56320/96244b1f-6c4e-4adc-848d-eccd0b44b182`.
It omits the folder's `/12800`. A future Jetson spec must use this actual
embedded value; inventing a suffix would fail strict spec comparison.

The branch's separate direct-eGPU PKL path already points to N16. Its Jetson
ONNX selector still points to `80eedcdd/12864`, SHA-256
`1d18c42ea4cd95c8bffab4635844b78b8e2d4e25b9de44a9c5ac2e0961371a2e`.
That PKL selection does not update the Jetson model. Learned-weight equivalence
between the PKL package and this ONNX was not independently established.

## Contract and PC execution checks

Full ONNX checking passes before and after the current TensorRT graph patch.
Relative to the selected Jetson model, only `sha256`, `nbytes` and `checkpoint`
in the parsed model spec change. All 11 inputs, six outputs, five state pairs,
shapes, output slices and frame-skip contract remain the same:

- Warped images: `[2, 6, 128, 256]`, uint8 on the wire.
- Map: `[1, 1, 256, 256]`; navigation: `[1, 47]`.
- Main output: `[1, 18580]`.
- Framed request/reply: 655,604 / 74,340 bytes, unchanged.
- ONNX opset 20 / IR 10; 1,194 nodes and 790 initializers.

After normalizing renamed intermediate tensors and trace metadata, topology,
operator attributes and wiring are identical to the selected ONNX. However,
742 initializer contents change, so this is a different learned model.

ONNX Runtime 1.23.2 CPU compares the original graph (tinygrad passthrough removed)
with the current FP16 image-queue patch through the actual Jetlink client,
session, queues, packed inputs and parser over a local socket. All six outputs
and recurrent states are exactly equal across 140 continuous frames plus eight
reset/replay frames (148 total). Inputs cover black/white/ramp/random images,
desires, traffic convention, action timing, map/navigation presence, absence and
recovery. Parsed outputs are finite/equal, reset replay is deterministic, and an
injected nonfinite map state is rejected and cleared. This is a software
protocol/packing check; the socket is not physical USB.

## Actual Orin build and execution

The bench test uses an isolated cache and the installed runtime. It does not
publish camera/control messages or modify the production model selector. Only
the normal Jetson inference service is temporarily stopped, with a bounded
supervisor and automatic restoration. HUD service remains running.

- TensorRT 10.3.0, `Orin-sm87`, existing L4T 36.4.7.
- Build: 40.91 seconds, seeded with a private copy of the existing timing cache.
- CUDA Graph captured; GPU-resident recurrent state enabled.
- 2,400 synthetic frames in 120.172 seconds, paced with 50ms sleeps.
- All driving outputs finite; existing state-validation path retained.

| Measured interval | Mean | P99 | Maximum | Frames >50ms |
| --- | ---: | ---: | ---: | ---: |
| `TrtEngine.run()` | 24.919ms | 26.743ms | 26.816ms | 0 |
| Host queue preparation | 0.948ms | 1.144ms | 2.194ms | 0 |
| Host feedback | 0.103ms | 0.159ms | 0.212ms | 0 |
| Queue + run + feedback | 25.994ms | 27.899ms | 28.171ms | 0 |

`TrtEngine.run()` measures host elapsed time through the reply event, including
scheduled transfers and checks; it is not GPU-kernel-only time. The other
intervals are measured separately, and their individual maxima need not coincide.
The observed paced rate is 19.971Hz, not a strict >=20Hz result. Processing time
fits inside 50ms in this isolated workload; this does not establish a full
camera-to-publication deadline under actual USB and onroad load.

Once-per-20-frame resource samples show maximum temperature 56.937°C, module
VDD_IN power mean 13.761W / maximum 14.057W, and minimum available memory
2,710,806,528 bytes. These are sampled module measurements, not total vehicle
power, peak GPU memory or a sustained thermal qualification.

The existing `tools/jetlink/probe_device_recurrence.py` comparison also runs
against this new plan in separate processes, using host state feedback and
GPU-resident feedback. All 32 outputs match exactly (maximum absolute error 0;
gate `rtol=atol=1e-4`), with resets at frames 0 and 16. This confirms equivalence
of those two TensorRT state-handling modes for this probe.

## Numerical and driving limits

CPU graph equivalence and TensorRT state-mode equivalence are distinct from
CPU-versus-TensorRT precision. Comparing the first 148 identical synthetic input
frames against the original CPU graph gives finite parsed outputs, but nonzero
CPU/TensorRT differences:

| Parsed value | Observed maximum absolute difference |
| --- | ---: |
| Action channel 0, divided by squared speed to obtain curvature | 0.002308 |
| Action channel 1, used as desired acceleration | 0.005737 |
| Plan means, all channels/horizons | 0.5625 |
| Lead means, all channels/horizons | 1.125 |
| Lane-line probabilities | 0.015171 |

For context, the largest plan difference is at synthetic frame 28/horizon 31:
channel 0 is 100.125 on CPU versus 99.5625 on TensorRT. The largest lead
difference is at frame 4/selection 0/horizon 5: channel 0 is 129.25 versus
128.125. These examples do not establish acceptable road error. No validated
numerical acceptance threshold was defined for this model, and synthetic images
do not validate perception quality, vehicle response or safety. The cause and
acceptability of the precision differences require separate investigation.

Not covered: recorded road-image accuracy/replay, real map/route arrival,
camera preprocessing plus physical USB roundtrip, simultaneous full onroad and
cruise workload, long-duration/thermal/fault recovery, or closed-loop driving.
Existing runtime timing and state checks passing must not be represented as
those missing validations passing.

## Deployment follow-up

No firmware/JetPack update is needed for the tested runtime compatibility.
Actual selection would still require a deliberate N16 model pin with the three
identity changes above, an updated display label, and a matching scoped host
bundle/USB manifest. Build a new hash-specific TensorRT plan; the old plan
contains the old weights. Preserve the official-release selection/restore
path. This report makes none of those production changes.

Both isolated supervisors exited successfully and restored the previous
inference service/source. The comma checkout and selected `80eedcdd` model
remained unchanged. Normal inference returned to `ready`; HUD connectivity was
observed after restoration, with pre-existing intermittent Wi-Fi retries also
observed during this session. No Wi-Fi stability improvement is claimed.

Detailed synthetic results and reproduction scripts are retained privately;
weights, compiled plans, credentials and device captures are not committed.
