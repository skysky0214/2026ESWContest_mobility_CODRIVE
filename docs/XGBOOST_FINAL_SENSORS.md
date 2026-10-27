# 최종 센서 XGBoost v3 · 2026-10-26

`artifacts/models/risk/final-sensors-v3/`에 모델, 전처리 계약, 합성 관측 10,000개와
평가 결과를 저장했다. 기존 B/v2 모델의 15개 입력은 별도로 유지한다.
이번 작업은 저장소의 학습·추론·Pi 연결 코드까지이며 실제 보드 서비스는 변경하지 않았다.

## 입력 계약

| 출처 | 관측·파생 입력 |
|---|---|
| 외부 카메라 · 8개 | `pedestrian_count`, `vehicle_count`, `bicycle_count`, `motorcycle_count`, `stop_sign_detected`, `central_object_present`, `max_bbox_area`, `traffic_light` |
| 내부 카메라 · 6개 | `driver_behavior`, `attention_score`, `drowsiness_score`, `looking_forward`, `phone_detected`, `behavior_duration_ms` |
| 음향 · 2개 | `audio_kind`, `audio_direction` |
| CAN · 7개 | `speed_mps`, `accelerator_pct`, `brake_pressed`, `speed_accel_mps2`, `accelerator_rate_pct_s`, `acceleration_duration_ms`, `pedal_rate_duration_ms` |
| 환경 · 5개 | `weather`, `is_night`, `precipitation_mm_h`, `snowfall_cm_h`, `visibility_m` |
| 유효성 · 8개 | `front_valid`, `driver_valid`, `audio_valid`, `speed_valid`, `pedal_valid`, `brake_valid`, `weather_valid`, `daylight_valid` |

총 36개 raw 입력을 범주형 one-hot 인코딩 후 **52개 모델 컬럼**으로 사용한다.
`vehicle_count`는 car/vehicle/bus/truck 합계이며 중앙 여부·박스 면적은 영상 좌표 특성이다.
거리나 충돌 임박으로 해석하지 않는다. 최종 센서 계약은
`board_runtime/src/codrive/sensor_features.py`, 인코딩은 `risk_ml/features_v3.py`에 있다.

차속은 **m/s**, 페달은 **0~100%**, 페달 변화율은 **%p/s**, 지속시간은 **ms**다.
기존 B/v2의 `vehicle_speed` km/h를 v3에 그대로 넣으면 안 된다.
CARLA의 throttle 0~1 변환과 시뮬레이션/실제 시각 매핑은 별도 입력 어댑터의 책임이다.

`traffic_light`는 RED/YELLOW/GREEN/OFF이며 불명은 null이다. 실제 OFF 관측과 미검출을 구분한다.
`audio_kind`는 none/siren/horn/other이며 유효한 무음 관측 none과 미수신 null을 구분한다.
사이렌·경적의 최종 방향만 left/right로 전달한다. 방향 생산자가 unknown을 내보내면 null이며
임의 방향을 채우지 않는다. 학습 데이터에는 양쪽 방향의 가상 이벤트를 포함했다.

유효성 false에 해당하는 값은 null로 유지하고, 모델에는 NaN으로 넣는다. CAN은 신호별 실제
수신 시각이 있어야 한다. 첫 관측이나 공백 직후처럼 변화율 계산 이력이 부족하면 변화율과
지속시간도 null이다. 입력 전부 미수신은 LOW가 아니라 unavailable이며 모델을 호출하지 않는다.
날씨와 주야간은 독립적으로 만료된다.

합의에서 제외한 차선, 거리·TTC, 객체 zone, 조향각 기반 위험, 급출발,
회전 중 과도한 가속, 가속 이상/급발진 원인 판단과 INS 가속도는 입력에 없다.
검출 신뢰도·원시 영상/음성도 추가하지 않았다.

## 공통 계산과 실행

`RiskEngine.evaluate()`가 규칙 판단에 사용한 운전자 행동·지속시간, 실제 CAN 관측 이력의
변화율·임계값 초과 지속시간, 객체 요약과 환경 유효성을 `last_features`로 제공한다.
학습 생성기와 Pi XGBoost가 이를 함께 사용한다. evidence 문자열을 파싱하지 않는다.
규칙 점수와 최종 위험 등급은 모델 특징에 넣지 않는다.

