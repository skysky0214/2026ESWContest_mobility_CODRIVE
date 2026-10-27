# 음향 입력·음성 출력 통합

2026-10-19 기준. 외부 카메라의 정격 전원은 아직 없어 해당 입력은 제외한다.

## 어떤 마이크를 쓰는가

- 원래 구조는 차량 외부 마이크 → STM32 → USART → Jetson 2다.
  근거는 [ARCHITECTURE.md](ARCHITECTURE.md)의 STM32 입력과
  발표자료 `/home/khj/Downloads/ESW 컨설팅 발표자료.pdf`의 차량 외부 마이크 센서 설명이다.
- 확인한 저장소·발표자료에는 최종 마이크 부품명이나 구체적인 설치 위치가 확정돼 있지 않다.
  C922를 최종 외부 마이크로 선정했다는 기록도 발견하지 못했다.
- 현재 개발 입력은 Jetson 2에 USB로 연결된 Logitech C922 Pro Stream Webcam의 내장 마이크다.
  ALSA 장치명은 `plughw:CARD=Webcam,DEV=0`이며 카메라 영상과 마이크 음성을 동시에 사용한다.
  [제조사 설명](https://www.logitech.com/en-us/shop/p/c922-pro-stream-webcam)에 따르면
  C922는 두 개의 무지향성 마이크를 가진다. 현재 코드는 16kHz mono로 변환해 사용한다.
- STM32용 수신 코드와 CRC32 PCM 프레임 계약은 있지만, 실제 마이크 보드·UART·펌웨어 연결은
  확인되지 않았다. 통합 프로필의 STM32 device는 null이며 임의의 ttyTHS0를 사용하지 않는다.
- 단일 mono 입력으로 음원의 접근 방향이나 외부 설치 성능을 검증했다고 주장하지 않는다.

## 현재 개발·배치

Jetson 2의 기존 미커밋 음향 작업을 보존한 채 통합 폴더로 이식했다. 모델 원출처는 yyj
`490e8194805aee3b50a5e69d1b6ec52812798850`이며 SHA-256과 전처리는
`config/devices/audio-model.json`에 있다. 기존 DMS 및 Pi의 위험 엔진 구조를 유지한다.

```text
C922 USB Audio → ALSA PCM16 mono 16kHz
  → 960ms rolling window / 100ms hop → log-mel CNN (CUDA)
  → EMA·확인 횟수·히스테리시스 → AudioEvent → DDS domain 42
  → Pi 융합 → HMI 시험 표시 (음향 자동 경고는 오탐 검증 전 보류)
```

- 실행: `board_runtime/src/codrive/apps/audio.py`, 전처리·모델: `board_runtime/src/codrive/sensor_input/audio/`
- 입력: `board_runtime/src/codrive/hardware/audio_input.py`
- 설정: `config/integration/jetson-cabin.yaml`
- 모델: `artifacts/audio/initial_model/audio_event_best.pt` (`audio-event-v1`)
- 학습 임계값 siren 0.89 / horn 0.63을 유지한다. 경적은 기존 주변음 오탐 기록 때문에
  이번 프로필에서는 발행하지 않고 확률만 로그에 남긴다.
- 원본 PCM을 DDS로 보내거나 파일로 저장하지 않는다.
- 마이크 오류·모델 미로딩은 정상 NONE으로 대신하지 않는다. 입력이 끊기면 AudioEvent TTL
  1초 후 Pi의 음향 상태가 수신 없음으로 바뀐다. heartbeat는 1.5초 후 누락으로 처리한다.
- 실시간 마이크는 `codrive-audio.service`, 카메라는 기존 `codrive-dms.service`가 실행한다.
  두 서비스는 같은 C922의 서로 다른 USB 인터페이스를 사용한다.

Jetson 2 통합 폴더에서:

```bash
systemctl --user status codrive-audio codrive-dms
tail -f logs/audio.log
systemctl --user stop codrive-audio
# 서비스 정지 후에만 같은 마이크를 수동으로 연다.
bash scripts/run-audio.sh --dry-run --print-json --diagnostics --max-windows 10
systemctl --user start codrive-audio
```

UI는 `http://192.168.99.21:8080`에서 운전자와 음향 수신 상태를 각각 표시한다.
이미 열려 있던 페이지는 새로고침하면 음향 카드가 나타난다.

## TTS는 별도 출력 경로

현재 Pi의 `espeak-ng -v ko` 합성 결과를 `paplay`로 HDMI 출력한다. 어눌한 발음은 이
합성기의 음질 문제이며 C922 입력을 교체해도 해결되지 않는다. 최종 음성 제공자는 정해지지 않았다.
고정 경고 문구를 품질 좋은 한국어 음성으로 미리 합성해 로컬 WAV로 재생하는 방식으로 교체할 수 있다.
문구·위험 우선순위·만료·반복 억제 정책은 유지한다. 이번 작업에서는 TTS 엔진을 교체하지 않았다.

시험실에서는 Pi 스피커 소리가 C922에 다시 들어갈 수 있다. 사이렌 미재생 상태의 오탐을
확인했으므로 현재 Pi 기본값은 음향 시험 표시 모드다. AudioEvent와 화면 후보 표시는 유지하고
음향으로 인한 위험·TTS 발행만 보류한다. 기존 운전자 INFO·음성 안내는 계속 동작한다.
`--audio-alerts`는 오탐 검증 후 경고를 활성화하거나 분리한 합성 시험에서 사용할 명시적 옵션이다.

## 두 Jetson의 연결 단계

두 Jetson을 통합 DDS망에 넣는 일은 2번 통신 단계에 속한다. 현재 Jetson 2 ↔ Pi 경로는
동작하며 Jetson 1의 통신 전용 연결은 [별도 안내](JETSON1_COMMUNICATION.md)를 따른다.
설계상 두 Jetson이 각자의 결과를 Pi로
발행한다. 별도 USB 케이블이나 보드 간 영상 전송이 필수인 구조는 아니다.

Jetson 1의 DDS 설치·peer 설정·Pi 기준 시각 동기화·heartbeat 검증은 외부 카메라 전원 없이도
진행할 수 있다. 실제 ExternalPerception 발행·카메라 검증은 전원 준비 후 수행한다.

## 검증 기록과 한계

- 노트북 회귀 시험 84개 통과, TensorRT 관련 1개 건너뜀.
- Jetson 2 음향 시험 15개 통과. 실제 학습 checkpoint CUDA, TorchScript CPU/CUDA,
  별도 합성 TensorRT 엔진 실행 경로를 포함한다. 배치 backend는 checkpoint CUDA다.
- C922 실제 PCM으로 10개 window 실행 완료, 원본 음성 파일 저장 없음.
- 실제 DMS·음향 병행 수신 표본 120회 모두 두 입력이 존재했고, 종료 시 수신 거부 0개였다.
- 분리한 domain 44 / HTTP 8081에서 `--audio-alerts`를 명시한 합성 AudioEvent → WARNING → HMI → HDMI TTS 재생 완료.
  발행 중지 후 audio TTL 만료도 확인했다. 실시간 domain 42에 합성 값을 주입하지 않았다.
- 실제 입력의 사이렌 판정 시 사용자가 사이렌을 재생하지 않았다고 확인했다. 오탐 사례로 기록한다.
  원인은 미확정이며 주변음·출력 음성 재입력 등을 확인해야 한다. 임계값을 임의 변경하지 않았다.
- 960ms 수집과 확인 횟수가 필요하므로 추론 시간만으로 종단 200ms 달성을 주장하지 않는다.
- 차량 외부 설치·실차 잡음·false alarms/hour·경적 활성화·장시간 부하는 후속 검증이 필요하다.

원시 수신 표본은 노트북 백업 폴더의 `audio-live-30s.json`, `audio-isolated-hmi.json`에 있다.
여기에는 정형 상태만 저장하며 PCM은 포함하지 않는다.
