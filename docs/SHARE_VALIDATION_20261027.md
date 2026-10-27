# 공유 브랜치 검증 — 2026-10-27

대상은 latest 워크스페이스의 누적 통합 변경과 세 모드 실행 프로필이다. 이 정리 작업은 현재 차량에서 실행 중인 실차 서비스를 전환하거나 소스를 재배포하지 않았다. 공유 버전에는 새 `carla-screen` 표시 플래그가 포함되므로, 나중에 보드로 새 소스를 배포한 뒤 그 프로필을 사용한다.

- 보드 런타임: 320개 실행, 315 통과, 5 skip, 실패/오류 0.
- src / 카메라 / 도구: 176개 통과, 실패/오류 0. 세 모드 dry-run 5개 테스트 포함.
- 합계 491개 통과, 5 skip: 테스트 PC의 TensorRT 미설치로 GPU 검사 1개, 제거된 구형 B 가중치를 요구하는 비교 검사 4개를 skip했다.
- 저장된 10,000행 학습 데이터의 전체 재생 비교와 1,500행 저장 확률 전체 비교 2개는 이번 공유 변경의 실행 경로·프로필 검증 범위에서 제외했다. 모델 재학습·가중치 변경은 하지 않았다.
- `scripts/check-deployment.py`: 파일 400개 해시 일치, DMS 모델 해시 일치.
- bash / JavaScript 문법과 `git diff --check` 통과. 새/변경 파일에서 개인키·GitHub 토큰·AWS 키·평문 sshpass 패턴 미검출. 비밀키/환경 파일, 캡처·로그·빌드 결과는 제외.

실행 명령:

```bash
PYTHONPATH=board_runtime/src PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest board_runtime/tests -q \
  -k 'not saved_dataset_replays_exact_features_labels_and_session_splits and not saved_test_probabilities_match_loaded_cpu_predictor'
PYTHONPATH=src PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest tests -q
python3 scripts/check-deployment.py
scripts/select-integration-mode.sh carla --dry-run
scripts/select-integration-mode.sh carla-screen --dry-run
scripts/select-integration-mode.sh real-vehicle --dry-run
```

검증 환경은 CPU 테스트용 Python venv이며, 보드의 GPU 추론·실차 동작을 이 테스트만으로 보증하지 않는다. 과거 CARLA 주행 경고, 실물 영상 수신, 실차 정차 CAN 조회·풀페달 경고 기록은 각 기능 문서의 범위로 구분한다. 실차 거리/TTC·실차 주행 성능과 이동 후 모니터 재보정은 여전히 별도 확인 대상이다.
