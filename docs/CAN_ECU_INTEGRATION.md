# STM32 CAN → 최신 Pi 위험 판단

기존 CARLA/CAN 송신 환경과 ECU 펌웨어에서 출력하는 `ECU,...` UART 보고를 사용한다.
노트북 CARLA 송신기는 기존 can-integration 실행 환경을 사용한다.

Pi에서 이 저장소와 DDS 의존성을 설치한 Python에 pyserial을 설치한다.

```bash
python3 -m pip install pyserial
bash scripts/run-can-ecu.sh --port /dev/ttyACM0
```

다른 터미널에서 Pi 앱을 실행한다. 기존 Pi 서비스와 중복 실행하지 않는다.

```bash
bash scripts/run-pi.sh --vehicle-source can-ecu --xgboost-model artifacts/models/risk/final-sensors-v3
```

두 프로세스는 기본 DDS domain 42 및 같은 Pi DDS 설정을 사용한다.
can-ecu 모드는 Panda worker를 실행하지 않고 pi5.can_ecu만 차량 소스로 허용한다.
차량 TTL 기본값은 CAN ECU 모드 500ms, Panda 모드 120ms다.
규칙과 v3 모델 입력 유효기간에도 같은 차량 TTL을 적용한다.

속도는 CAN 0.01km/h를 m/s로 변환한다. 브레이크는 boolean이다.
ECU uptime 기준 신호 나이를 Pi 수신 시각에서 빼 신호별 관측 시각을 기록한다.
미수신·500ms 초과·bus-off는 can_valid=false로 발행하고 fusion에서 제외한다.
UART가 끊기면 새 값을 만들지 않으며 기존 값은 TTL로 만료한다.
logs/can-ecu-status.json에서 보고 수, 거부 수, 신호 나이와 실제 변환값을 확인한다.

가속 페달은 현재 CAN 규약에 없어 신호 시각을 생성하지 않는다.
v3에는 pedal_valid=false, accelerator_pct=null로 전달한다.
조향은 수신하지만 v3 위험 특징에는 사용하지 않는다.
현재 200ms ECU 보고는 기본 motion_max_gap_ms=150보다 길어 속도 변화율 기반 판단은
연속 이력이 부족할 수 있다. 급가속 판단까지 검증하려면 ECU 보고 주기 단축과
관측 주기 검증을 별도로 수행한다. 기존 모델 입력 구조와 가중치는 변경하지 않는다.

이 변경은 로컬 코드 통합이며 보드 서비스 설치나 하드웨어 연결 검증을 포함하지 않는다.

## Pi 실제 배포 확인 · 2026-10-26

실행 폴더: /home/rp/ESW2026-can-v3. 기존 ESW2026-main은 보존했다.
두 user 서비스의 99-can-v3.conf drop-in으로 실행 경로를 지정했다.
기존 음향 경고 비활성 설정을 --no-audio-alerts로 유지했다.
기존 광진구 좌표 설정을 새 환경 설정에 반영했다.
ECU v2는 실제 가속 페달(accel_raw 0.01%)도 수신한다.
수신기는 기존 3-ID 형식과 v2 4-ID 형식을 지원하며 v2는 페달 수신도 검증한다.
v2는 accelerator_pct 신호 시각도 전달하므로 모델 pedal_valid가 true가 된다.
실물 보고 주기는 약 50ms로, 이전 보관 문서의 200ms와 다르다.
정지 상태 CAN valid, v3 LOW, 속도·페달 변화율 0 및 DDS 거부 0을 확인했다.
급가속 주행 시험은 아직 수행하지 않았다.
기존 TTS 캐시에 최신 규칙 문구 일부가 없어 cache_ready=false이며 음성은 별도 확인이 필요하다.

복구:
```bash
bash /home/rp/codrive-backups/can-v3-20261026-205042/rollback.sh
```

## G29 방향지시등 · 2026-10-26

최신 워크스페이스에서 `bash scripts/run-g29-indicators.sh`로 별도 실행한다.
config/g29-indicators.json: 왼쪽 버튼 5, 오른쪽 버튼 4. 한 번 누르면 켜짐,
같은 버튼 재입력 시 꺼짐, 반대 버튼은 방향 전환. 기존 후진 버튼 0은 유지한다.
이 프로세스는 조향·페달을 제어하지 않고 CARLA hero 차량의 방향지시등만 제어한다.
현재 ECU에 깜빡이 CAN ID가 없어 `turn_indicator_state` DDS 토픽으로 G29 의도를 별도 전송한다.
Pi는 승인한 laptop.g29.indicators 소스만 받고, 실제 CAN 차량 상태가 있을 때만 합성한다.
CAN 값·수신 시각은 보존하며 방향지시등 미수신/500ms 만료 시 OFF로 복귀한다.
HMI TURN SIGNAL에 표시하고 기존 팀 차선 이탈 조건에 전달한다. TTC 충돌 판단은 유지한다.
노트북 상태 파일: logs/g29-indicators-status.json. 실제 CAN 펌웨어 경로 통합은 별도 작업이다.
