# 장치 간 데이터 계약

정의의 기준은 `src/codrive/models.py`의 dataclass와
`src/codrive/transport/envelope.py`의 `DdsEnvelope`다. JSON은 `to_json()`으로 직렬화하고
`from_json()`과 `decode_model()`로 검증한다. 영상·PCM 원본은 이 계약에 포함하지 않는다.

## 공통 Header

| 필드 | 의미 |
|---|---|
| source_id | 스트림 발신자. 내부 카메라는 `jetson-cabin.dms` |
| sequence | 프로세스 부팅 내 스트림별 증가 번호 |
| capture_time_ns | 캡처 시각, 동기화된 Unix ns. 현재 USB 경로는 read() 직후 소프트웨어 시각 |
| publish_time_ns | 추론이 끝나 발행할 수 있게 된 Unix ns |
| monotonic_ns | 해당 호스트 내 측정 전용. 호스트 간 직접 비교 금지 |
| frame_id | 내부 카메라는 `camera_cabin_optical` |
| boot_id | 프로세스 재시작마다 변경. sequence 초기화와 함께 사용 |
| trace_id | 처리 경로 추적 식별자 |
| schema_version | 현재 1 |

Envelope는 source_id, sequence, capture_time_ns, publish_time_ns, schema_version와
payload_json을 가진다. payload의 Header와 Envelope 값은 일치해야 한다.

## DriverState

| 필드 | 형식·범위 |
|---|---|
| header | 위 Header |
| camera_id | `cabin` |
| model_version | 현재 `dms.pt` |
| attention_score | 0~1 |
| looking_forward | bool |
| eyes_closed | bool |
| phone_detected | bool |
| face_detected | bool |
| inference_ms | 0 이상 |
| drowsiness_score | 0~1, 기본 0 |
| microsleep_ms | 0 이상의 정수, 기본 0 |
| yawning | bool, 기본 false |

이번 이식에서 마지막 세 필드를 추가했다. 기존 필드의 위치·단위는 유지한다.
누락된 추가 필드는 기본값으로 읽는다. 구버전의 엄격한 소비자는 추가 필드를 거부하므로
발행자와 소비자를 같은 배포 manifest로 갱신해야 한다. 혼합 버전 운용을 지원한다는 뜻은 아니다.

현재 학습 모델의 라벨은 awake/distracted/drowsy/head drop/phone/smoking/yawn이다.
`face_detected`는 이 모델에서 운전자 행동 검출에 의한 존재 추정이다. 독립 얼굴 검출이 아니다.
눈 감김을 직접 검출하는 클래스가 없으므로 eyes_closed=false, microsleep_ms=0을 유지한다.
이 값을 눈을 뜨고 있다는 실측 증거로 사용하지 않는다.

## 이번 통합에서 사용하는 토픽

| 키 | 실제 DDS 이름 | 용도 |
|---|---|---|
| driver_state | codrive/v1/perception/driver | Jetson 2 → Pi |
| audio_event | codrive/v1/perception/audio | Jetson 2 → Pi |
| node_health | codrive/v1/system/health | 양방향 상태 감시 |
| situation_snapshot | codrive/v1/fusion/snapshot | Pi 융합 결과 |
| risk_event | codrive/v1/risk/event | Pi 위험·감지 안내 |
| hmi_command | codrive/v1/hmi/command | 화면·음성 명령 |

NodeHealth의 header.source_id와 node_id는 동일해야 한다. DMS는 둘 다 `jetson-cabin`,
Pi HMI는 둘 다 `pi5.hmi`다. 음향 health는 둘 다 `jetson-cabin.audio`다.
카메라 미연결 Jetson 1의 통신 전용 health는 둘 다 `jetson-front`이며 DEGRADED를 발행한다.
heartbeat 연결 여부와 센서 준비 상태는 별도로 표시한다.
운전자 센서 결과의 source_id와 health의 source_id는 서로 다르다.

## AudioEvent

기존 데이터 계약을 그대로 사용한다. header.source_id는 `jetson-cabin.audio`, kind는
none/siren/horn/other, confidence는 0~1, window_ms는 960, model_version은 `audio-event-v1`,
direction은 unknown이다. mono 입력으로 방향을 추정하지 않는다. capture_time_ns는 window
마지막 샘플의 소프트웨어 추정 시각이며 publish_time_ns는 전처리·추론 후 설정한다.
음향 TTL은 1000ms다. 실제 샘플 수집 960ms가 capture_time_ns 이전에 필요하므로 이 시각부터
수신까지의 지연을 전체 음향 검출 지연으로 해석하지 않는다.

현재 프로필은 경적 발행을 비활성화했다. none은 활성화한 사이렌 조건 미검출을 뜻하며
전체 환경이 안전하다는 판정이 아니다. 확률·추론 시간은 로컬 진단 로그이며 DDS 계약 필드를
임의로 확장하지 않는다. 상세 입력과 실행은 [AUDIO_INTEGRATION.md](AUDIO_INTEGRATION.md)를 따른다.

원본 수신 데이터가 유효기간을 넘으면 snapshot에서 제거한다. 내부 카메라 TTL은 400ms,
heartbeat 누락 판정은 1.5초다. 발행자가 종료될 때 생기는 DDS InvalidSample은 생명주기 알림이며
JSON 메시지로 해석하지 않는다.

## 실행 범위와 UI

실시간 장치는 domain 42를 사용한다. 통신 시험은 43, 합성 DMS·음성 검증은 44를 사용하며
시험 도구는 42로 합성 데이터를 발행하지 못하게 막는다.

현재 CAN·INS·전방 카메라는 미연결이다. 기존 주행 위험 규칙에 필요한 입력을 생성하거나
정상값으로 대체하지 않는다. cabin-only Pi 앱에서는 얼굴/행동 존재가 확인된 모델의
휴대폰·졸음·주의 분산 감지를 INFO 안내로 제공한다. 이는 주행 중 위험 등급 판정과 구분한다.

`/api/state`는 브라우저용 투영이다. DDS 계약 자체를 대체하지 않으며, snapshot과 HmiCommand를
그대로 포함하고 수신 건수·지연·TTS 결과를 덧붙인다. 웹 연결이 끊기면 브라우저가 이전 값을
계속 정상 상태로 표시하지 않는다.
