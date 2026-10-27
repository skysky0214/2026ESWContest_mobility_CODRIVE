# CODRIVE 사이렌·경적 음향 인식 파이프라인

## 1. 구현 범위

현재 구현은 동일한 추론 코어 앞에 두 입력을 연결한다.

```text
AI Hub WAV ─→ STM32 protocol emulator ─┐
                                      ├─→ AudioStreamDecoder
실제 microphone ─→ STM32 USART ───────┘        ↓
                                         960 ms rolling PCM
                                                ↓ 100 ms hop
                                         64×96 log-mel
                                                ↓
                                         AudioEventNet
                                                ↓
                                      EMA + enter/leave hysteresis
                                                ↓
                           AudioEvent → DDS / ROS Marker+Image → RViz
```

WAV 데모도 `encode_audio_frame()`과 `AudioStreamDecoder`를 통과하므로 마이크가 도착한 뒤
바뀌는 부분은 실제 serial read뿐이다. 원본 PCM과 spectrogram은 안전 DDS 토픽으로 보내지 않고,
RViz spectrogram은 로컬 디버그 ROS 토픽으로만 발행한다.

## 2. 확인된 AI Hub 데이터

AI Hub Shell 다운로더로 받은 데이터 디렉터리를 준비한다.

```text
<AIHUB_CITY_SOUND_ROOT>/
├── TL_1.교통소음.tar
├── TS_1.교통소음.tar
├── VL_1.교통소음.tar
└── VS_1.교통소음.tar
```

AI Hub Shell 다운로더 결과는 TAR 안에 `*.zip.part<offset>`들이 들어 있는 형식이다. 파일을
이어 붙이거나 26 GB를 다시 풀 필요가 없다. `SplitPartTarReader`가 TAR의 물리 offset을 하나의
seekable ZIP처럼 제공하고 필요한 JSON/WAV만 읽는다. 오래된 ZIP 인코딩 때문에 깨진 한글
파일명도 CP949로 자동 복원한다.

manifest 실측 통계:

| 구분 | 이벤트 수 |
|---|---:|
| siren | 2,239 |
| horn (자동차+이륜차) | 8,667 |
| background hard negatives | 17,737 |
| 합계 | 28,643 |

원본 `category_03`의 구성과 현재 모델 target 매핑은 다음과 같다.

| AI Hub 소음 | 수량 | 모델에서의 의미 |
|---|---:|---|
| 차량사이렌 | 2,239 | `siren=1` |
| 차량경적 | 3,588 | `horn=1` |
| 이륜차경적 | 5,079 | `horn=1` |
| 차량주행음 | 1,909 | background |
| 이륜차주행음 | 5,233 | background |
| 기차 | 2,366 | background |
| 지하철 | 2,617 | background |
| 비행기 | 1,952 | background |
| 헬리콥터 | 3,660 | background |

여기서 background는 별도의 출력 막대를 만들지 않고 `siren=0, horn=0`으로 학습하는
hard negative다. 따라서 화면의 `NONE`은 이 여섯 종류뿐 아니라 일반 주변음·무음처럼 두
target 모두 아니라고 판단한 상태를 뜻한다.

Training 원본 녹음 이름을 group key로 해 85% train / 15% tune으로 나눈다. AI Hub 공식
Validation 3,044개는 threshold 튜닝에 사용하지 않고 `final_test`로 보존한다.

## 3. 환경 준비

ROS Python을 함께 쓰기 위해 system site package를 보존한다.

```bash
git clone https://github.com/skskelwl/ESW2026.git
cd ESW2026
python3 -m venv --system-site-packages .venv
. .venv/bin/activate
python -m pip install -U pip
python -m pip install 'numpy>=1.24,<3' 'torch>=2.2,<3' 'onnx>=1.15,<2' \
  'onnxruntime>=1.17,<2' 'pytest>=8,<9' 'PyYAML>=6,<7'
```

현재 PC에 NVIDIA driver가 없으면 `--device cpu`로 기능 시험만 수행한다. 전체 학습은 CUDA가
활성화된 PC가 빠르며 최종 지연 측정과 TensorRT engine 생성은 실제 Jetson에서 수행한다.

## 4. manifest 생성

