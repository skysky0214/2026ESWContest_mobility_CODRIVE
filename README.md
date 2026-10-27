# CODRIVE
### 멀티모달 상황 해석 기반 운전 보조 시스템

**제24회 임베디드SW경진대회 | 자동차/모빌리티 부문**

## 프로젝트 소개

CODRIVE는 차량 외부 환경, 운전자 상태 및 차량 주행 정보를 통합하여 복합 위험을 판단하고, 운전자에게 최우선 행동지침을 제공하는 지능형 운전 보조 시스템입니다.

Jetson, STM32, Raspberry Pi 기반의 분산형 임베디드 시스템으로 구현하였으며, CARLA 시뮬레이션과 실제 차량 환경에서 기능을 검증하였습니다.

## 주요 기능

- **외부 환경 인식:** 차량, 보행자, 차선, 신호등 및 사이렌 감지
- **운전자 상태 인식:** 졸음, 전방 미주시 및 주의 분산 감지
- **차량 데이터 처리:** STM32 FDCAN 기반 속도, 조향각, 브레이크 및 가속 페달 정보 처리
- **복합 위험 판단:** Situation Snapshot, TTC, 차량 동역학, Rule-based Risk Engine 및 XGBoost 활용
- **위험 경고:** 4단계 Risk FSM 기반 경고 우선순위 결정 및 HMI, 음성 안내
- **통신 신뢰성:** ROS 2 DDS, CAN, TTL 및 heartbeat 기반 데이터 상태 관리

## 하드웨어 구성

| 장치 | 역할 |
|---|---|
| NVIDIA Jetson 1 | 외부 주행 환경 및 음향 인식 |
| NVIDIA Jetson 2 | 운전자 상태 인식 |
| STM32 NUCLEO-H723ZG | FDCAN 기반 차량 데이터 수신 및 처리 |
| Raspberry Pi 5 | 데이터 융합, 위험 판단 및 HMI |
| Logitech G29 | 가상 주행 조작 |
| CANable V2.0 | CAN 통신 인터페이스 |

## 개발 환경

- **OS:** Ubuntu 24.04, Windows
- **Language:** Python, C
- **AI:** YOLO, PyTorch, OpenCV, XGBoost
- **Communication:** ROS 2 Jazzy, DDS, Classic CAN 500 kbps
- **Simulation:** CARLA

## 핵심 특징

1. **분산형 임베디드 아키텍처:** 이기종 보드에 인식, 차량 통신, 위험 판단 기능을 분산 구현
2. **실제 CAN 하드웨어 연동:** CARLA 및 G29의 주행 정보를 STM32 FDCAN과 연계
3. **복합 위험 분석:** TTC, 차량 동역학 및 운전자 상태를 통합한 위험 판단
4. **안전 중심 경고:** 긴급 경고와 일반 안내를 분리하여 최우선 행동지침 제공

## 실행 방법

```bash
git clone https://github.com/skysky0214/2026ESWContest_mobility_CODRIVE.git
cd 2026ESWContest_mobility_CODRIVE
```

보드별 소스·의존성·모델·서비스 설치와 SSH 연결을 준비한 뒤, 아래 중 사용할 모드 하나를 실행합니다. CARLA 모드는 서버와 주행 프로그램도 먼저 실행해야 합니다.

```bash
bash scripts/select-integration-mode.sh carla         # CARLA 가상 카메라 입력
bash scripts/select-integration-mode.sh carla-screen  # 실물 카메라로 CARLA 화면 촬영
bash scripts/select-integration-mode.sh real-vehicle  # 실차 카메라 + EQ900/CANable
```

보드 주소·설치 경로 설정 및 카메라 보정은 [실행 안내](docs/INTEGRATION_MODES.md)를 참고하세요. 모드 전환은 정차 상태에서 수행합니다.

## 개발 및 검증

CARLA 기반 가상 주행 환경과 실제 차량 환경에서 기능을 검증하였으며, STM32 CAN 통신과 이기종 임베디드 장치 간 데이터 연동을 확인하였습니다.

---

**CODRIVE Team | 제24회 임베디드SW경진대회**
