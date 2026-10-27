# 코드 구조와 장비 경계

코드는 **기능별로 한 번만 저장**하고, 세 보드의 차이는 `apps/` 실행 조합과
`config/devices/` 설정으로 표현한다. Jetson마다 같은 파일을 복사하지 않는다.

## 장비별 실행 소유권

| 노드 | 실행 코드 | 설정 | 담당 기능 |
|---|---|---|---|
| `jetson-front` | `apps/jetson_front/` | `jetson-front.yaml` | 전방 카메라, 객체, 신호등, 차선 |
| `jetson-cabin` | `apps/jetson_cabin/` | `jetson-cabin.yaml` | 내부 DMS, 음향, 선택 rear/side 카메라 |
| `pi5` | `apps/raspberry_pi/` | `pi5.yaml` | CAN/INS, Snapshot, Risk/FSM, 안내, HMI/TTS |

두 Jetson이 함께 사용하는 발행 검증은 `apps/jetson_publisher.py`, 모든 노드가 공유하는
메시지와 토픽은 `contracts/`에 둔다. 실제 CycloneDDS 구현은 `transport/`에만 둔다.

## 기능별 패키지

| 패키지 | 책임 |
|---|---|
| `contracts` | 도메인 모델, DDS envelope, 버전 고정 토픽 계약 |
| `hardware` | camera/CAN/INS/audio 장치 접근과 원시 parser |
| `sensor_input` | 외부 카메라, 운전자, 음향, 차량, 환경 입력 정규화 |
| `situation` | TTL, timestamp 정렬, `SituationSnapshot` 생성 |
| `risk` | TTC, 점수, 우선순위, FSM/hysteresis |
| `guidance` | 확정된 위험의 로컬 문구 및 선택적 LLM 문장화 |
| `hmi` | Web/PyQt 화면, VehicleState HTTP bridge와 TTS 출력 adapter |
| `monitoring` | heartbeat, watchdog, latency/health |
| `reporting` | 이벤트 저장, 안전 점수, 주행 리포트 |
| `transport` | CycloneDDS와 테스트용 in-memory bus |
| `simulation` | 하드웨어 없는 시나리오 재생 |

## 실행 흐름

```text
jetson-front                       jetson-cabin
external perception               DMS / audio
        └──────── contracts + CycloneDDS ────────┐
                                                  ▼
pi5 vehicle input → situation → risk → guidance → HMI/TTS
                           └────────→ HMI vehicle HTTP bridge
                                  └──────────────→ reporting
```

- LLM은 `RiskEvent`의 의미나 우선순위를 변경하지 않는다.
- CRITICAL 안내는 항상 로컬 template을 사용한다.
- `InMemoryBus`는 테스트·시뮬레이션에서만 사용한다. 실장 노드는 `CycloneDdsBus`를 사용한다.
- 카메라 프레임과 PCM 원본은 DDS 안전 토픽으로 보내지 않는다.

## 주요 경로

```text
src/codrive/
├── apps/{jetson_front,jetson_cabin,raspberry_pi}
├── contracts/{models,envelope,topics}.py
├── hardware/{camera,can,ins,audio}/
├── sensor_input/{external,driver,audio,vehicle,environment}/
├── situation/fusion.py
├── risk/{engine,policy,fsm}.py
├── guidance/{policy,llm}/
├── hmi/{web,qt,tts}/
├── monitoring/
├── reporting/
├── transport/
└── simulation/
```

실행·배포 스크립트는 `deploy/`, 모델 변환과 benchmark는 `tools/`, 장비별 모델·녹화·로그는
Git에서 제외되는 `artifacts/`에 둔다. 테스트 디렉터리는 가능한 한 `src/codrive` 구조를 그대로
따른다.
