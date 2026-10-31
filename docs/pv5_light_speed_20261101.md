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

## Published package and PC results

- [PV5 Light speed package](https://github.com/tjdgus200/openpilot/releases/tag/jetson-pv5-light-speed-20261101-51b8b9f3) — source `51b8b9f3c37770c62ea4263c94ecf3fa9a754c3a`.
- Bundle: 65,165,870 bytes, SHA-256 `03f38f7a0a703b0aa44c855ce75d30e66657391f3b0f9e7f5da8942b8785fb01`.
- All four public release assets were downloaded anonymously and hashed against the local files.
- The original and patched ONNX pass full graph checking; actual metadata equals the complete
  reviewed pin. Four changing/recurrent/reset synthetic frames produce exactly equal finite
  CPU outputs, including every recurrent-state output. This is not a TensorRT output test.
- Source preservation gates: Linux 832 and Windows 708 passed with no failures or skips.
  Additional Linux exporter/isolated entrypoint/eGPU containment checks: 18 passed.
  Web model selection/title tests: 12 passed. The final selector/package pin also requires
  a fresh final-commit preservation run; owner-side JSON records bind each run to its HEAD.
- The existing N16, Flip Flop and CTV3M package pointers and the global N16 default are unchanged.

First device use remains a validation step: inspect engine build and model identity, compare
TensorRT outputs/reset, then measure sustained inference timing and temperatures before any
driving evaluation. No device was installed, restarted or tested during this PC task.
