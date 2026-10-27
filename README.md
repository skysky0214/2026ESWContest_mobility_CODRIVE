# CODRIVE · ESW 2026

인지 기능 저하 운전자를 위한 멀티모달 상황 해석 기반 운전 보조 프레임워크다.

- Jetson Orin Nano A: GMSL 전방 카메라 객체·신호등·차선 추론
- Jetson Orin Nano B: USB 내부 카메라 DMS, STM32 USART 음향 추론, 선택 rear/side GMSL
- Raspberry Pi 5: 실차 EQ900/CANable 조회 또는 CARLA/STM32 CAN 입력, 상황 융합, 팀 FSM/TTC, 로컬 HMI·여성 TTS
- 현재 통합 구성에 INS는 없으며, 실차 거리·TTC는 카메라 장착 보정 전이다.
- 장치 간 통신: CycloneDDS domain 42
- 지연 목표: CRITICAL `capture → HMI dispatch` 200 ms 이내. 목표이며 현재 달성 보장이 아니다.

이 저장소는 차량 제어기가 아니라 **운전 보조 안내 시스템**이다. Panda/SocketCAN 경로는 수신 전용이며,
EQ900 실차 경로는 저장소에서 검증한 고정 진단 데이터 조회·ISO-TP 흐름제어만 송신한다. 차량 제어·ECU 쓰기 API는
노출하지 않으며 조향·제동·가속 명령을 보내지 않는다.

> 실제 보드 통합 실행은 `board_runtime`의 Pi 앱과 `src`의 Jetson 앱을 사용한다.
> 보존된 `src` 시뮬레이터·구형 Pi 예제와 현재 보드 실행 경로는 구분한다.
> `codrive-sim`의 시간은 Pi 내부 계산만 측정하므로 capture→실제 GUI/TTS의 200ms 달성을 증명하지 않는다.

## 통합 시연 / 실차 실행

공유 기준은 `ESW2026-latest`의 현재 통합 코드다. 먼저 **[세 모드 실행 안내](docs/INTEGRATION_MODES.md)**를 읽는다.

```bash
scripts/select-integration-mode.sh carla          # CARLA 가상 전방카메라 직접 입력
scripts/select-integration-mode.sh carla-screen   # 실물 외부카메라로 CARLA 모니터 촬영
scripts/select-integration-mode.sh real-vehicle   # 실물 외부카메라 + EQ900/CANable
```

프로그램·인식 모델은 공통으로 사용하고 입력·보정·차량 데이터 출처를 프로필로 전환한다.
`carla-screen`은 기본적으로 옛 화면 거리 보정을 적용하지 않는다. 카메라/모니터 고정·재보정 후에만
`--screen-calibrated`를 명시한다. 내부카메라 DMS와 마이크는 전방 입력 전환과 별개다.

현재 Jetson/Pi의 기존 서비스·가상환경·모델 설치를 전제로 한다. Git clone만으로 보드 설치까지 완료되지는 않는다.
소스 변경 배포와 실행 모드 전환은 별개이며, 전환 스크립트는 소스를 덮어쓰거나 차량을 움직이지 않는다.

## 현재 구현된 골격

- 공통 timestamp/trace를 가진 typed domain model과 엄격한 JSON round-trip
- CycloneDDS IDL envelope, 10개 고정 topic, traffic별 QoS, keyed multi-publisher history
- 두 Jetson의 static sharding용 perception publisher 경계
- Pi의 TTL/sequence와 bounded history 기반 `SituationSnapshot` fusion
- stream source allowlist와 3회 heartbeat 누락 감시
- 도로/운전자/음향 후보 기반 RiskEngine과 4단계 hysteresis FSM
- 통신 없는 `DecisionPipeline`, 매 주기 `RiskAssessment`, YAML 규칙 설정과 모의 시나리오
- CRITICAL용 짧은 한국어 화면 문구와 로컬 TTS template
- 최신/구형 comma.ai Panda `can_recv()` 호환 수신 전용 adapter
- `BESTPOSA`, `HEADING2A`, `GPGGA` parser와 local ENU/yaw 변환
- CRC32·재동기화를 포함한 STM32 PCM16 USART protocol
- AI Hub 음향 데이터 manifest, 16 kHz log-mel, 사이렌·경적 CNN 학습·평가·실시간 추론
- Jetson Argus/V4L2 GMSL 및 USB webcam GStreamer source builder
- 브라우저 HMI 화면과 NORMAL/CAUTION/WARNING/EMERGENCY 데모 시나리오
- 적색 신호 접근 end-to-end simulator와 단위 시험

