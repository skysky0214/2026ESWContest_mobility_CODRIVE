# Geometry lane tuning — 2026-10-05

Current camera default: colour geometry with GeometricLaneTracker, one boundary maximum on each side, 523×294 preview.

Adjusted near-camera oblique paint acceptance: maximum |dx/dy| 1.5→3.0, component height 7→5 pixels at 640×360, reference row 0.90 instead of image bottom. White/yellow HSV masks retained.

Candidate weights: paint support 0.25, vertical coverage 0.20, alignment 0.25, linked pieces 0.10, proximity to camera axis 0.15, recent history 0.05. Minimum score 0.50. Coordinates blend 65% current / 35% previous only for nearby same-colour observations. History expires after 250ms; a missing boundary is immediately removed.

Visual review: eight extracted frames from test_driving.mp4 and UFLD example.mp4. Comparison images remain local and are not included in Git. Road frames 0 and 240 now have both sides; frame 480 now has right paint. Road 120 still misses the right dash. Urban 120 retains a crosswalk-related false positive. These are qualitative checks, not annotated accuracy measurements or independent test-set results. Further road-scene validation is needed before departure decisions; camera pose/vehicle centre calibration is not provided by this change. New lane scores are overlay/log data, not wired into legacy CODRIVE lane_confidence.

Validation: 62 pytest tests passed, including oblique dashed paint and removal of stale lanes. Live /dev/video0 run: 90 frames, 14.7676 FPS from host receipt timestamps, mean processing 43.5843ms including YOLO and colour lane processing. This is not sensor-to-display latency. Previous colour run: 14.5145 FPS, 43.154ms; runs are sequential, not controlled simultaneous comparisons.

Configuration: `src/codrive/sensor_input/external/lane/geometric_weights.json`.
Pre-tuning backups remain local.
