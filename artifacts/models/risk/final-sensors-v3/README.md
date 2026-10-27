# Final sensor XGBoost v3

합성 관측 10,000개, 200개 세션. 28개 관측·파생값 + 8개 유효성 표시를 52개 모델 컬럼으로 인코딩한다.

정답은 현재 RuleEngine의 후보 중 최대 severity다. 반복 경고 cooldown은 정답에 영향을 주지 않는다. 규칙 점수·정답·세션 ID·시나리오·시각은 모델 입력에서 제외한다. 후보 없음=LOW, INFO=MEDIUM, WARNING/CRITICAL=HIGH.

세션 단위로 70/15/15 분리했고 validation만 early stopping에 사용했다. 최종 합성 test 일치율 97.80%, macro F1 0.9813. 실제 주행 정확도나 사고 확률을 뜻하지 않는다. 실차/CARLA 데이터 검증은 별도다.

음향 left/right는 대칭 예측을 평균해 같은 위험도로 처리한다. 안내 방향은 실제 입력을 유지한다. NaN은 결측이며 정상/0으로 대체하지 않는다. 전부 미수신이면 판단하지 않는다.

- `model.ubj`, `preprocess.json`, `metadata.json`: 함께 배포할 CPU 추론 파일
- `training_report.json`, `learning_curve.json`: 평가·파라미터·중요도·호스트 벤치마크
- `data/dataset.csv`, `data/dataset.jsonl`: 정확히 10,000개의 특징·정답·세션 분할
- `data/sensor_stream.jsonl.gz`: 특징을 재계산할 원본 가상 센서 메시지
- `data/test_predictions.jsonl`: 최종 test의 정답·확률·예측
- `manifest.json`: 산출물 SHA256

저장소 루트에서, 별도의 빈 output 디렉터리를 지정해 재현한다.

```bash
PYTHONPATH=board_runtime/src python3 -m codrive.risk_ml.train --samples 10000 --seed 20261026 --output artifacts/models/risk/reproduced-v3
PYTHONPATH=board_runtime/src python3 -m codrive.risk_ml.infer --model-dir artifacts/models/risk/final-sensors-v3 --input artifacts/models/risk/final-sensors-v3/sample_input.json
```

현재 Pi 경로는 화면 표시 전용이다. `--xgboost-model`로 이 디렉터리를 지정하면 기존 Rule-based risk와 함께 실행한다. 실제 보드 배포는 이 학습 작업에 포함되지 않는다.