```bash
PYTHONPATH=src python -m codrive.sensor_input.audio.manifest \
  --dataset-root /path/to/aihub_city_sound \
  --output artifacts/datasets/audio/city_sound_manifest.jsonl
```

출력에는 압축 내부 WAV member, 원본 녹음 group, siren/horn target, 도시·장소·날씨·거리·방향
메타데이터가 들어간다. 방향 메타데이터는 평가 slice용일 뿐 단일 마이크 방향 추론 label로
사용하지 않는다.

## 5. 학습

CPU 코드 경로만 빠르게 확인:

```bash
PYTHONPATH=src python -m codrive.sensor_input.audio.train \
  --manifest artifacts/datasets/audio/city_sound_manifest.jsonl \
  --output-dir artifacts/models/audio/smoke_model \
  --device cpu --epochs 1 --batch-size 8 \
  --max-train-samples 64 --max-eval-samples 32
```

전체 CUDA 학습:

```bash
PYTHONPATH=src python -m codrive.sensor_input.audio.train \
  --manifest artifacts/datasets/audio/city_sound_manifest.jsonl \
  --output-dir artifacts/models/audio/model \
  --device cuda --epochs 25 --batch-size 64 --workers 2 \
  --export-onnx
```

학습 데이터에는 다음이 적용된다.

- 44.1 kHz WAV → 16 kHz mono
- 960 ms crop/pad
- 25 ms frame, 10 ms STFT hop, 64 mel bins, 96 time frames
- positive와 실제 교통 hard negative를 -8~18 dB SNR로 혼합
- gain, time mask, frequency mask
- 독립 sigmoid `p(siren)`, `p(horn)`와 weighted BCE
- tune set에서 클래스별 F1 threshold 결정
- 잠근 official Validation에서 최종 precision/recall/F1 산출

최종 산출물:

```text
artifacts/models/audio/model/
├── audio_event_best.pt
├── audio_event.onnx
├── audio_event.json
└── training_report.json
```

체크포인트를 잠근 전체 Validation에서 다시 평가하고 오검출 카테고리를 확인한다.

```bash
PYTHONPATH=src python -m codrive.sensor_input.audio.evaluate \
  --manifest artifacts/datasets/audio/city_sound_manifest.jsonl \
  --checkpoint artifacts/models/audio/model/audio_event_best.pt \
  --partition final_test \
  --output artifacts/models/audio/model/final_test_report.json
```

실시간 판정부는 학습 시 보정된 사이렌·경적 임계값을 각각 적용하고, EMA와 진입 2회/해제 5회
히스테리시스로 순간적인 오검출과 화면 깜빡임을 억제한다.

### 로컬 초기 모델 검증 결과

CPU에서 파이프라인 검증용으로 train 5,000개, tune 1,000개를 5 epoch 학습했다. 제출용 전체
학습 모델이 아니라 동작 가능한 초기 기준선이다. 학습에 전혀 사용하지 않은 AI Hub 공식
Validation 3,044개 전체 결과는 다음과 같다.

| 클래스 | Precision | Recall | F1 | TP / FP / FN |
|---|---:|---:|---:|---:|
| siren | 95.05% | 84.74% | 89.60% | 211 / 11 / 38 |
| horn | 95.15% | 96.08% | 95.61% | 882 / 45 / 36 |

사이렌 오검출 11개는 이륜차주행음 6, 차량경적 2, 차량주행음 2, 헬리콥터 1이다. 로컬에서
생성한 PyTorch와 ONNX 모델의 실제 사이렌 샘플 확률 최대 차이는 `5.6e-9`였다. 개발 PC ONNX
단일 추론 평균은 약 0.36 ms지만, Jetson 지연은 보드에서 별도로 측정해야 한다. 이 초기 모델과
평가 보고서는 `artifacts/models/audio/initial_model/`에 포함되어 있다. AI Hub 원본 데이터와
manifest, 새로 학습한 모델은 저장소에 포함하지 않는다.

## 6. 노트북 내장 마이크 실시간 추론

