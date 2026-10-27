# Jetson 실행과 전방 인지

전방 Jetson 실행 코드는 `src/codrive/apps/jetson_front`, 재사용 가능한 인지 코드는
`src/codrive/sensor_input/external`, 카메라 드라이버는 `src/codrive/hardware/camera`에 있다.
실내 Jetson의 DMS는 `src/codrive/apps/jetson_cabin/dms.py`에서 실행한다.

JetPack이 제공하는 GStreamer OpenCV, PyGObject, CUDA, TensorRT를 사용한다. 일반 PyPI OpenCV
wheel로 교체하면 GStreamer 입력이 사라질 수 있다. TensorRT engine은 대상 Jetson에서 빌드하고
`artifacts/models/vision/` 아래에 저장한다.

## 전방 Jetson 실행

프로젝트 루트에서 실행한다.

```bash
deploy/jetson-front/run_camera.sh
deploy/jetson-front/run_video.sh

source deploy/jetson-front/activate.sh
cd "$CODRIVE_REPO_ROOT"
python -m codrive.apps.jetson_front --video /path/to/video.mp4
python -m codrive.apps.jetson_front --video /path/to/video.mp4 \
  --headless --max-frames 60 --dry-run --log artifacts/logs/front-video.jsonl
```

기본 모델 경로는 `artifacts/models/vision/yolo11n.engine`이다. 녹화 영상은
`artifacts/recordings/`에 둔다. Q/Esc로 preview를 종료한다.

기본 실행은 DDS domain 42에 `ExternalPerception`을 발행한다. 센서 단독 확인이나 재생 시험에서
DDS가 필요 없을 때만 `--dry-run`으로 `InMemoryBus`를 선택한다. heartbeat 발행은 아직 별도
supervisor 연결이 필요하다.

전방 영상은 DDS와 분리된 `http://<jetson-front-ip>:8080/stream.mjpg` MJPEG 경로로 제공한다.
기본값은 960 px, 12 fps, JPEG quality 70이며 최신 프레임 한 장만 유지한다. 느린 HMI 때문에
인지 프레임이 적체되지는 않는다. 필요하면 `--hmi-stream-fps`, `--hmi-stream-width`,
`--hmi-stream-quality`로 조절하고, 영상을 끄려면 `--no-hmi-stream`을 사용한다.

## 모델 변환과 benchmark

```bash
PYTHONPATH=src python tools/model_export/yolo_tensorrt.py
PYTHONPATH=src python tools/model_export/ufld_tensorrt.py
PYTHONPATH=src python tools/benchmarks/front_perception.py \
  --model artifacts/models/vision/yolo11n.engine \
  --output artifacts/reports/front-benchmark.json
```

Geometric 차선 설정은
`src/codrive/sensor_input/external/lane/geometric_weights.json`에 있다. UFLD checkpoint와 engine은
`artifacts/models/vision/ufld/`에 둔다. UFLD upstream license는 구현과 같은 디렉터리에 보관한다.

## 테스트

```bash
PYTHONPATH=src PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  pytest -q tests/sensor_input/external/test_lane_geometric.py
```

차선 결과는 아직 preview/log 정보이며 calibration 기반 차선 이탈 판단까지 연결되지 않았다.
처리 시간 측정도 sensor capture부터 HMI까지의 전체 지연을 의미하지 않는다.
