> 과거 B 모델 통합 기록이다. 현재 공유 실행은 `final-sensors-v3`를 사용하며 B 가중치는 포함하지 않는다. [현재 실행 안내](INTEGRATION_MODES.md)를 따른다.

# XGBoost B · Pi HMI 연결

> 이 문서는 기존 15개 입력 B/v2의 연결 이력이다. 2026-10-26 추가한 36개 입력·10,000개
> 합성 데이터 모델은 [최종 센서 XGBoost v3](XGBOOST_FINAL_SENSORS.md)를 따른다.

2026-10-21 랩탑 구현이다. 기반은 `origin/integration/wired-hmi-20261021`의
`f3ae33a`이며 2026-10-21 21:01(KST) Pi에 배포했다. CARLA/CAN 연결과 외부 카메라는 범위 밖이다.

## 가져온 모델과 코드

`origin/XGBoost`의 `8545fbb`에서 실험 B의 `model.ubj`, `preprocess.json`,
`metadata.json`을 그대로 `artifacts/models/risk/B/`에 가져왔다.
모델 SHA256은 `85cac5e3da74a0fe7bfa839d150ef557aa3b5b54ecc84cbe41bebb5e31e6411f`다.
학습이나 가중치 변경은 하지 않았다. 합성 데이터로 학습한 LOW/MEDIUM/HIGH 분류 모델이며
실제 사고 확률이나 실도로 성능을 검증한 모델이 아니다.

통합 브랜치에는 `codrive/risk.py`가 있으므로 `risk/ml`을 통째로 합치면 구조와 규칙 계약이
충돌한다. `codrive/risk_ml`에 B의 전처리·문구·추론만 분리했다. B는 규칙 점수를 Feature로
사용하지 않으므로 원본 A/C 비교 계산을 실행하지 않는다. C 모델은 명시적으로 거부한다.
이 모듈은 학습 CLI가 아니다. 학습·평가 코드는 원본 XGBoost 브랜치에 남겨 둔다.

모델은 시작 시 한 번 로드하며 체크섬·24개 인코딩 컬럼·순서·클래스·입력 계약을 확인한다.
ML Feature 계약 v2와 DDS Header schema_version=1은 독립적이다. DDS 계약은 바꾸지 않았다.

## 입력 변환

`risk_ml/adapter.py`는 `SituationSnapshot`에서 항상 15개 키를 만든다. 기본값은 모두 null이다.
Snapshot을 거친 값도 capture 시각으로 TTL과 미래 시각을 다시 확인한다.

| 모델 입력 | 현재 연결과 결측 처리 |
|---|---|
| traffic_light / pedestrian_count / vehicle_count | 외부 카메라 보류로 null. NONE/0을 생성하지 않음 |
| vehicle_speed / accelerator_pct / brake_pressed | 현재는 null. 이후 검증된 신호별로만 허용하며 속도는 m/s × 3.6 |
| driver_drowsy | 신선한 DMS·행동 검출이 있을 때 drowsiness_score ≥ 0.7 |
| phone_usage | 같은 조건에서 phone_detected |
| eyes_on_road | 같은 조건에서 looking_forward. 실제 시선 계측이 아닌 모델 추정 |
| distraction_score | 같은 조건에서 1 − attention_score |
| siren_detected / horn_detected / audio_direction | 음향 오탐 감사 보류로 모두 null. false로 대체하지 않음 |
| weather / is_night | 신선하고 api_valid인 EnvironmentState만 반영. UNKNOWN 날씨는 null |

DMS 400ms, vehicle 120ms, environment 300,000ms 기준을 사용한다. 현재 환경 수집기는
연결하지 않았으며 시간·위치·날씨를 임의 생성하지 않는다. 향후 시간별 API 수집기를 연결하면
환경 TTL과 일출·일몰 캐시 정책도 함께 검토해야 한다.