모델 TensorRT engine, 실제 EV6 DBC, 카메라 calibration과 HMI 실시간 데이터 연동은 하드웨어가 확정된 뒤 연결해야
하는 plug-in 경계로 남겨 두었다. 신호 주소를 추측해 실차 CAN에 적용하지 않는다.

## 사이렌·경적 음향 인식

음향 코드는 `src/codrive/sensor_input/audio/`에 있다. AI Hub 도시 소리 데이터의 TAR 내부
분할 ZIP을 압축 해제 없이 읽어 manifest를 만들고, 16 kHz log-mel 기반 경량 CNN을 학습·평가한다.
실시간 입력은 STM32 PCM16 프레임 또는 ALSA 마이크를 받아 `AudioEvent`로 변환하며,
CycloneDDS의 `codrive/v1/perception/audio` 토픽으로 발행할 수 있다.

```bash
PYTHONPATH=src python -m codrive.sensor_input.audio.manifest \
  --dataset-root /path/to/aihub_city_sound \
  --output artifacts/datasets/audio/city_sound_manifest.jsonl

PYTHONPATH=src python -m codrive.sensor_input.audio.train \
  --manifest artifacts/datasets/audio/city_sound_manifest.jsonl \
  --output-dir artifacts/models/audio/model \
  --device cuda --export-onnx
```

바로 시험할 수 있는 초기 ONNX 모델, PyTorch 체크포인트, 메타데이터와 평가 보고서는
`artifacts/models/audio/initial_model/`에 포함되어 있다. 학습, 마이크 입력, STM32 프레임,
DDS와 RViz 확인 방법은 [음향 인식 파이프라인](docs/AUDIO_PIPELINE.md)을 참고한다.

## 빠른 확인

```bash
cd ESW2026
python3 -m venv --system-site-packages .venv
. .venv/bin/activate
export CYCLONEDDS_URI="file://$PWD/config/cyclonedds.xml"
PYTHONPATH=src PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest -q
PYTHONPATH=src python -m codrive.simulation.simulator
```

개발 PC에 ROS의 pytest plugin이 설치되어 있고 의존성이 충돌하면 격리해서 실행한다.

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest -q
```

## HMI 화면

Pi 런타임을 실행한 뒤 브라우저에서 `http://127.0.0.1:8090/`을 연다.

```bash
export CYCLONEDDS_URI="file://$PWD/config/cyclonedds.xml"
PYTHONPATH=src python -m codrive.apps.raspberry_pi
```

Pi의 HTTP 브리지는 DDS `vehicle_state`를 융합한 최신 `SituationSnapshot.vehicle`에서 속도,
조향각, 브레이크, 가속페달을 읽어 `/api/vehicle`로 제공한다. HMI는 이를 20 Hz로 반영하며
500 ms 이상 Pi 갱신이 없거나 `can_valid=false`이면 `CAN 연결 대기` 상태로 전환한다.
CSS와 JavaScript는 [HMI HTML](src/codrive/hmi/web/index.html)에 포함되어 있어 별도 프런트엔드
빌드는 필요 없다.
숫자 키 1~4로 위험 단계별 데모를 전환하고, T 키로 테스트 버튼을 숨기거나 표시한다.
날씨·시간 조회에는 인터넷 연결이 필요하다.

가운데에는 `jetson-front`의 외부 카메라 MJPEG 영상이 표시되고 위험 안내가 그 위에 겹쳐진다.
기본 주소는 `http://192.168.50.21:8080/stream.mjpg`이며 다른 주소는 다음처럼 지정한다.

```text
index.html?camera=http%3A%2F%2FJETSON_IP%3A8080%2Fstream.mjpg
```

`?camera=off`로 영상 연결을 끌 수 있다. 영상 스트림은 비안전 표시 경로이며 인지 결과와 위험
이벤트는 계속 CycloneDDS 토픽으로 전달한다.

HTML 파일을 직접 열어도 기본적으로 `http://127.0.0.1:8090/api/vehicle`에 연결한다. 다른 Pi의
브리지는 `?vehicle=http%3A%2F%2FPI_IP%3A8090%2Fapi%2Fvehicle`로 지정하고, 차량값 없이 화면
시나리오만 시험할 때는 `?vehicle=off`를 사용한다. 화면 데이터는
`window.CODRIVE_HMI.setData({...})`로도 갱신할 수 있다. 차량값은 실제 토픽과 연결됐고,
위험 안내용 DDS `HmiCommand`와 실제 TTS 연결은 별도 후속 작업이다.

