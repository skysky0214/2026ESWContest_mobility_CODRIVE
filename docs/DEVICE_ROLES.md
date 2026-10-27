# 3개 보드 역할

## `jetson-front`

- 전방 GMSL/USB 카메라 캡처
- YOLO 객체와 신호등 상태
- geometric/UFLD 차선 인식
- `ExternalPerception` 및 heartbeat 발행
- 설정: `config/devices/jetson-front.yaml`

## `jetson-cabin`

- 내부 카메라 DMS(졸음, 휴대폰, 전방 주시)
- STM32 PCM 입력과 사이렌·경적 분류
- 필요 시 rear/side 외부 카메라
- `DriverState`, `AudioEvent`, 선택 `ExternalPerception`, heartbeat 발행
- 설정: `config/devices/jetson-cabin.yaml`

## `pi5`

- Panda CAN-FD 수신과 INS parsing
- DDS 입력의 timestamp 정렬 및 `SituationSnapshot`
- TTC, RiskEngine, 우선순위, FSM/hysteresis
- 로컬 template, 선택적 Qwen/OpenAI, HMI/TTS
- watchdog, 이벤트 저장, 주행 리포트
- 설정: `config/devices/pi5.yaml`

장비를 바꾸더라도 알고리즘 파일을 복사하지 않는다. 예를 들어 rear 카메라를
`jetson-cabin`에 추가할 때는 `sensor_input/external`을 재사용하고 cabin 설정에 카메라와
`source_id`만 추가한다.
