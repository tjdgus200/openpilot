# PV5 radar track class labels

The paired Jetson releases annotate measured front radar track IDs with COCO classes on compatible dual-camera PV5s. The Rust worker returns a sparse snapshot of class, confidence and expiry per ID. A missing or expired entry means unknown. The snapshot has no subscribers in vehicle control, lead selection or warning code.

The comma downloads its pinned release through the existing model update flow. The Jetson bundle contains the ARM64 association worker, the Rust/CUDA detector library and the pinned ONNX. During candidate validation the Jetson builds a local TensorRT FP16 plan and runs one synthetic wide frame through the installed detector. Subsequent model switches reuse a compatible cached plan after another load and inference check. The Jetson needs the image's TensorRT Python runtime. Installation does not contact a model registry or compile Rust on the device. The host and HUD enable labeling automatically for a validated release. Missing or incompatible label assets leave the driving model available and yield unclassified tracks.

## Data path

1. On a real dual-camera PV5, a calibrated modeld requests classification on each Jetlink v2 frame if the host advertised wide-label support. The existing image block contains road and wide camera input in that order. Modeld tags successful replies with the wide exposure midpoint and a sparse wide pixel fingerprint. The projection uses the wide extrinsics and the actual model warp, including the selected FOV and pitch.
2. The Jetson submits its driving graph, then offers the second camera slice to a Rust FP16 detector through a separate host thread in the same process and CUDA primary context. YOLO uses the current 512×256 wide view; it unpacks YUV, reconstructs RGB and letterboxes it for the detector. No additional USB image transfer is required. One local 196608-byte host copy lets the primary transport reuse the receive buffer. A busy detector skips the new frame. Model output never waits for YOLO. Successful main replies authorize the detector to forward boxes.
3. The CPU-only Rust association process joins boxes to batched HUD metadata by exact frame and wide fingerprint. It checks radar age, calibration and unambiguous repeated matches before publishing expiring per-ID labels. The Jetson HUD selects the shape for that ID. If the renderer has a newer radar sample, the association requires one matching measured front track with the same ID and continuous motion within 100 ms. Ambiguity or expiry clears the class. Detector cadence and HUD refresh are independent.

The 3D mesh uses the same forward vector as the object's HUD box. For a classified raw front-radar marker, the renderer uses the existing radar-box velocity rule and its forward fallback when motion is unavailable or too small. The displayed heading is an estimate. A moving pedestrian's body can face a different direction.

Meshes have nominal display dimensions in metres. The car uses the original vehicle box footprint, including its compact marker scale. The same mesh keeps its world dimensions at every distance, so the scene camera supplies the perspective scaling. The dimensions describe HUD artwork and do not measure the tracked object.

In the wide-camera view the renderer scales each classified silhouette from the camera projection at the track's distance. It anchors the sprite at the projected ground point and uses the visible pixels of the selected atlas view when sizing it. This lets distant silhouettes continue shrinking below the unclassified box's minimum size. The atlas bounds are read once when the HUD opens.

Geometry and per-face lighting are uploaded once. The camera overlay draws one textured quad per classified object, selecting the closest of 16 views relative to the line of sight. The 384×1024 RGBA atlas is baked on a PC with `python tools/pv5_radar_labels/bake_hud_atlas.py`. Distance and speed follow the existing display settings. When the normal scene only includes corner tracks, a confirmed front track can supply its own marker at the measured position. Neighboring vision or corner detections do not inherit its class. The renderer shares the existing decoded radar event and checks for a new atomic label file at most once per 50 ms time slot while drawing the driving view. Repeated draws reuse the decoded classes and projected points. Expiration is checked on each draw.

The two engines have distinct CUDA streams. Available SM, register and memory resources determine actual overlap. A low-priority auxiliary stream cannot reserve spare cores or bound the time of a running kernel. Bench main-only vs concurrent under the intended power mode, including first engine load and thermal steady state.

## Prepare on a Jetson bench

Use the original Ultralytics YOLOv5n v7 ONNX from the [official release](https://github.com/ultralytics/yolov5/releases/tag/v7.0). `build_engine.py` verifies its bytes, builds a GPU-specific FP16 plan with the installed TensorRT, and smoke-tests loading it. The build needs JetPack CUDA and TensorRT development headers, CMake and Rust. Engine compilation competes for GPU resources; finish compilation before starting a timing capture.

```sh
cd tools/pv5_radar_labels
python3 native/build_engine.py /path/to/yolov5n.onnx /path/to/yolov5n.plan
cargo build --locked --release --features tensorrt --lib
cargo build --locked --release --no-default-features --bin radar-label-worker
target/release/radar-label-worker /run/pv5-radar-labels
```

Run the worker, host and renderer under the same local account with a private (0700) runtime directory. For a custom location pass it to the worker and set `PV5_RADAR_LABEL_DIR` on both the host and renderer. Set `PV5_RADAR_LABELS=1` for comma modeld and its HUD publisher, and for the Jetson host and renderer. For the GPU host set `PV5_RADAR_LABEL_LIB` to the absolute path of `libpv5_radar_labels.so` and `PV5_RADAR_LABEL_PLAN` to the plan compiled for that Jetson. Jetlink v3 and older peers retain their driving model path. Label expiry uses Jetson CLOCK_MONOTONIC. The source radar timestamp uses the vehicle clock.

## Reproduce offline

```sh
cargo test --locked
python3 test_transport.py
python3 replay_video.py SCENE-road_warp.mkv SCENE.yuv6
target/release/replay-detector /path/to/yolov5n.plan < SCENE.yuv6 > detections.ndjson
target/release/replay_labels AUDIT_DIR SCENE road_warp detections.ndjson > labels.ndjson
```

The example above reproduces the earlier narrow-camera baseline at its original sampling cadence. For the new path supply actual 512×256 wide-model images, wide exposure timestamps and matching per-frame projection and use `wide_warp` for replay. A missing wide-camera recording precludes validating its detections. Synthetic input is sufficient to measure GPU scheduling. The older `bench_pair.py` measures process-level timing only; use CUDA kernel activity tracing to establish cross-engine overlap.
