# 규칙 기반 위험 판단

2026-10-26 복구: 실제 보드 경고는 `board_runtime/src/codrive/risk.py`의 팀 FSM/TTC 코어를 사용한다. `config/risk.yaml`과 DMS 2초/3초 확인, CAN 차속 기반 급가속 교차 확인 및 행동 안내를 복원했다. XGBoost v3는 별도 표시 전용이다. 아래 축소 규칙 기록은 이전 배포 이력이다.

2026-10-26 이후 실제 Pi의 최종 센서 규칙은 [FINAL_SENSOR_RISK.md](FINAL_SENSOR_RISK.md)를 따른다. 아래는 기존 `src` 참조 엔진의 축소 규칙 기록이다.

2026-10-26: XGBoost 입력 항목이 없는 정보를 사용하는 위험 판단을 제거했다.
`src/codrive/risk`와 실제 보드용 `board_runtime/src/codrive/risk.py`에 함께 적용한다.

## 제외한 판단

- 거리·상대속도·TTC·개별 객체 종류/신뢰도 기반 충돌 판단
- 객체 방향 구역에 따른 위험 가중치와 운전자 경고 상향
- 차선 편차·차선 신뢰도·방향지시등/비상등에 따른 차선 이탈 판단
- INS 종방향 가속도를 사용하는 CAN/INS 가속 교차 검증
- 신호등·음향 신뢰도를 직접 읽는 risk 조건

관련 위험 정책과 YAML 설정도 제거했다. 센서 수집·검출·메시지 필드는 유지한다.
검출 단계의 신뢰도 필터는 센서 생산자가 담당하며 risk가 별도 임계값을 적용하지 않는다.
원시 메시지에 제외한 값이 있어도 위험 점수·단계·후보 우선순위에 사용하지 않는다.
INS 누락은 이제 기존 `src` 위험 엔진의 필수 입력 누락으로 취급하지 않는다.

## 남은 판단

| 입력 | 기존 `src` 엔진 | 실제 보드 엔진 |
|---|---|---|
| 차속·브레이크·신호등 색상 | 적색·고속 황색 접근 | 적색 접근 |
| 운전자 상태 | 졸음·휴대폰·주의 분산, 신호 위험에 대한 운전자 가중치 | 주행 중 주의 분산, cabin-only INFO 감지 안내 |
| 음향 종류 | 사이렌·경적, 속도·음원 방향 맥락 | 사이렌, 황색 신호 맥락 |

보드의 음향 자동 경고는 여전히 `--audio-alerts`를 명시해야 한다.
XGBoost의 기존 15개 입력 계약, CAN 검증 제한, 외부 카메라·음향 입력 보류는 변경하지 않았다.
따라서 이 변경이 센서 연결이나 XGBoost 재학습을 완료했다는 의미는 아니다.
보행자/차량 개수 기반의 새로운 위험 규칙도 추가하지 않았다.

## 기존 `src` 엔진 실행

```bash
PYTHONPATH=src python3 -m codrive.simulation.scenarios --policy config/risk.yaml
PYTHONPATH=src python3 -m codrive.simulation.scenarios --policy config/risk.yaml --details
PYTHONPATH=src python3 -m codrive.simulation.simulator
```

데모는 정상, 휴대폰 사용, 적색 신호에서 정차/주행, 사이렌, 복합 위험, CAN 누락을 비교한다.
단일 simulator는 적색 신호 접근 → HMI를 실행한다. 센서부터 실제 화면까지의 지연을 측정하지 않는다.

`DecisionPipeline.ingest()`는 공통 메시지를 받고 `step()`은 snapshot, assessment와 선택적
HMI 명령을 반환한다. 이미 완성된 snapshot에는 `RiskEngine.assess()`를 사용할 수 있다.

## 최신성과 상태

입력의 측정 시각·TTL·CAN 유효성·운전자 검출 여부를 확인한다. CAN 누락을 정차로,
행동 미검출을 정상 주시로 바꾸지 않는다. 복합 규칙은 측정 시각 차이가
`max_context_skew_ms` 이내인 입력만 결합한다.

기존 `src` 엔진은 NORMAL `<30`, CAUTION `<60`, WARNING `<90`, EMERGENCY `>=90`을 사용한다.
후보 점수를 합산하지 않고 위험 단계, 점수, 동률 우선순위, 대상 ID로 선택한다.
FSM은 상향을 즉시 적용하고 하향에 20/45/75점 해제 임계값과 500ms 유지 시간을 적용한다.
cooldown 중에도 `RiskAssessment`는 갱신되며, 단계 상승이나 대상 변경은 안내 억제를 우회한다.

`RiskAssessment.degraded`와 `unavailable_rules`로 입력 가용성을 별도 확인한다.
`NORMAL`만으로 전체 센서가 연결됐거나 환경이 안전하다고 해석하지 않는다.

## 검증

두 Python 소스 루트는 분리해서 실행한다.

```bash
PYTHONPATH=src PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest tests/risk tests/integration tests/apps
PYTHONPATH=board_runtime/src PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest board_runtime/tests
```

