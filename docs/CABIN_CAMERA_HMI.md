# 내부·외부 카메라 HMI 위치 전환

기본 화면은 큰 왼쪽 영역에 외부 카메라, 오른쪽 차량 상태 바의 아래에 내부 카메라다.
보조 영상 위 **⇄ 위치 전환** 버튼으로 두 영상을 바꾼다. 버튼은 터치·마우스와
키보드 Enter/Space를 지원한다. 선택은 해당 브라우저의 localStorage에 저장된다.
위치를 바꿔도 경고·TTS 표시는 큰 화면에 유지되며 각 카메라 이름도 함께 바뀐다.

## 영상 경로

Jetson 2의 기존 DMS가 읽는 프레임을 비동기 MJPEG 인코더에 함께 전달한다.
카메라를 두 번 열거나 두 번째 DMS 추론을 실행하지 않는다. 모델·클래스·판정·DDS 계약은
변경하지 않는다. 영상은 DDS 밖의 별도 HTTP 경로로만 보낸다.

- 실행: `scripts/run-cabin.sh` → `codrive.apps.dms` → `transport/mjpeg.py`.
- 설정: `config/integration/jetson-cabin.yaml`의 `hmi_stream`.
- 내부 영상: `http://192.168.50.22:8082/stream.mjpg`, 상태: 같은 서버의 `/health`.
- 기본 최대 폭 640px, 최대 10fps, JPEG quality 70. 인코딩 큐는 최신 프레임 한 개다.
- main `b93538ae36b4cab261de0ee07c5d9a3bd3c03c18`의 MJPEG 구현을 가져와
  Python 3.10 호환 주석·로깅을 정리했다.
- `--no-hmi-stream`으로 송출을 끌 수 있다. 포트 충돌이나 영상 전달 오류가 나도
  DMS 판정·발행은 계속한다.

Pi의 `scripts/run-pi.sh`는 기본 내부 영상 주소를 위 유선 주소로 지정한다.
다른 주소는 `CODRIVE_CABIN_CAMERA_STREAM` 환경 변수 또는 `--cabin-camera-stream`으로
설정한다. 환경 변수를 빈 문자열로 명시하면 내부 영상을 요청하지 않는다.
외부 영상 설정 `CODRIVE_FRONT_CAMERA_STREAM`/`--front-camera-stream`은 유지한다.
API의 `camera_preview.cabin_stream_url`로 전달한다.

두 카메라는 독립적으로 health·실제 디코딩을 확인한다. 1.5초 이상 오래된 프레임은
숨기고 해당 영상만 대기로 표시한다. 자동 재연결하며 위치 전환 자체는 URL을 바꾸지 않는다.
Pi 상태 연결이 끊기면 두 영상도 대기로 전환한다. 영상 연결을 DMS 검출 성공이나
외부 인지의 XGBoost 연결 완료로 해석하지 않는다.

## 배포와 검증

처음에는 Pi 전원을 끈 상태에서 구현·로컬 검증 후 Git에 올렸다.
2026-10-21 main 통합 후 세 보드의 main 체크아웃에 적용했다. 실제 내부·외부 영상 디코딩,
위치 전환, Pi 모니터 표시를 확인했다. 실행 위치와 복구는 [main 배포](MAIN_BOARD_MERGE.md)를 따른다.
기존 실행 폴더는 Git 저장소가 아니므로 그 안에서 pull하지 않는다. 별도 clone/checkout의
검증한 변경만 적용하고, Jetson 2의 별도 수정된 음향 파일은 보존해야 한다.
Jetson 2에는 DMS 앱·MJPEG 모듈·integration 설정을, Pi에는 HMI 앱·JS·HTML 어댑터·launcher를
함께 적용한 뒤 각각 `codrive-dms`, `codrive-pi` 서비스를 재시작한다.
Pi 브라우저 새로고침 후 `/health`의 증가하는 sequence와 두 실제 영상을 최종 확인한다.

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -ra
node --check src/codrive/apps/main_hmi_live.js
PYTHONPATH=board_runtime/src .venv/bin/python scripts/check-hmi-cameras.py
```

브라우저 검증에는 별도 Playwright/Chromium·numpy·OpenCV가 필요하다. 보드의 JetPack OpenCV를
일반 pip wheel로 교체하지 않는다. 실제 장치·DDS·TTS 없이 로컬 HTTP 시험 영상 두 개를 쓴다.
기본 위치, URL 유지, 위치 저장, 독립 만료·복구, 키보드 버튼, 경고 유지와
1920×1080 / 1280×1024 / 1366×768 / 1280×720 배치를 확인한다.
시험 캡처와 결과는 기본 `/tmp/codrive-hmi-cameras/`에 저장한다.


## 오탐 확인 화면

HMI MJPEG는 검출 박스·확률·디버그 패널 없는 원본 프레임만 전송한다.
HMI 스트림에 디버그 표시를 넣는 옵션은 제거했다.

Jetson에서만 `--preview`를 선택적으로 사용하면 동일 추론 프레임의 검출 박스,
확률, 휴대폰 PASS/below, 최종 행동 상태를 로컬 GUI로 확인할 수 있다.
Ubuntu two 계정의 GUI 로그인이 필요하며 기본 서비스에서는 preview를 켜지 않는다.
기존 DMS 서비스가 카메라를 사용 중이면 별도의 추론 프로세스를 중복 실행하지 말고
서비스 실행 인자에만 `--preview`를 임시 추가한다. 이때도 HMI 영상에는 박스가 없다.
