# main HMI와 Pi 실제 입력 연결

2026-10-21 Pi에서 GitHub 인증을 완료하고 원격 main을 직접 fetch했다.
최신 커밋은 `b93538ae36b4cab261de0ee07c5d9a3bd3c03c18`이며,
`src/codrive/hmi/web/index.html`을 `board_runtime/src/codrive/apps/main_hmi.html`로 그대로 보관한다.
원본 SHA-256은 `b099ea4b096b784ca890299b5168cb2ba1051754f8df8e09e9139dad90d9e908`이다.

기본 `/` 화면은 팀원이 만든 main의 CSS·마크업을 사용한다. `main_hmi.py`는 제공할 때
원본의 데모·카메라·CAN 폴링·외부 날씨 스크립트를 `main_hmi_live.js`로 교체한다.
현재 integration의 DDS·XGBoost·한국어 여성 TTS는 기존 Pi 서비스에서 유지한다.
전체 main Python 구조를 현재 보드 런타임에 덮어쓰지 않았다.

후속 Git 변경으로 우측 바 아래 내부 영상과 **⇄ 위치 전환** 버튼을 추가했다.
[내부 카메라 HMI](CABIN_CAMERA_HMI.md)에 설정·검증·보드 배포 상태를 기록했다.

- 실제 화면: `http://192.168.99.21:8080/`, Pi 모니터 Firefox 전체 화면으로 실행 확인.
- 상세 진단: `/diagnostics`, 전체 상태: `/api/state`, 화면용 상태: `/api/hmi`.
- XGBoost LOW/MEDIUM/HIGH를 NORMAL/CAUTION/WARNING 색상으로 표시한다.
  LOW라도 입력이 부족하면 '낮음 · 입력 제한'이며 전체 주행 안전을 뜻하지 않는다.
- 규칙 WARNING은 모델 WARNING과 같을 때 우선하며 CRITICAL은 항상 최우선이다.
  INFO는 운전자 상태 안내(CAUTION)로 표시한다. 모델 출력은 음성·DDS 경고를 만들지 않는다.
- Pi의 응답 시각을 기준으로 DMS 400ms, CAN 120ms, 음향 1000ms와 모델 유효 기한을 확인한다.
  상태 갱신이나 HTTP 연결이 1.5초 끊기면 표시를 연결 대기로 바꾼다.
- CAN 미수신은 `—`, 날씨는 미수신으로 표시한다. 외부 영상은 10/21 연결을 확인해
  [GMSL 영상 경로](FRONT_CAMERA_HMI.md)를 활성화했다. 외부 인지의 모델 입력은 아직 보류다.
  음향은 수신 여부만 표시하며 자동 경고 보류를 유지한다.
- 원본 데모 버튼과 1~4/T 키는 실행되지 않는다. 외부 날씨 API를 호출하지 않는다.
  카메라는 명시한 `--front-camera-stream` 또는 `CODRIVE_FRONT_CAMERA_STREAM`이 있을 때만 요청한다.
- 여성 음성 안내 중 표시는 실제 paplay 실행 구간의 `speech.playing`을 사용한다.

Pi의 main 참조 저장소는 `/home/rp/ESW2026-main-reference`다. 이곳에서는 인증된
`git pull --ff-only origin main`을 사용할 수 있다. 실행 폴더 `/home/rp/ESW2026-integration`은
Git 저장소가 아니므로 그곳에서 pull하지 않는다. 새 main 변경은 별도 검토·연결 후 배포한다.

## 검증과 복구

전체 Python 테스트 132개 통과, CUDA/TensorRT 테스트 3개 제외.
Pi 별도 staging에서 Pi 앱·음성 테스트 14개 통과.
로컬 HTTP 브라우저 검증은 TTL, 경고 우선순위, CAN 미수신, 백엔드 정지·복귀,
데모 비활성화, 외부 요청 미발생과 1920×1080/1280×1024 화면을 확인했다.
실제 DDS domain 42에는 합성 값을 넣지 않았다.

```bash
PYTHONPATH=board_runtime/src .venv/bin/python scripts/check-hmi-cameras.py
```

Pi 복구 스크립트:
`/home/rp/codrive-backups/main-hmi-20261021.y6_5lfhc/rollback.sh`.
적용 직전 Pi 앱과 배포 manifest를 복구하고 서비스를 재시작한다.
Pi 실제 모니터 캡처와 브라우저 검증 결과는 `artifacts/reviews/main-hmi/`에 보관했다.

## 예상 경로 가이드 · 2026-10-27

전방 메인 영상에만 Pi driving_corridor의 bicycle 경로를 카메라 보정 좌표로 투영해 표시한다.
양쪽 경계·중심선·거리 눈금은 실제 위험 계산과 같은 조향/차량 폭/경로를 사용한다.
정차 시에는 조향 방향을 확인할 짧은 미리보기 경로를 표시한다. 실제 TTC는 주행/객체 운동으로 계산한다.
충돌 예상 대상 최대 3개에 bbox·남은 초를 표시하고, 추적된 횡단 보행자에는 이동 방향 화살표를 표시한다.
원본 영상·추론 모델·위험 조건은 바꾸지 않는다. 내부 카메라로 전환하거나 CAN/영상이 끊기면 가이드를 숨긴다.
Pi HDMI 출력 음량은 100%, 음소거 해제로 설정했다.