현재 Ubuntu에서 확인된 내장 마이크는 `HDA Intel PCH / ALC285 Analog`(`hw:1,0`)이고,
PulseAudio를 통한 16 kHz mono PCM16 캡처가 정상 동작한다. 먼저 Ubuntu **설정 → 소리 →
입력**에서 내장 마이크를 선택하고 입력 레벨 막대가 움직이는지 확인한다.

녹음 경로만 5초 시험하려면 다음 명령을 사용한다.

```bash
arecord -q -D pulse -f S16_LE -r 16000 -c 1 -d 5 /tmp/laptop_mic.wav
aplay /tmp/laptop_mic.wav
```

RViz를 띄운 뒤 두 번째 터미널에서 실시간 분류기를 실행한다.

```bash
cd /path/to/ESW2026
source /opt/ros/humble/setup.bash
source .venv/bin/activate

PYTHONPATH=src python -m codrive.sensor_input.audio.microphone \
  --model artifacts/models/audio/initial_model/audio_event.onnx \
  --capture-device pulse \
  --siren-only \
  --rviz
```

`pulse`가 실패하면 이 PC의 직접 장치인 `--capture-device plughw:1,0`을 사용한다. 사이렌은
처음에는 휴대전화처럼 노트북과 다른 기기에서 재생하는 편이 좋다. 스피커와 마이크를 0.5~2 m
떨어뜨리고 과도하게 큰 볼륨을 피한다. 같은 노트북에서 재생해도 되지만 echo cancellation과
스피커 진동이 입력을 바꿀 수 있다. 종료는 `Ctrl+C`다. 초기 모델은 노트북 주변음에서 horn
확률이 높아지는 사례가 확인됐으므로 사이렌 기능을 시험할 때는 `--siren-only`를 사용한다.
이 옵션은 horn 확률과 노란 막대는 계속 표시하지만 `HORN` 상태 전환만 막으며, 사이렌 검출과
Pi의 사이렌 위험 판단에는 영향을 주지 않는다. 경적까지 시험할 때만 이 옵션을 뺀다.

AI Hub에서 실제 사이렌 WAV 하나를 꺼내 같은 노트북 스피커로 시험하려면 세 번째 터미널에서
다음처럼 실행한다. 먼저 낮은 볼륨으로 시작하고 이 WAV를 외부에 재배포하지 않는다.

```bash
PYTHONPATH=src python -m codrive.sensor_input.audio.sample \
  --manifest artifacts/datasets/audio/city_sound_manifest.jsonl \
  --partition final_test --label siren --index 0 \
  --output /tmp/aihub_test_siren.wav

aplay /tmp/aihub_test_siren.wav
```

실시간 경로는 960 ms rolling window를 100 ms마다 판정한다. 로컬 초기 모델에서는 확률이 사이렌 0.89 또는
경적 0.63을 두 번 연속 넘으면 이벤트가 켜지고, 0.42 아래가 다섯 번 이어지면 꺼진다. 이 값은
checkpoint에서 읽으므로 새 모델을 학습하면 자동으로 바뀐다.

## 7. AI Hub WAV 실시간 추론과 RViz

터미널 1:

```bash
source /opt/ros/humble/setup.bash
rviz2 -d config/rviz/audio_demo.rviz
```

터미널 2:

```bash
cd /path/to/ESW2026
source /opt/ros/humble/setup.bash
. .venv/bin/activate
PYTHONPATH=src python -m codrive.sensor_input.audio.demo \
  --manifest artifacts/datasets/audio/city_sound_manifest.jsonl \
  --model artifacts/models/audio/initial_model/audio_event.onnx \
  --partition final_test --label siren --index 0 \
  --realtime --rviz
```

RViz 토픽:

| 토픽 | 타입 | 표시 |
|---|---|---|
| `/codrive/audio/markers` | `visualization_msgs/MarkerArray` | event, confidence, 두 확률 bar |
| `/codrive/audio/spectrogram` | `sensor_msgs/Image` | 64×96 log-mel |
| `/codrive/audio/probabilities` | `std_msgs/Float32MultiArray` | siren, horn, active confidence |

기본 RViz 설정은 그리드와 설정 패널을 숨기고 위에서 내려다보는 2D 대시보드로 구성한다.