## 센서 없이 위험도 판단 실행

센서별 코드와 토픽·통신 방식은 이후 연결한다. 현재 코어는 공통 모델을 입력받아
Snapshot 생성, 위험 후보 점수 계산, 우선순위 선택, FSM과 HMI 템플릿 생성을 실행한다.
CycloneDDS 설치 없이 프로젝트 루트에서 모의 데이터를 확인할 수 있다.

```bash
PYTHONPATH=src python3 -m codrive.simulation.scenarios --policy config/risk.yaml
PYTHONPATH=src python3 -m codrive.simulation.scenarios --policy config/risk.yaml --details
```

나중에 센서 adapter는 `DecisionPipeline.ingest()`에 공통 모델을 전달하면 된다.
입력 단위, 점수 계산, 데이터 누락, FSM 상태와 테스트 방법은
[규칙 기반 위험 판단](docs/RULE_BASED_RISK.md)을 참고한다.

## 구조

기능 코드는 한 번만 저장하고 `apps/`와 `config/devices/`에서 두 Jetson과 Pi의 실행 역할을
구분한다. 공통 메시지·토픽 계약은 `contracts/`, 장치별 실행 조합은 `apps/`가 소유한다.

```text
ESW2026/
├── config/
│   ├── devices/              # jetson-front, jetson-cabin, pi5 배포 profile
│   ├── can/                  # EV6 DBC 검증 전 placeholder
│   ├── rviz/                 # 음향 인식 디버그 대시보드
│   ├── risk.yaml             # 점수, 가중치, TTC, FSM 설정
│   └── cyclonedds.xml
├── docs/
│   ├── ARCHITECTURE.md       # 배치, topic/QoS, latency, fail-safe
│   ├── AUDIO_PIPELINE.md     # 음향 데이터·학습·실시간 추론·RViz
│   ├── DEPLOYMENT.md         # 3-board 설치·검증 순서
│   ├── DEVICE_ROLES.md       # 두 Jetson과 Pi의 역할
│   ├── REQUIREMENTS.md       # 발표자료 요구사항 추적
│   ├── RULE_BASED_RISK.md     # 통신 없는 판단 코어와 입력 계약
│   ├── CODE_STRUCTURE.md      # 계층 책임, 코드 경로, 의존 관계
│   └── STM32_AUDIO_PROTOCOL.md
├── src/codrive/
│   ├── apps/                 # jetson_front, jetson_cabin, raspberry_pi
│   ├── contracts/            # 모델, envelope, 토픽 단일 정의
│   ├── hardware/             # camera, CAN, INS, audio 장치 경계
│   ├── sensor_input/
│   │   ├── external/         # 외부 객체·신호등·차선
│   │   ├── driver/           # 내부 DMS
│   │   ├── audio/            # 사이렌·경적
│   │   ├── vehicle/          # CAN/INS gateway
│   │   └── environment/      # 날씨·시간
│   ├── situation/            # timestamp/TTL과 SituationSnapshot
│   ├── risk/
│   │   ├── engine.py         # 위험 후보, 점수, 우선순위
│   │   ├── policy.py         # YAML 규칙 설정
│   │   └── fsm.py            # 4단계 상태 안정화
│   ├── guidance/             # 로컬 template + 선택적 LLM
│   ├── hmi/                  # web, qt, tts
│   ├── monitoring/           # heartbeat/watchdog/latency
│   ├── reporting/            # 이벤트·안전점수·주행리포트
│   ├── transport/            # CycloneDDS + in-memory bus
│   └── simulation/           # simulator.py, scenarios.py
├── deploy/                   # 보드별 실행 스크립트와 서비스
├── tools/                    # model export, benchmark, calibration
├── tests/                    # src 구조를 따르는 단위·통합·smoke 시험
└── artifacts/                # 모델·녹화·로그; 내용은 Git 제외
```

TensorRT engine 경로는 프로젝트 루트 기준 `artifacts/models/`다.
자세한 계층 책임과 기존 파일의 새 위치는 [코드 구조](docs/CODE_STRUCTURE.md)를 참고한다.

