# 발표자료 요구사항 추적표

검토 대상은 `/home/khj/Downloads/ESW 컨설팅 발표자료.pdf` 18쪽 전체다. 이 PDF는 공식
대회 규정집이 아니라 팀 컨설팅 발표자료이므로 참가 자격, 제출 형식, 평가 배점과 공식 마감은
별도 요강에서 확인해야 한다.

| 자료 요구 | 페이지 | 프레임워크 반영 |
|---|---:|---|
| 인지→융합→안내→HMI 200 ms | 4 | 로컬 CRITICAL 경로와 latency budget |
| Jetson 인지, Pi DDS 수신·위험·HMI | 5 | 2 Jetson 고정 분할 + Pi orchestrator |
| 객체·차선·신호등·운전자·음향 | 8, 13, 14 | typed perception models와 worker config |
| CAN 속도/조향/브레이크/등화/와이퍼 | 8, 10 | receive-only Panda adapter, DBC placeholder |
| BESTPOSA/HEADING2A/GPGGA→ENU pose | 12 | NovAtel parser와 local ENU assembler |
| SituationSnapshot, Risk FSM/TTC | 8, 9 | `sensor_input/fusion.py`, `risk/engine.py`, `risk/fsm.py` |
| CRITICAL은 LLM 우회 | 9, 16 | `hmi/guidance.py`의 즉시 템플릿 |
| 복합 보행자 TTC 1.2 s + 차선 이탈 | 10 | simulator와 우선순위 테스트 |
| 급가속 CAN/INS 교차 검증 | 10 | `unintended_acceleration` 규칙 |
| 주의 분산·졸음 HMI 3~4단어 | 10 | 짧은 display template과 DMS 졸음 규칙 테스트 |
| 센서/API/process fail-safe | 16 | TTL, health topic, 강등 표 |

## 아직 plug-in/후속 구현인 항목

| 자료 요구 | 현재 경계 | 완료 조건 |
|---|---|---|
| YOLO/차선/DMS/음향 실제 추론 | model/result와 Jetson publisher | 확정 모델의 TensorRT backend 및 실측 정확도/FPS |
| EV6 CAN signal 해석 | receive-only frame + `VehicleStateDecoder` protocol | 정확한 fingerprint/DBC와 기록 재생 검증 |
| 실제 급가속 CAN/INS 교차검증 | Risk rule과 `longitudinal_accel_mps2` field | INS 모델에 맞는 motion/IMU log parser 연결 |
| 날씨·야간 가중치 | `EnvironmentState`, DDS topic, snapshot | API/cache 및 악천후 기록으로 임계값 검증 |
| 주행 종료 안전 점수·리포트 | `RiskEvent` 출력 | 영속 event ledger, 점수식, HMI report 화면 |
| 화면 GUI와 실제 TTS | VehicleState Web HMI 연결, `HmiCommand` 출력 | 위험 명령 renderer와 Pi 오디오 TTS 연결 |
| 3-board 상시 실행·watchdog | node runtime, health monitor, device YAML | Jetson/Panda/INS/HMI entrypoint와 systemd unit |
| 200 ms 종단 검증 | 구간별 latency budget, Pi-core simulator | capture부터 실제 display/TTS까지 trace p99 계측 |
| 공식 대회 제출·평가 대응 | 이 PDF에는 정보 없음 | 별도 공식 요강 확보 후 traceability 보완 |

## 자료 내 충돌을 해소한 정책

- 200 ms 전체 목표와 1초 미만 cloud LLM 목표는 하나의 SLA가 될 수 없다. 로컬 CRITICAL은
  200 ms, 비긴급 설명은 최대 1초의 별도 경로로 정의한다.
- 음향 3초 버퍼는 3초마다 결과를 내는 batch가 아니라 rolling history로 구현한다.
- 자료의 GPT-4o mini/Gemini 표기가 서로 다르므로 provider는 나중에 교체 가능한 선택 기능으로 둔다.
- 발표자료의 Jetson AGX/NX 표기는 실제 장비인 Orin Nano 2대로 교정한다.
- 주석이 든 예시 JSON 대신 검증 가능한 model + schema version + DDS envelope를 사용한다.
- 후속 설계가 없는 LiDAR는 현재 범위에서 제외한다.
- “차량 개조 없음”은 “영구 ECU 개조와 차량 제어 없음, 외장 센서와 CAN 수신 장치 설치”로 표현한다.