face_detected=false 또는 DMS 미수신·만료 시 DMS 네 값이 모두 null이다.
eyes_closed=false와 microsleep_ms=0은 현재 모델의 미지원 기본값이므로 졸음 계산에 쓰지 않는다.
현재 배포된 DMS 계약의 drowsiness_score와 행동 추정치를 사용하는 연결이다.

VehicleState.can_valid만으로 각 신호의 실제 수신 여부는 증명되지 않는다. 따라서 현재
`verified_vehicle_fields=()`로 전부 차단한다. 후속 CAN 담당자가 신호별 수신·최신성을 보증한
필드만 AdapterPolicy에 명시할 수 있다. 신호별 null·시각을 DDS에 표현하는 작업은 별도다.

## 실행

이 worktree에서 실행한다. 가상환경 설치는 랩탑/Pi 각각 필요하다.

```bash
python3 -m venv --system-site-packages .venv
. .venv/bin/activate
python -m pip install -r config/xgboost/requirements-infer.txt 'cyclonedds>=0.10'

# DDS 참가자·네트워크 발행·TTS가 없는 로컬 합성 미리보기
PYTHONPATH=board_runtime/src python -m codrive.apps.hmi_preview --scenario phone
# http://127.0.0.1:18080/
# --scenario normal / drowsy / missing 도 지원
```

Pi는 기존 `codrive-pi` 서비스를 재시작해 연결했다. 새 프로세스를 중복 실행하지 않는다.
서비스 drop-in은 `--xgboost-model`과 별도 가상환경을 지정한다. 수동 실행 명령은 다음과 같다.

```bash
PYTHONPATH=board_runtime/src python -m codrive.apps.pi \
  --dds-config config/integration/cyclonedds-pi-wired.xml \
  --xgboost-model artifacts/models/risk/B
```

`--xgboost-model`이 없으면 기존 경로가 실행되며 numpy/xgboost를 가져오지 않는다.
옵션을 주었는데 라이브러리·파일·체크섬 문제가 있으면 로그와 `/api/state.risk_model`에
model_error를 표시하고 기존 DMS 경고를 계속 처리한다. `--audio-alerts`는 추가하지 않는다.

## 표시와 경고 경로

Pi의 기존 fusion/risk/HmiCommand/TTS 경로는 유지한다. XGBoost는 별도 단일 작업 스레드에서
최신 입력 한 건만 대기시켜 실행한다. 입력 값이 같으면 예측을 재사용하되, 최신 센서 값과
유효기간을 매 Pi 주기마다 확인한다. 값 변경·미수신 시 이전 예측은 노출하지 않는다.
추론 오류는 model_error이며 전체 입력 미수신은 unavailable이다. LOW로 대신하지 않는다.

`/api/state.risk_model`은 모델 상태·입력 15개·missing_fields·withheld_sources·유효기간·
모델 버전·추론 시간·등급별 확률·warning_message를 담는다. pending 상태에는 이전 등급을
포함하지 않는다. 확률은 모델 클래스 확률이며 사고 확률이 아니다.

HMI는 LOW→NORMAL / MEDIUM→CAUTION / HIGH→WARNING 라벨과 한국어 문구를 표시한다.
부분 입력에는 `입력 제한`을 붙이며 15개 입력의 미수신·보류 상태를 펼쳐 볼 수 있다.
통신이 지연돼도 브라우저 자체 타이머가 입력 유효기간에 맞춰 예측·확률을 지운다.
Pi 연결이 끊기면 이전 모델 결과를 지우고 연결 확인 상태로 전환한다.

이번 연결은 화면 표시 전용이다. 모델에서 DDS risk_event/HmiCommand를 발행하지 않고
LocalSpeech에도 제출하지 않는다. 현재 기본 화면은 [팀 main HMI](MAIN_HMI.md)이며
규칙 CRITICAL 우선·동일 등급 규칙 우선 정책으로 안내한다. 기존 상세 모델 패널은
`/diagnostics`에서 따로 확인할 수 있다.
최종 위험도 통합·발화 정책·음성 품질 개선은 실제 입력 검증 후 진행할 작업이다.