전체 설계 결정과 남은 하드웨어 확정 항목은 [시스템 아키텍처](docs/ARCHITECTURE.md), 실제
보드 준비 순서는 [배포 기준](docs/DEPLOYMENT.md)을 참고한다.

## 다음 구현 순서

1. GMSL 센서·carrier·JetPack 버전을 확정하고 카메라별 calibration 저장
2. YOLO/차선/DMS/audio 모델을 ONNX→TensorRT로 변환하고 backend 연결
   - DMS는 `DriverState.attention_score`, `looking_forward`, `drowsiness_score`,
     `microsleep_ms`, `eyes_closed`를 채우는 adapter부터 연결
   - 내부 USB 카메라 YOLO DMS publisher는
     `PYTHONPATH=src python -m codrive.apps.jetson_cabin.dms --model artifacts/models/vision/dms.engine`로 실행
   - 화면 확인용 preview는 `PYTHONPATH=src python -m codrive.sensor_input.driver.dms_preview --model artifacts/models/vision/dms.pt
     --device-index 0 --no-gstreamer`로 실행
   - 전방 GMSL 카메라 YOLO 객체·신호등·차선 publisher는
     `PYTHONPATH=src python -m codrive.apps.jetson_front --model artifacts/models/vision/yolo11n.engine --sensor-id 0`로 실행한다.
     YOLO의 `person`, `bicycle`, `car`, `motorcycle`, `bus`, `truck`, `traffic light`,
     `stop sign`을 발행하며 신호등 색상과 차선은 OpenCV 후처리한다.
3. 2022 EV6 정확한 fingerprint/DBC를 확보해 녹화 CAN으로 signal decoder 검증
   - 수신 로그는 `{"ts","address","bytes","bus"}` JSONL 형식으로 주입할 수 있다.
     `codrive.hardware.can.ev6_file.Ev6CanFileDecoder`는 hw 브랜치의 EV6 wheel speed,
     steering, accelerator, brake 신호만 읽어 `VehicleState`로 변환하며 Panda 송신 기능은 없다.
   - `matplotlib` 환경에서 파일 신호 시각화:
     `PYTHONPATH=src python tools/visualization/ev6_can_file.py --input ev6_can.jsonl
     --output ev6_can_plot.png --csv ev6_can_decoded.csv`
4. INS port/rate/CRC와 ENU 원점을 확정하고 STM32 firmware를 wire protocol v1에 맞춤
5. Pi HMI의 `HmiCommand`/TTS 연결 후 packet loss·노드 장애·실차 폐쇄구간 시험

## Jetson camera and video preview

See [Jetson 실행 문서](docs/jetson/README.md) for GMSL camera/video replay, TensorRT export, colour lane tracking and traffic-signal confirmation.

## STM32 ECU 및 G29 CAN 실험

`stm32/` 폴더에 STM32H723 ECU 펌웨어와 PC용 G29 입력·보정·CAN 송신·UART 수신 확인 도구가 포함되어 있습니다.
파일 구성과 설치·빌드·배선·실행 방법은 [STM32 / G29 실행 안내](stm32/README.md)를 참고하세요.
G29 송신 도구는 벤치 테스트용이며 위의 Panda 수신 전용 경로와 별도로 실행합니다.


## 실제 보드 통합 실행

2026-10-26 최종 센서 규칙: 객체·신호등·DMS·방향별 사이렌/경적·급페달/급가속과 환경 가중치를 연결했다. 날씨는 1시간마다, 일출·일몰은 하루 한 번 조회한다. [설정·입력·검증 안내](docs/FINAL_SENSOR_RISK.md)를 따른다.

최종 센서 XGBoost v3는 합성 관측 10,000개로 학습했으며 28개 관측·파생값과 8개 유효성 표시를 사용한다.
[모델·데이터·재학습·실행 방법](docs/XGBOOST_FINAL_SENSORS.md)을 참고한다.

Jetson/Pi의 검증된 유선 DDS·DMS·음향·XGBoost·여성 TTS·카메라 HMI는
`board_runtime/src/codrive`에 있다. 기존 main의 팀별 모듈 `src/codrive`와 구조가 달라
Python import 경로를 분리했다. `scripts/run-*.sh`는 보드 런타임 경로를 자동 선택한다.
HMI는 외부 영상과 우측 아래 내부 영상, 위치 전환을 지원한다.
[main 통합·배포 안내](docs/MAIN_BOARD_MERGE.md)를 따른다.
