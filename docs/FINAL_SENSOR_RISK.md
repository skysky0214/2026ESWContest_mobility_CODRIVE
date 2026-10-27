# Pi 최종 센서 결과 기반 규칙 · 2026-10-26

이 문서는 이전 배포 규칙의 기록이다. 현재 실제 경고는 팀 FSM/TTC 코어로 복원했다. 여기의 엔진은 `v3_feature_engine.py`로 분리되어 XGBoost v3 특성 계산·기존 학습 재현에만 사용되며, Pi 경고·TTS를 선택하지 않는다.

실제 실행 대상은 `scripts/run-pi.sh`가 선택하는 `board_runtime/src/codrive`다.
기존 `src/codrive/risk`는 앞서 축소한 참조 엔진이며 이번 최종 보드 규칙의 실행 경로가 아니다.
규칙의 `last_features`가 XGBoost v3와 학습 로그에도 같은 관측·파생값을 제공한다.
[36개 입력 모델·합성 데이터 학습](XGBOOST_FINAL_SENSORS.md)을 참고한다.

## 입력과 판단

| 입력 | 보드 규칙 |
|---|---|
| 외부 객체 종류·개수·정규화 bbox | 보행자·차량·자전거·오토바이 주의 후보. 화면 중앙/면적은 영상상의 특성이며 거리·충돌 임박으로 해석하지 않는다. 정지 표지판도 별도 주의 후보다. |
| 신호등 최종 색상 + CAN 속도·브레이크 | 주행 중 미제동 적색·황색 접근. 같은 시각의 운전자 상태와 환경 가중치를 결합한다. |
| 운전자 최종 행동·주의도·졸음 점수 | 휴대폰·졸음·고개 떨굼·주의 분산. 새 관측의 지속시간에 따라 위험을 높인다. 차량값이 없으면 주행을 가정하지 않고 INFO 감지 안내만 제공한다. |
| 음향 최종 kind + direction | 사이렌·경적. left/right는 동일 기본 위험도에 방향별 화면·음성 안내를 제공한다. 방향 변경은 cooldown을 우회한다. |
| CAN 가속페달·차속 이력 | 급페달 조작, 차속 변화 기준 급가속 추정. 실제 신호 수신 시각·유효기간·연속 표본으로만 계산한다. |
| 날씨·강수/강설·가시거리·야간 | 시야 가중치와 노면 가중치의 곱을 2.0 이하로 제한한다. 시야 요인끼리는 최댓값을 사용해 중복 곱셈을 막는다. |

차선 위험, 거리·TTC·객체 zone 기반 충돌 판단, 급출발, 회전 중 과도한 가속,
가속 이상/급발진 의심, INS 가속 교차 판단은 사용하지 않는다.
검출 신뢰도는 센서 생산자의 필터 책임이며 risk 점수에 다시 가중하지 않는다.

후보는 severity, score, code 순으로 정렬한다. 높은 단계의 경고를 낮은 단계의 큰 점수가
덮어쓰지 않는다. 도로 객체 주의는 환경에 의해 WARNING까지 가능하지만 충돌 CRITICAL로
만들지 않는다. 적색/황색 신호는 환경/운전자 보정 후 높은 점수에서 CRITICAL로 상승할 수 있다.

`/api/state.rule_risk`는 모든 후보와 미수신 입력을 제공한다. 기존 이벤트/HMI 경로는
가장 우선순위가 높은 안내를 전달하며 반복 안내를 억제한다.

## 실행

```bash
bash scripts/run-pi.sh --latitude 37.5665 --longitude 126.9780
# 좌표는 실제 운용 위치로 지정한다. 위 숫자는 실행 형식 예시다.
```

`CODRIVE_LATITUDE` / `CODRIVE_LONGITUDE` 또는 `config/environment/pi5.yaml`로도 설정할 수 있다.
고정 좌표가 없으면 신선하고 fix가 유효한 `InsState`를 기다린다. 임의의 기본 도시를 사용하지 않는다.
설정은 `--environment-config`, 규칙은 `--risk-config`, CAN은 `--can-config`로 지정한다.
`--no-environment`와 `--no-audio-alerts`로 해당 입력을 명시적으로 끌 수 있다.
음향 자동 경고와 통합 프로필의 경적 발행은 이번 버전에서 기본 활성화했다.

## 환경 수집 주기와 캐시

- 날씨: 시작 시 캐시 확인, 성공 조회 후 **3600초마다** 갱신.
- 일출·일몰: 현재 지역 날짜별 **하루 한 번**, 같은 날 재시작 시 디스크 캐시 재사용.
- 현재 시각 API: 사용하지 않는다. 주간/야간은 기기 시각과 저장된 일출·일몰을 1초마다 비교.
- 수집과 HTTP(8초 timeout): 별도 worker. Pi의 50Hz risk 루프는 최신값 큐만 확인한다.
- 유효시간: weather 수신 75분/관측 90분 중 더 이른 시각. 새 포장 timestamp가 관측 유효시간을 갱신하지 않는다.
- 캐시 위치: `artifacts/cache/environment.json`. 시간대와 위치·지역 날짜·원본 관측 시각을 검증한다.
- API 실패: 60초부터 최대 600초까지 backoff. 유효기간이 끝난 날씨를 맑음으로 바꾸지 않는다.
- GNSS 위치가 만료되거나 조회 지역에서 20km 넘게 이동하면 해당 환경값은 미사용으로 처리한다.
  위치 이동만으로 시간당 조회 간격을 줄이지는 않는다. 다음 정기 날씨 조회에서 새 지역의
  일출·일몰도 함께 갱신하며, 같은 지역·날짜에서는 다시 조회하지 않는다.