- 상단 상태 배지: 초록 `LISTENING / NO ALERT`, 빨강 `SIREN DETECTED`, 노랑 `HORN DETECTED`
- 중앙 카드: 클래스별 확률, `MONITORING/VERIFYING/DETECTED`, ON threshold와 게이지
- 흰 세로선: 해당 클래스가 켜지는 threshold 위치
- 하단 카드: 현재 판단 신뢰도, 모델 추론시간, 오디오 window/update 주기
- `--siren-only`: HORN 카드는 확률을 보여 주되 `DISABLED / MONITOR ONLY`로 표시
- 실시간 파형: 모델이 보는 동일한 960 ms PCM을 min/max envelope로 표시
- 파형 상태: RMS dBFS, peak, `LOW INPUT/LEVEL OK/CLIPPING`으로 마이크 레벨 진단

화면 값의 의미:

- `AUDIO STATE: SIREN`: 차량 사이렌으로 확정된 상태
- `AUDIO STATE: HORN`: 자동차·이륜차 경적으로 확정된 상태 (`corn`이 아니라 `horn`)
- `AUDIO STATE: NONE`: 둘 다 아니거나 아직 연속 확인 횟수가 부족한 상태
- `siren`, `horn`: 최근 프레임에 EMA를 적용한 0~1 확률이며 막대 높이와 같은 값
- `ON≥...`: 해당 이벤트가 켜지는 클래스별 threshold
- `confidence`: SIREN/HORN이면 해당 확률, NONE이면 `1-max(siren, horn)`이다. `--siren-only`
  모드의 NONE은 horn을 제외하고 `1-siren`으로 계산한다.
- `inference`: CNN/ONNX 한 번의 계산 시간으로, 960 ms 수집시간과 히스테리시스 시간은 제외
- `LIVE WAVEFORM`: 가운데 선이 0이며 위·아래 진폭이 클수록 입력 소리가 크다. `CLIPPING`이
  표시되면 마이크 입력 볼륨을 낮추고, 계속 `LOW INPUT`이면 입력 장치와 볼륨을 확인한다.

왼쪽 `Log-Mel Spectrogram`의 체크박스를 켜면 모델이 실제로 보는 64×96 시간-주파수 이미지를
확인할 수 있다. 빨강은 SIREN 확률, 노랑은 HORN 확률 막대다.

`--dds`를 추가하면 domain 42의 `codrive/v1/perception/audio`에도 기존 `AudioEvent`를 발행한다.
Pi orchestrator는 이를 받아 `emergency_vehicle` → `siren_yield` 경고를 생성한다.

## 8. ONNX와 Jetson TensorRT

ONNX를 별도로 만들 때:

```bash
PYTHONPATH=src python -m codrive.sensor_input.audio.export \
  --checkpoint artifacts/models/audio/model/audio_event_best.pt \
  --output artifacts/models/audio/model/audio_event.onnx
```

Jetson에서 JetPack에 포함된 `trtexec`로 고정 shape FP16 engine을 생성한다.

```bash
/usr/src/tensorrt/bin/trtexec \
  --onnx=artifacts/models/audio/model/audio_event.onnx \
  --saveEngine=artifacts/models/audio/audio_event.engine \
  --fp16 --shapes=log_mel:1x1x64x96
```

ONNX와 TensorRT의 logits/probability 차이, p50/p95/p99 추론 시간, 메모리 사용량을 동일한
locked test feature로 비교한 후 `config/devices/jetson-cabin.yaml`의 engine 경로에 배치한다.

## 9. 합격 판단

보고서에는 최소 다음을 남긴다.

- locked final test의 siren precision, recall, F1
- horn·helicopter·train별 confusion/false positive
- 실제 차량 hard-negative의 false alarms/hour
- SNR(-8, -5, 0, 5, 10, 18 dB)별 siren recall
- window 종료→AudioEvent publish p95
- 실제 소리 onset→안내 p95

`risk/policy.py`의 `RiskPolicy.audio_siren_min_confidence` 기본값은 0.7이다. 학습 확률이 보정되지 않은
상태에서 이 값을 임의로 낮추지 않고, tune 결과와 실차 false alarms/hour를 함께 보고 조정한다.

단일 mono 마이크에서는 방향을 판단할 수 없으므로 `AudioEvent.direction="unknown"`을 유지한다.
