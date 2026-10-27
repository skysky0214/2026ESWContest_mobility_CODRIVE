# main 통합과 실제 보드 실행

2026-10-21 `integration/xgboost-hmi-20261021`의 `e5104a6`을 main에 합쳤다.
합치기 직전 main은 `904e112`이며 STM32/G29 작업을 포함한다.

## 코드 구조

- `src/codrive`: 기존 main의 팀 모듈 구조. 원본 소스는 그대로 보존한다.
- `stm32`: main의 STM32 펌웨어·G29 도구를 그대로 보존한다.
- `board_runtime/src/codrive`: 실제 보드에서 검증한 integration 런타임.
  DMS·음향·Pi 융합·XGBoost B·여성 F1 TTS·카메라 HMI를 포함한다.
- `config/board`: 보드 런타임용 일반 프로필. `config/devices`는 기존 main 프로필을 보존한다.
- `config/integration`: 실제 유선 네트워크·DMS·마이크·MJPEG 설정.
- `scripts/run-*.sh`: 보드용 PYTHONPATH를 명시한다. 전방 main 앱은 `src`를 사용한다.

main의 `risk/`, `guidance/`, `hardware/camera/` 패키지와 integration의 같은 이름 파일은
계약이 달라 한 Python import 경로에 섞을 수 없다. 소스 루트를 분리해 main의 팀 작업과
현재 보드 런타임을 함께 보존했다. 기존 DDS 타입의 Python 모듈 경로·wire 계약은 유지한다.
두 소스 루트를 동시에 한 Python 프로세스에서 import하지 않는다.

## 합치기 직전 상태

| 보드 | 실제 실행 코드 | 별도 원본/참조 Git |
|---|---|---|
| Jetson 1 | `ws/ESW2026-integration` 통신 + `ws/ESW2026-main-front-20261021` 영상(main b93538a 사본) | `ws/adas/ESW2026-prototype-github`: prototype 04de988 |
| Jetson 2 | `EWS/ESW2026-integration` 배포 사본 | `EWS/ESW2026`: feature/jetson-cabin-dms b69ceb3 |
| Pi | `ESW2026-integration` 배포 사본 | 실행 폴더의 새 Git은 main 이름만 있고 커밋 없음. `ESW2026-main-reference`는 참조 main |

따라서 기존 실행이 모두 prototype인 상태는 아니었다. Pi의 미완성 Git index에는
가상환경·캐시도 올라가 있었으므로 그 index를 커밋하거나 덮어쓰지 않는다.

## main 배포

각 보드의 새 Git 체크아웃을 `main` 브랜치로 만들고 같은 main 커밋을 실행한다.

| 보드 | main 체크아웃 | 활성 서비스 |
|---|---|---|
| Jetson 1 | `/home/one/ws/ESW2026-main` | codrive-front-standby, codrive-front-camera |
| Jetson 2 | `/home/two/EWS/ESW2026-main` | codrive-cabin-link, codrive-audio, codrive-dms |
| Pi | `/home/rp/ESW2026-main` | codrive-pi |

`scripts/activate-main.py --role front|cabin|pi`가 user 서비스의 `60-main.conf`로
실행 폴더·launcher를 전환한다. Pi는 `--python`으로 기존 `.venv-risk/bin/python`,
전방은 기존 JetPack `.venv/bin/python`을 지정한다. 라이브러리를 재설치하지 않는다.
기존 실행 폴더와 설정을 보존하며, 변경 전 drop-in과 복구 스크립트 경로를 출력한다.
Pi의 외부 영상 설정과 여성 TTS, XGBoost 모델, 음향 자동 경고 보류를 유지한다.
Jetson 2 내부 MJPEG 8082와 Pi 우측 아래 영상·위치 전환도 함께 활성화한다.

각 보드에서 확인:

```bash
git branch --show-current
git rev-parse HEAD
cat .codrive-deployment.json
systemctl --user show <서비스> -p WorkingDirectory -p ExecStart
```

## 검증

```bash
CODRIVE_TEST_PYTHON=<검증 Python> bash scripts/check-main-and-boards.sh
PYTHONPATH=board_runtime/src <검증 Python> scripts/check-hmi-cameras.py
```

main 테스트와 보드 테스트는 별도 Python 프로세스에서 실행한다. main 기존 테스트의
누락된 json import 및 현재 점수 85(WARNING)·정차 문구와 맞지 않던 기대값만 정리했다.
실제 main 위험 정책·안내 문구는 바꾸지 않았다. 보드 모델·클래스·마이크 정책도 유지한다.

## 실제 적용 결과 · 2026-10-21

런타임 통합 커밋은 `71c6ca0`이며, 세 보드 모두 위 main 체크아웃에서 실행함을 확인했다.
이후 문서/배포 manifest 갱신도 같은 main에 반영한다. 원래 작업 폴더·Pi의 미완성 Git은 보존했다.

- main 테스트 144개, 보드 런타임 테스트 138개 통과(CUDA/TensorRT 3개는 랩탑에서 제외).
- 실제 Jetson 1 MJPEG 테스트 3개, Jetson 2 DMS·영상·음향 테스트 29개, Pi 테스트 22개 통과.
- 세 보드 서비스 6개 정상, Pi의 heartbeat 4종 모두 연결, DDS 거부 0개.
- 실제 외부·내부 영상 디코딩과 위치 전환, Pi 모니터의 두 영상 표시 확인.
- XGBoost B 실제 입력·여성 F1 로컬 음성 재생 정상. 음향 자동 경고·외부 인지의 모델 입력은 보류 유지.

복구 스크립트:

- Jetson 1: `/home/one/codrive-backups/main-front.j6t2sq1p/rollback.sh`
- Jetson 2: `/home/two/codrive-backups/main-cabin.h_krwuw9/rollback.sh`
- Pi: `/home/rp/codrive-backups/main-pi.zoskchv2/rollback.sh`

원래 drop-in·실행 폴더로 되돌리고 서비스를 재시작한다. 이력 확인은 Git HEAD와
`.codrive-deployment.json`, 서비스 WorkingDirectory를 함께 확인한다.