[Open-Meteo 공식 문서](https://open-meteo.com/en/docs)의 current 날씨·강수·강설·가시거리와
날짜별 sunrise/sunset을 사용한다. API 관측 간격의 강수량은 mm/h, 강설량은 cm/h로 변환한다.
호출은 고정 HTTPS endpoint만 사용한다. 인터넷 연결이 없어도 나머지 센서 risk는 계속 계산한다.

## CAN 수신과 검증

Panda는 수신 전용 silent 모드를 사용한다. `VehicleWorker`가 10ms 간격으로 수신하고 최신
상태만 Pi에 전달한다. `config/can/ev6_2022.example.yaml`은 검증 전 placeholder이므로 실제 DBC와
신호 매핑을 넣기 전에는 `waiting_configuration`으로 표시한다. 임의 CAN ID를 생성하지 않는다.

`required_signals`의 각 항목은 다음 구조로 지정한다. message_id와 signal은 실제 DBC에 맞춘다.

```yaml
required_signals:
  speed_mps:
    message_id: <검증한 정수 ID>
    signal: <DBC의 차속 신호 이름>
    bus: 0
    scale: 0.2777777778  # DBC 결과가 km/h인 경우에만 m/s 변환
    offset: 0
```

같은 방식으로 `accelerator_pct`, `brake_pressed`, `steering_wheel_deg`를 매핑한다.
브레이크는 변환 후 0/1, 페달은 %, 조향각은 deg다. 조향각은 표시용이며 가속 위험에는 쓰지 않는다.

`signal_times_ns`는 speed/pedal/brake/steering의 실제 수신 시각을 담는다. 미수신 신호에 시각을
붙이지 않는다. 새 신호 표본만 최대 32개씩 저장하고, 중복·역순·장시간 공백·source/boot 변경은
연속 관측으로 인정하지 않는다. 값이 같은 새 표본은 정상적으로 처리한다.
기존 메시지에 metadata가 없으면 급페달/급가속 규칙은 동작하지 않는다.

초기 실험 임계값은 `config/rules/final-sensors.yaml`에 있다.
급가속 3m/s²/300ms, 페달 변화 300%p/s·20%p 이상·최종 60%/100ms를 사용한다.
실차의 급발진 원인이나 운전자 의도를 확정하는 규칙은 없다.

## 음향 방향과 음성

음원 방향 추정 알고리즘 자체는 이후 음향 생산자에 연결한다. 이번 구현은 최종
`AudioEvent.direction=left/right`를 실제 입력으로 받아 risk와 HMI/TTS로 전달한다.
현재 mono 생산자의 unknown을 임의의 left/right로 바꾸지 않는다. 방향 미수신은 일반 소리 안내다.
원시 사이렌·경적 확률은 risk에서 다시 분류하지 않는다.

기존 F1 WAV는 그대로 사용한다. 새 방향·급가속 문구에 WAV가 없으면 음성 worker에서만
`espeak-ng -v ko --stdout`으로 합성해 `paplay`로 재생하며 `/api/state.speech.last_engine`에 표시한다.
모든 문구를 같은 F1 음질로 쓰려면 기존 `codrive.speech build` 명령으로 확장 문구까지 미리
생성한 캐시를 배포한다. 실시간 risk 루프에서는 음성 합성·재생을 하지 않는다.

## 배포 범위와 계약

Pi·Jetson 생산자를 함께 갱신한다. VehicleState/EnvironmentState의 필드를 추가했고,
보드 TrackedObject가 기존 전방 생산자의 in_path 필드를 읽을 수 있도록 맞췄다.
구버전의 엄격한 JSON 소비자와 혼합 배포하지 않는다. wire envelope 버전은 1로 유지한다.

외부 카메라 서비스의 `--dry-run`을 제거하고 유선 DDS 설정을 연결했다. 실제 보드 서비스
재시작·센서 연결·CAN DBC 확인은 이 저장소의 로컬 테스트와 별개다.

## 검증과 성능

```bash
PYTHONPATH=board_runtime/src PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest board_runtime/tests -ra
PYTHONPATH=src PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest tests/common tests/hardware tests/risk tests/integration tests/apps
PYTHONPATH=board_runtime/src python3 tools/benchmarks/pi_rules.py --iterations 10000
```

테스트는 HTTP 가짜 응답과 로컬 버스를 사용한다. 시간당 호출·일별 캐시·실패/만료·야간 전환,
CAN 신호별 수신·관측 지속성, 제외 규칙의 비활성화, 방향 변경 안내, 실제 Pi orchestration을 확인한다.
벤치마크는 실행한 기기의 측정이며, DDS·실제 브라우저 영상·API·TTS가 포함된 Pi 종단 지연을
증명하지 않는다. 최종 운용 전 같은 스크립트와 전체 앱의 주기 초과율을 실제 Pi에서 측정한다.