## 랩탑 검증

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest
node --check src/codrive/apps/dashboard.js
python3 scripts/check-deployment.py
```

입력 누락·TTL·미래 시각·행동 미검출·CAN 검증 조건·단위 변환·음향 차단·모델 오류·
체크섬/계약 불일치·대기열 교체·기존 경고 보존·HTTP 경로를 검증한다.
원본 sample_input/stream_input/warning_demo 13개에서 B의 클래스·확률·문구·결측 목록이
정확히 일치했다. Chromium 데스크톱 1440px/모바일 390px 화면을 확인했다.
전체 Python 테스트 122개가 통과했고 CUDA/TensorRT가 필요한 음향 테스트 3개는 건너뛰었다.
변경 Python 파일의 Ruff 검사와 JavaScript 구문 검사도 통과했다.
## 실제 보드 배포 · 2026-10-21

세 보드 모두 SSH 로그인해 서비스·유선 주소·chrony를 확인했다. 유선 인터페이스를 지정한
모든 쌍의 양방향 ping에 성공했다. Jetson 서비스와 카메라 설정은 변경하지 않았다.

Pi의 기존 파일이 인계본과 같은 해시인지 확인하고 아래 경로에 백업했다.

- 코드: `/home/rp/ESW2026-integration`
- 모델: `artifacts/models/risk/B`
- 가상환경: `.venv-risk` (Python 3.12.3, xgboost-cpu 3.4.1, numpy 1.26.4, scipy 1.11.4)
- 서비스 override: `~/.config/systemd/user/codrive-pi.service.d/40-xgboost.conf`
- 백업·복구: `/home/rp/codrive-backups/xgboost-20261021.lxk9ps5s/rollback.sh`

Pi에 ensurepip가 없어 `python3 -m venv --without-pip --system-site-packages .venv-risk`로
환경을 만들고 시스템 pip를 해당 환경의 Python에서 실행했다. `xgboost-cpu` ARM64 wheel을
설치했으며 CUDA/NCCL은 설치하지 않았다. 기존 numpy/scipy/CycloneDDS를 사용한다.

Pi의 staging에서 입력/모델/앱 테스트 21개가 통과했다. 단독 모델 1,000회 추론은
중앙값 0.674ms, p95 0.842ms, 최대 1.440ms였다. 합성 입력을 모델 함수에만 넣어 측정했고
DDS에 발행하지 않았다. 전체 capture→HMI 지연이나 안전성 검증은 아니다.

서비스 재시작 후 네 heartbeat가 복귀했고 DMS·음향 수신 수가 계속 증가했다.
30초 모니터링 동안 모두 연결 상태를 유지하고 수신 거부는 0건이었다.
사용자가 C922 앞에 있는 동안 실제 `jetson-cabin.dms` sequence 52454가 입력되어
Pi에서 LOW/NORMAL 예측을 확인했다. DMS 네 관측값은 false/false/true/0.0이었고
나머지 미연결·보류 11개는 null이었다. 실제 수신 예측의 모델 계산 시간은 1.657ms였다.
이 결과는 해당 모델 출력이며 전체 주행 안전 보장은 아니다.
실제 Pi HMI는 `http://192.168.99.21:8080/`, 이 랩탑의 SSH 터널은
`http://127.0.0.1:18081/`이다. Wi-Fi 주소는 변경될 수 있다.

복구가 필요하면 Pi에서 위 rollback.sh를 bash로 실행한다. 백업한 파일과 기존 서비스
실행 경로로 되돌린다. 가상환경·모델 파일은 남지만 기존 앱에서 사용하지 않는다.

최초 배포 당시 실제 음성 출력은 물리 출력 장치가 없어 `auto_null` 오류였다. 후속 작업에서
Pi HDMI 출력 연결과 Supertonic 3 여성 F1 교체·사용자 청취 확인을 마쳤다.
[한국어 여성 TTS](TTS_KO.md)를 참고한다. XGBoost의 모델 발화 정책은 변경하지 않았다.
실도로 정확도·종단 200ms와 장시간 운용 검증은 남아 있다.
