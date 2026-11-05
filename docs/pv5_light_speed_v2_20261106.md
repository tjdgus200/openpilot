# PV5 Light speed v2

## Provenance and contract

Official model: [Hugging Face commit ea306f53](https://huggingface.co/commaai/openpilot_driving_models/commit/ea306f534159ac9a3caab0ac3bd4a5f82f9cd805).
The reviewed companion catalog/metadata are from
[carrot-wip 612881c4](https://github.com/ajouatom/openpilot/commit/612881c4d9052baf0de8246ea2226a23f70a573f).

- Catalog key: `light-speed-2`; label: `Light speed v2 (ea2f516f)`.
- ONNX SHA-256: `909a140019050a9d7f00db22dd4f4085928d2af9474a82244672ddab964f95a8`.
- Size: 768,749,892 bytes; download is pinned to the official HF revision above.
- Checkpoint: `f22e06e5-3b3f-48d2-a6b9-2f32dc20e707/56320/ea2f516f-f758-42be-a245-913bb818a64f`.
- Actual graph inputs: uint8 `new_img[2,6,128,256]`, float32 desire[8],
  traffic_convention[1,2], action_t[1,2]; uint8 state_img_q[2,5,6,128,256],
  float32 state_desire_q[132,1,8] and state_feat_q[128,1,16384].
- Output: float32 outputs[1,18452] plus all three matching next-state tensors.
  Every output slice is pinned in `model_catalog/light-speed-2.json`.
  No navigation inputs/NOO. The shape contract matches v1; weights and identity differ.

## Delivery and preserved behavior

Keep N16 as the default and retain N16, Flip Flop, CTV3M and Light speed v1.
Only expose v2 when `pv5-usb-light-speed-2.json` matches its exact model hash/size.
Export a distinct committed PV5 package with v2 in both bootstrap spec filenames.
Never substitute the upstream non-PV5 host or another model's package.
Selection stays frozen throughout the current boot, including worker recovery.
First use needs an Internet download and a new TensorRT engine; choose the next-boot
model while parked with ignition off, then reboot comma and Jetson.

Preserve all PV5 HUD/BLE/battery/108-channel heatmap, lane probability/steering/center
gauges and alert priorities, black boot diagnosis, 1Hz resource footer, Q92/FPS,
model-priority scheduling, USB input-power block and Park-update behavior.
GPU recurrent state, supported CUDA graphs, input overlap and shared-RAM negotiation
retain their existing defaults. Exact N16-only casting/map/output/fusion gates do
not apply to v2. Performance diagnostics stay off. No control, camera warp, FOV,
cruise or actuator policy changes are included.

## PC verification and limits

The complete official download matched SHA-256 and size. Actual graph metadata,
tensor types, checkpoint and slices matched the reviewed fixture/pin. Both original
and production TensorRT-input-patched graphs passed full ONNX checking. On CPU,
four synthetic changing-image/recurrent/reset frames produced finite, exactly
equal outputs and next-state tensors for the original versus patched graph.

Selector, boot freeze, wrong-package rejection, parser/publication, reset, existing
optimization gates and isolated package import/render checks cover the integration.
PC CPU equivalence does not establish Orin TensorRT/FP16 correctness, throughput,
P99, thermal stability, physical USB delivery or vehicle safety. No physical device
was installed, rebooted or road-tested. The companion eGPU PKL metadata is retained
from upstream; PC ONNX tests are not eGPU artifact execution tests.
