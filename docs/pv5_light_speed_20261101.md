# PV5 Light speed model integration

## Source and contract

- Reviewed upstream source: [d66d445b](https://github.com/ajouatom/openpilot/commit/d66d445b05c99ab27b4a96aa1f904cc3dfe2834c).
- Official ONNX: [fixed Hugging Face revision](https://huggingface.co/commaai/openpilot_driving_models/blob/84c72ed5381a64b8630890b861cddb5adf5ae8aa/f22b8b34-0983-4185-a453-a87b73798d61/16384/big_driving_supercombo.onnx).
- SHA-256 `6274eb4a3f3eb7ad1ab341d222bbba2ba709b06645dbbd15ac4971d6cb22ab21`, 792,305,602 bytes.
- Catalog key `light-speed`, display `Light speed (f22b8b34)`.
- The pin includes every input/output shape, all output slices, checkpoint and frame skip.
  This model has no navigation inputs; NOO is not supported. N16 remains the default.

## PV5 delivery and preservation

The selector exposes this model only with its exact matching `pv5-usb-light-speed.json`.
The official non-PV5 host bundle is not substituted. Each model-specific bundle has a
distinct committed source identity, so older USB bootstraps cannot reuse another model's
directory or readiness result. The exporter writes the selected catalog spec to both
bootstrap filenames without changing the checkout's N16 default.

The shared comma model choice is frozen for the entire boot for both eGPU and Jetson.
Selection prepares the next boot; it never swaps a running model. Park with ignition off,
connect to the Internet and reboot both devices. First use downloads the model and builds
the TensorRT engine. Physical USB delivery retains existing SHA-256 owner opt-in, size,
runtime, storage, rollback and freshness checks; hashes are not publisher authentication.

The package retains BSD rails/gradients/blink/brightness, W/temperature, battery page 8,
108-channel/15-temperature heatmaps and stationary view, BLE retry/parser/retention,
Q92 and 10–30 FPS, USB input-power block, dark boot diagnosis and Park update checks.

Compatible common defaults remain enabled: GPU recurrent state, CUDA graph where
supported, LUT input conversion, input preparation overlap and negotiated shared RAM.
Exact-model gates remain intact: N16-only native image conversion, direct/deferred map,
compact output and qualified SM87 input fusion are not enabled for Light speed.

## Validation boundaries

PC verification covers model hash/size, actual ONNX metadata, graph validity, existing
TensorRT input patch equivalence on CPU, repeated recurrent-state/reset outputs,
model-specific selector/package identity, preserved optimization gates, and isolated
exported server/HUD imports and rendering. Verification results accompany the release.

It does not establish physical Orin TensorRT build success, FP16 output accuracy,
20 Hz timing/P99, USB reconnect behavior, thermal stability or driving safety. Those
require separate device and vehicle validation. Upstream USB AMD eGPU timing incidents
are a different execution backend and do not quantify Orin performance.