제외한 값이 위험을 생성·상향하지 않는지, 신호등 색상과 음향 종류가 신뢰도 값과 독립적으로
처리되는지, 반복 안내·상태 해제·XGBoost 입력 계약과 기존 DMS 안내가 유지되는지 검증한다.

## 날씨·주야간 통합 · 2026-10-26

기존 팀 TTC/FSM의 객체·신호·차선 위험 점수에 환경 가중치를 적용한다.
주야간은 기존 일출/일몰 API에서 산출한 is_night/daylight_valid를 사용한다.
야간 1.15, 우천 1.2, 눈 1.35, 안개 1.4, 뇌우 1.35이며 시정 저하도 반영한다.
시야 가중치와 노면 가중치를 곱하되 최대 2.0으로 제한한다. 우천·야간은 1.38이다.
값은 config/risk.yaml에서 조정한다. 새 API 호출은 위험 판단 루프에 추가하지 않는다.
환경 메시지는 3초 이내이며 날씨 자체의 유효기간/API 유효성과 주야간 유효성을 별도로 검사한다.
만료·미수신 시 해당 가중치는 1.0이다. 환경만으로 새 위험 후보를 만들지 않는다.
객체 가중치는 TTC 단계 하한과 비접근·경로 불확실성 상한보다 먼저 적용한다.
따라서 기존 TTC 긴급 기준·안전 상한·우선순위·FSM 히스테리시스를 유지한다.
DMS 지속 시간과 급가속 교차 확인 조건, XGBoost v3 모델·입력 경로는 변경하지 않는다.
API state의 environment_risk와 각 도로 위험 후보 evidence에 가중치·환경 근거를 기록한다.

## 조향 기반 근거리 충돌 경로 · 2026-10-26

실제 Pi 앱은 config/camera/carla-driving-corridor.json을 읽는다.
ADAS_project/app/control/ttc_warning.py의 bicycle future-ego-footprint 방식을 참고했다.
카메라 bbox 바닥점의 보정된 지면 좌표와 CAN 차속·조향만 사용하며 CARLA 객체 좌표는 사용하지 않는다.
현재 CARLA Model 3 정적 제원: wheelbase 3.005m, 차량 반폭 1.082m, 여유폭 0.15m.
CAN 조향각은 CARLA normalized steer*450이므로 실제 최대 조향각 70도와 속도별 steering curve로 환산한다.
구형 실차용 steer ratio 14.25를 CARLA에 그대로 적용하지 않는다. 실차 프로필은 steering_ratio 모드를 사용한다.
최대 30m·4초의 예측 차량 점유영역을 검사한다. 보정 영역 밖·근거리 ROI 밖·예상 충돌 없는 물체는
충돌 후보에서 제외한다. 조향하면 경로가 휘며, 추적한 객체의 자차 운동 보상 속도로 교차 진입도 검사한다.
TTC는 해당 예측 점유영역과 객체가 처음 겹치는 시점이다. TTC 긴급 기준과 팀 FSM은 유지한다.
필터는 규칙 판단용 snapshot에만 적용하고 원본 검출·XGBoost v3 입력은 그대로 유지한다.
API state의 driving_corridor에 예상 경로, 물체별 좌표·제외 이유·TTC·accepted/filtered 수를 기록한다.
--no-driving-corridor로 이전 판단을 선택할 수 있다. 물리 카메라/모니터 재촬영은 별도 지면 보정이 필요하며,
CARLA 가상 카메라 보정값을 물리 카메라에 그대로 쓰면 안 된다.

## 경고 민감도·차선 추적 조정 · 2026-10-27

사용자 승인: 급가속은 실제 엑셀 90%(0.9), 차속 추정 가속도 4.0m/s² 이상,
브레이크 미입력이 500ms 지속될 때 후보를 생성한다. 차속 추정 이력 준비 시간은 별도다.
차선 편위 기본 기준은 WARNING 0.65m / EMERGENCY 1.0m이며 각각 500ms 확인한다.
확인은 고유 capture timestamp로 계산하며 결측·브레이크·깜빡이·방향 전환 등 불충족 시 재시작한다.
날씨·운전자 가중치와 TTC 충돌 우선순위는 유지한다. XGBoost v3 모델은 재학습하지 않는다.

Jetson UFLDv2는 upstream hybrid decoder 방식(중앙 row 슬롯 1/2, 바깥 col 슬롯 0/3)으로
최대 4개 차선 후보를 읽는다. 기준: https://github.com/cfzd/Ultra-Fast-Lane-Detection-v2/blob/master/demo.py
지면상 폭 2.6~4.6m, 곡선 일관성·이전 경계와 오차·CAN 운동 예측으로 우리 차로 좌우 쌍을 추적한다.
급격한 경계 점프·한쪽 누락은 lane_offset_m=null/confidence=0으로 발행하며 옛 위치를 새 측정값으로 내보내지 않는다.
정상 쌍은 초기 3프레임/300ms, 경계 변경은 일관된 새 쌍을 재확인한 뒤 채택한다.
디버그 영상은 선택된 쌍을 노란색 ego-left/right, 다른 후보는 회색으로 구분한다.
Jetson JSONL 로그 lane_association에 추적 상태·후보 수·선택 슬롯·변경 오차를 기록한다.