Pi에서는 기존 Rule-based risk 루프가 50 Hz로 실행된다. 선택한 XGBoost는 별도 worker에서
CPU 한 스레드, 최대 10 Hz로 실행하고 대기 입력은 최신 한 건만 유지한다.
v3는 마지막 예측의 **원래 입력·시각·유효기간**을 함께 표시한다. 새 값이 대기 중이어도
이전 예측이 아직 유효하고 센서 유효성 구성이 같으면 표시할 수 있다.
이때 `latest_input_pending=true`, `prediction_age_ms`로 표시 시점을 구분한다.
센서 유효성이 달라지거나 기존 입력이 만료되면 해당 이전 예측을 표시하지 않는다.

음향 방향은 양쪽 예측을 평균해 left/right만으로 위험도가 달라지지 않게 한다.
안내 문구에는 실제 입력 방향을 유지한다. tree gain은 관측 특징의 인과적 설명이 아니다.

```bash
# 저장소 루트, 전용 가상환경 사용
python3 -m pip install -r config/xgboost/requirements-train.txt

# 로컬 파일 한 건 추론
PYTHONPATH=board_runtime/src python3 -m codrive.risk_ml.infer \
  --model-dir artifacts/models/risk/final-sensors-v3 \
  --input artifacts/models/risk/final-sensors-v3/sample_input.json

# 실제 보드에서 실행할 때 지정할 모델 경로
bash scripts/run-pi.sh --xgboost-model artifacts/models/risk/final-sensors-v3
```

Pi 복사에는 `model.ubj`, `preprocess.json`, `metadata.json` 세 파일이면 된다.
XGBoost는 기존 화면 표시 전용 경로이며 DDS 경고나 음성을 발행하지 않는다.
규칙의 CRITICAL 우선순위는 유지한다. 모델은 LOW/MEDIUM/HIGH 세 등급이므로
규칙 WARNING과 CRITICAL을 모델 HIGH로 함께 학습한다.

## 데이터와 평가

시드 **20261026**, **200개 세션 × 50개 관측 = 10,000개**다. 관측 간격은 100ms이며
각 세션의 센서 조건·차량값·시작 시각·방향·날씨를 생성한다. 센서 누락, 얼굴 미검출,
CAN 신호별 미수신, 카메라 만료, 날씨 만료 사례를 포함한다.

정답은 `RiskEngine.last_candidates` 중 최대 severity로 정한다.
이벤트 반복 억제로 `evaluate()`가 None을 반환하더라도 위험 후보가 있으면 정답을 유지한다.
후보 없음=LOW, INFO=MEDIUM, WARNING/CRITICAL=HIGH다. 정답 코드·등급·시나리오·시각·세션 ID는
데이터 감사용 메타데이터이며 학습 행렬에 넣지 않는다.

| 분할 | 세션 | 관측 | 용도 |
|---|---:|---:|---|
| train | 140 | 7,000 | 모델 학습 |
| validation | 30 | 1,500 | early stopping |
| test | 30 | 1,500 | 최종 평가 |

서로 다른 분할에 같은 세션이 들어가지 않는다. 동일 생성기를 사용하므로 이 평가는
**가상 데이터에서 현재 규칙을 재현하는 성능**이다. 독립적인 실제 주행 안전성 검증이 아니다.
최종 test 일치율 **97.80%**, macro F1 **0.9813**이며 상세 혼동행렬·클래스별 지표는
모델 디렉터리의 `training_report.json`을 따른다. 실차/CARLA 수집 데이터로 추가 검증해야 한다.

`data/dataset.csv`와 `data/dataset.jsonl`에 10,000개 특징·정답·분할을 저장했다.
`data/sensor_stream.jsonl.gz`에는 같은 순서의 원본 가상 센서 메시지가 있다.
테스트는 10,000개를 재생해 특징과 정답의 완전 일치를 확인한다.
학습 코드·규칙·설정·데이터 해시는 metadata, 산출물 해시는 manifest에 저장한다.

재학습할 때는 기존 산출물과 섞이지 않도록 빈 output 디렉터리를 지정한다.

```bash
PYTHONPATH=board_runtime/src python3 -m codrive.risk_ml.train \
  --samples 10000 --seed 20261026 \
  --output artifacts/models/risk/reproduced-v3

PYTHONPATH=board_runtime/src PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  python3 -m pytest board_runtime/tests/test_risk_ml_v3.py board_runtime/tests/test_risk_ml.py
```

학습은 CPU hist, 최대 400 rounds, depth 5, eta 0.05, validation 35 rounds early stopping으로
실행했다. 최적 시점까지만 저장하며 이번 모델은 299 rounds다.
추론 벤치마크는 개발 호스트 x86_64의 1,000회 측정이며 Pi 실측과 구분한다.
선택·저장 방식은 [XGBoost 공식 문서](https://xgboost.readthedocs.io/en/stable/python/python_api.html)의
EarlyStopping(save_best=True)를 사용한다.
