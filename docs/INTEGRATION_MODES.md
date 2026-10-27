# 세 모드 실행과 공유 기준

## 빠른 전환

저장소 루트에서 실행한다. 명령은 Jetson 1과 Pi의 user systemd override를 전환한다. 내부카메라·마이크와 노트북 CARLA 프로그램은 그대로 둔다.

|명령|Jetson 1 입력|Pi 차량 입력 / 환경|거리·TTC|
|---|---|---|---|
|`scripts/select-integration-mode.sh carla`|CARLA RGB HTTP relay|STM32 ECU DDS / 시나리오 임의 날씨·주야간|가상 카메라 60° pinhole와 Audi A2 geometry|
|`scripts/select-integration-mode.sh carla-screen`|실물 GMSL 외부캠, 렌즈 보정|STM32 ECU DDS / 시나리오 임의 날씨·주야간|기본 비활성. 화면 재등록 필요|
|`scripts/select-integration-mode.sh real-vehicle`|실물 GMSL 외부캠, 렌즈 보정|EQ900 CANable SLCAN 고정 OBD 조회 / 실제 API|실차 장착 보정 전 비활성|

`--dry-run`은 보드에 접속하지 않고 선택할 프로필만 출력한다. 현재 실행 모드를 바꾸지 않는 확인용이다.

```bash
scripts/select-integration-mode.sh carla-screen --dry-run
```

잘못된 모드/옵션은 거절한다. 실제 전환은 Pi 서비스를 재시작하므로 정차 후 수행한다. 실차 모드에서 CANable이 없으면 장치 대기, 응답이 없거나 속도 신호가 오래되면 CAN 연결 대기 상태가 된다. 0 표시를 측정된 실제 차속으로 오해하지 않는다.

## 모니터 촬영 보정

렌즈 보정과 모니터→가상 지면 보정은 서로 다르다. 카메라·모니터를 움직이면 과거 `physical-screen-ground-20261027.json`을 그대로 쓰면 안 된다.

1. 외부캠과 모니터를 고정하고 `carla-screen`으로 영상 입력을 확인한다.
2. [화면 보정 안내](PHYSICAL_SCREEN_CALIBRATION.md)와 `tools/calibrate_screen_recapture.py`로 현재 배치를 재등록한다. 가상 카메라는 1920×1080, 수평 FOV 60°, mount 1.8/0/1.6m 기준이다.
3. `config/camera/physical-screen-ground-20261027.json` 및 화면 인식 ROI를 현재 배치로 갱신하고 Jetson/Pi에 배포한다. `physical-screen-driving-corridor.json`의 차량 치수도 실제 CARLA hero와 맞춘다.
4. 보정 후에만 아래를 선택한다. 이 옵션은 배치의 유효성을 자동 검증하지 않는다.

```bash
scripts/select-integration-mode.sh carla-screen --screen-calibrated
```

옛 보정 파일은 참고용으로 보존했다. 현재 차량으로 옮긴 카메라에 유효하다고 주장하지 않는다. 실차에서는 모니터 보정을 사용하지 않는다.

## 접속 정보와 선행 설치

기본: Pi `rp@192.168.50.10`, Jetson 1 `one@192.168.50.21`, Jetson 2 `192.168.50.22`, 노트북 `192.168.50.30`; DDS domain 42. 장치들은 같은 유선 스위치에 연결한다. SSH 키 또는 직접 비밀번호 인증을 사용하며 비밀번호는 저장소에 저장하지 않는다.

```bash
export CODRIVE_PI_HOST=rp@192.168.50.10
export CODRIVE_FRONT_HOST=one@192.168.50.21
export CODRIVE_PI_REPO=/home/rp/ESW2026-can-v3
export CODRIVE_FRONT_REPO=/home/one/ws/ESW2026-main
# 선택: 이미 생성한 SSH 연결 재사용. 특정 /tmp 소켓에 의존하지 않는다.
# export CODRIVE_FRONT_SSH_CONTROL=/path/to/master-socket
```

필수 선행 사항:

- Pi: `codrive-pi`, 시뮬레이터용 `codrive-can-ecu` user 서비스와 DDS/환경/TTS 설정, `board_runtime` 코드·정식 v3 위험 모델·F1 음성 캐시. `board_runtime`의 `can`, `edge`, `risk-ml`, `dds` 선택 의존성을 설치한다. EQ900은 python-can 4.5~4.x와 pyserial 3.x가 필요하다.
- Jetson 1: `codrive-front-camera` user 서비스, GStreamer 지원 OpenCV, TensorRT/PyTorch/YOLO/UFLD 모델, 측정된 렌즈 보정 파일. `deploy/systemd-user`의 Python/모델 경로는 기존 연구실 배포 경로다. 다른 설치는 해당 프로필도 수정한다.
- CARLA: 0.9.16 서버, RGB front screen/relay, ROS bridge, G29 제어·CAN sender·저장 시나리오 실행. 전환 명령은 CARLA 서버를 새로 설치하거나 시작하지 않는다. 현재 지도 Town10HD_Opt, hero Audi A2, FOV60.
- 실차: CANable 2 SLCAN 16d0:117e, 500kbps/115200 baud. `deploy/99-codrive-canable.rules`를 해당 Pi 사용자에 맞춰 설치하고 udev를 재적용한다. [EQ900 안내](REAL_VEHICLE_EQ900.md)의 배선·정차 확인 범위와 송신 제한을 따른다.

전환 스크립트는 선택한 프로필을 로컬에서 보드로 복사하지만 애플리케이션 소스·모델·캘리브레이션은 자동 배포하지 않는다. 새 소스를 보드 checkout에 먼저 배포한다. 최고 우선순위 override는 `99-zzzz-integration-mode.conf`다. 기존 다른 override를 삭제하지 않는다.

## 모델·기록 포함 범위

Git에는 소스·검증 테스트·실행 프로필·시나리오·보정 JSON·현재 v3 모델·DMS/음향 기준 모델·여성 음성 캐시와 휴대 가능한 신호등 보조 `.pt`가 포함된다. TensorRT `.engine`/ONNX 내보내기, 가상환경, CARLA 배포본, 원본 데이터·기록, 보정용 사진과 캡처, 개인 SSH/환경 파일은 제외한다. TensorRT 엔진은 배포 Jetson에 맞춰 생성해야 한다.

`board_runtime`의 팀 FSM/TTC는 `config/risk.yaml`을 사용한다. 보존된 `src`의 구형 Pi 규칙 예제는 별도 `config/risk-legacy.yaml`을 사용해 서로 다른 스키마가 섞이지 않는다. 팀 FSM/TTC가 실제 HMI/TTS를 선택한다. XGBoost는 별도 위험 등급/설명을 표시하며 모델을 바꾸거나 텍스트를 표시했다고 안전 규칙을 대체하지 않는다. 실차 풀페달 경고는 90%·300ms 입력 조건이며 실제 가속도·비의도 급발진을 측정한 판정이 아니다.

## 확인

```bash
scripts/select-integration-mode.sh carla --dry-run
scripts/select-integration-mode.sh carla-screen --dry-run
scripts/select-integration-mode.sh real-vehicle --dry-run
PYTHONPATH=src PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest tests/tools/test_integration_modes.py -q
python3 scripts/check-deployment.py
```

전방 영상 API `/health`와 Pi `/api/hmi`의 `test_mode`, `vehicle_input`, `snapshot`, `driving_corridor`를 확인한다. HMI 전체화면은 Pi 브라우저 표시 서비스로 별도 운영하며 기존 화면 설정을 유지한다.
