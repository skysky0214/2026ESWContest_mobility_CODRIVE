# 3-board 배포 기준

## 네트워크

유선 Gigabit Ethernet switch에 세 보드를 직접 연결한다. 데모용 기본 주소는 다음과 같다.

| host | address | DDS domain |
|---|---|---:|
| `pi5` | `192.168.50.10/24` | 42 |
| `jetson-front` | `192.168.50.21/24` | 42 |
| `jetson-cabin` | `192.168.50.22/24` | 42 |

인터넷/Wi-Fi는 LLM 같은 비안전 기능에만 사용한다. `config/cyclonedds.xml`의 interface 자동
선택은 개발 편의를 위한 값이다. 실차에서는 각 보드의 유선 NIC 이름으로 고정하고 방화벽에서
DDS discovery/data UDP와 `jetson-front`의 HMI 영상 TCP 8080을 허용한다. switch가 multicast를
막아도 세 static peer로 discovery한다. 영상 포트는 대회용 격리 유선망에서만 노출한다.

```bash
# Debian/Ubuntu에서 venv 생성이 실패하면 먼저 python3-venv를 설치한다.
python3 -m venv --system-site-packages .venv
. .venv/bin/activate
export CYCLONEDDS_URI="file://$PWD/config/cyclonedds.xml"
PYTHONPATH=src PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest -q
PYTHONPATH=src python -m codrive.simulation.simulator
```

명령은 `ESW2026` 프로젝트 루트에서 실행한다. 장치 설정의 TensorRT engine 경로는
`artifacts/models/`를 사용하고, 녹화·로그는 `artifacts/recordings/`, `artifacts/logs/`에 둔다.
장비별 Python 실행점은 `deploy/README.md`에 정리되어 있다.

## Pi HMI와 CAN 표시

Pi orchestrator를 실행하면 HMI 서버도 기본 포트 8090에서 함께 시작한다.

```bash
export CYCLONEDDS_URI="file://$PWD/config/cyclonedds.xml"
PYTHONPATH=src python -m codrive.apps.raspberry_pi
```

Pi 화면에서는 `http://127.0.0.1:8090/`, 다른 개발 PC에서는
`http://192.168.50.10:8090/`을 연다. `/api/vehicle`은 `vehicle_state`의 속도(km/h), 조향각,
브레이크, 가속페달을 읽기 전용으로 제공한다. 500 ms 동안 갱신되지 않거나 CAN decoder가
`can_valid=false`를 내보내면 HMI에 `CAN 연결 대기`가 표시된다. 포트는
`--hmi-web-port`, 기능 비활성화는 `--no-hmi-web`으로 지정한다.

Jetson에서는 JetPack/carrier BSP가 제공하는 GStreamer 지원 OpenCV와 TensorRT를 사용한다.
일반 PyPI `opencv-python-headless` wheel은 GStreamer가 빠져 있을 수 있으므로 실기 배포 의존성에
넣지 않았다. 개발 PC에서 카메라 없이 import만 시험할 때만 `.[vision-dev]`를 선택한다.

## 시간 동기화

Pi 5를 PTP grandmaster로, 두 Jetson을 slave로 설정한다. 부팅 후 offset이 5 ms 이내로 들어오기
전에는 노드 상태를 `STARTING`으로 유지한다. 하드웨어 timestamp를 지원하지 않는 NIC라면 같은
유선망에서 chrony를 사용하고 실제 p99 offset을 기록한다.

## Jetson 확인 순서

1. carrier vendor의 정확한 JetPack/BSP와 device tree를 설치한다.
2. 각 GMSL stream을 `v4l2-ctl --list-devices` 및 vendor/Argus 도구로 단독 검증한다.
3. `hardware/camera/sources.py`가 만든 GStreamer pipeline으로 해상도/FPS/timestamp를 검증한다.
4. ONNX를 해당 Jetson에서 TensorRT engine으로 빌드한다. `.engine` 파일은 다른 GPU/JetPack에서
   만든 것을 복사하지 않는다.
5. 30분 soak test에서 capture drop, inference p50/p95/p99, 온도와 throttling을 기록한다.

Jetson B의 내부 카메라 DMS는 C922 기준 `/dev/video0`, `MJPG 640x480@15`를 기본으로 둔다.
YOLO TensorRT engine을 Jetson B에서 빌드한 뒤 다음처럼 `DriverState` 발행을 확인한다.

```bash
PYTHONPATH=src python -m codrive.apps.jetson_cabin.dms \
  --model artifacts/models/vision/dms.engine --device-index 0 --width 640 --height 480 \
  --fps 15 --input-format MJPG --print-json --max-frames 30
```

개발 PC에서 화면으로 먼저 확인할 때는 preview 전용 루프를 사용한다.

```bash
PYTHONPATH=src python -m codrive.sensor_input.driver.dms_preview --model artifacts/models/vision/dms.pt --device-index 0 \
  --width 640 --height 480 --fps 15 --input-format MJPG --device 0 --no-gstreamer
```

## Panda/EV6 확인 순서

Panda USB 권한과 공식 userspace library를 설치한 뒤 **수신만** 확인한다. 실제 신호 해석은
차량의 정확한 fingerprint와 검증된 DBC가 준비될 때까지 활성화하지 않는다.

1. 시동 OFF/ON 각각에서 bus와 CAN-FD 여부를 기록한다.
   nominal/data bitrate와 CAN-FD auto 설정은 이 결과가 확정된 뒤 적용한다.
2. 정차 로그에서 모든 speed 신호가 0인지 확인한다.
3. 저속 폐쇄 구간에서 계기판 속도·조향·브레이크·방향지시등과 decoded 값을 대조한다.
4. counter/checksum 오류가 있는 프레임은 상태에 반영하지 않는다.
5. 코드·설정·운영 계정 어디에도 Panda 송신 경로를 두지 않는다.

## 시험 게이트

- 단위 시험: parser, serialization, stale/sequence, risk priority, cooldown
- 재생 시험: 시간 동기화된 CAN/INS/perception 기록으로 모든 발표 시나리오 반복
- 네트워크 시험: 한 Jetson cable 제거, multicast 차단, packet loss/latency 주입
- HMI 시험: CRITICAL 문구 3~4단어, TTS 겹침 방지, 경고 우선순위
- 실차 시험: 폐쇄 구간, 안전 운전자/감독자 동승, advisory-only 확인 후 진행
