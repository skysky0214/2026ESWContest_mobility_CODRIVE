# UFLDv2 CULane ResNet18 적용 — 2026-10-05

Official source: https://github.com/cfzd/Ultra-Fast-Lane-Detection-v2
Weights: CULane R18, official Drive ID 1oEjJraFr-3lxhX_OXduAGFWalWa6Xh3W. Checkpoint loaded with strict=True; inference-only network follows upstream names/shapes. Upstream MIT license is in .deps/ufld-v2/LICENSE.

Model input: RGB; resize to 1600×533, crop bottom 320 rows, ImageNet mean/std. Official row slots 1/2 used for left/right ego lanes; at most one polyline each. At least 37/72 row anchors must have existence probability >0.6. Paint support: at least 3 and 10% of selected anchors near white/yellow pixels. No arbitrary Hough lines.

FP16 ONNX/engine: models/culane_res18.onnx, models/culane_res18.engine (~394MB). export_ufld.py rebuilds it. Initial FP32 export hit OOM; FP16 export completed. Engine builder skipped one memory-heavy tactic and completed. No temporary swap or boot changes were made.

## Sequential 150-frame file benchmark

| Method | FPS | additional lane step mean ms |
|---|---:|---:|
| colour interval 1 | 16.68 | 17.0 |
| ufld interval 1 | 10.07 | 57.6 |
| ufld interval 2 | 14.39 | 29.4 |
| ufld interval 3 | 16.88 | 21.0 |

Same YOLO TensorRT and hardware decode, 1280×720 frames, 384×640 YOLO input. No display/network/TTS. Existing publisher still computes legacy lane_confidence independently; new lane model output is a preview/log boundary and not wired into RiskEngine. The interval-3 run additionally uses a dedicated CUDA stream for UFLD.

## Live CAM1, 90 frames each

| Method | measured loop FPS | mean processing ms |
|---|---:|---:|
| colour | 14.51 | 43.2 |
| UFLD interval 3 | 11.52 | 58.2 |
| UFLD interval 5 | 13.02 | 52.7 |

Default run_camera.sh now uses UFLD every 5 processed frames. This gives ~2.6 lane updates/sec at 13fps. Cached polylines expire after 250ms, so they can disappear before the next inference when processing is slow. Processing times do not include sensor exposure/transport or display presentation. Frame logging records host receipt timestamps, not guaranteed sensor capture timestamps.

Lane model successfully ran on the provided driving video; annotated samples ufld-frame-0/60/120.jpg. Actual live camera was an indoor scene with zero detected lane boundaries during the 90-frame tests. Thus live pipeline is validated, but live monitor lane accuracy and white/yellow dashed-line accuracy are NOT established. No ground truth accuracy evaluation performed. Fixed training perspective may fail when a road video occupies only part of the camera image. Curbs or road boundaries may be predicted.

60 existing/synthetic tests passed; pip check clean. Model inference itself was validated by real benchmark/camera runs. PyTorch CC8.7 warning still occurs in the YOLO wrapper.

## Run

```bash
cd /home/one/ws/adas
./run_camera.sh
./run_camera.sh --lane-method colour  # return to previous detector
./run_camera.sh --lane-interval 3  # faster lane updates, lower camera FPS
```

Vehicle center/calibration, temporal lane identity, and lane departure risk integration remain unimplemented. Current output is lane polylines, not a validated departure warning.
