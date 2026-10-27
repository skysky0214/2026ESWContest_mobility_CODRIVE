# GMSL 외부 카메라 → Pi HMI 영상

2026-10-21 사용자가 카메라 연결을 알린 뒤 Jetson 1에서 실제 캡처를 확인했다.
이전 외부 카메라 보류는 영상 표시에 한해 해제했다.

- 장치: `vi-output, ox08bc_gmsl 9-0040`, `/dev/video0`, 3840×2160 UYVY 30fps 모드.
- 실제 V4L2 8프레임 캡처 및 main TensorRT 파이프라인 30프레임 추론 성공.
  30번째 처리 루프 약 14.8fps/51.4ms이며 센서→HMI 지연 측정값은 아니다.
- 실행 소스: main `b93538ae36b4cab261de0ee07c5d9a3bd3c03c18`의 src/config/deploy를
  `/home/one/ws/ESW2026-main-front-20261021`에 분리 배치했다.
  원본 main Python 코드는 수정하지 않았다.
- Jetson 기존 `/home/one/ws/adas/.venv`와 해당 보드의 `yolo11n.engine`을 사용한다.
  엔진은 새 폴더 `artifacts/models/vision/yolo11n.engine`에 심볼릭 링크했다.
- 서비스: `codrive-front-camera`, unit 원본은
  `deploy/systemd/user/codrive-front-camera.service`. 부팅 자동 실행을 활성화했다.
- 카메라 입력은 GStreamer 하드웨어 축소 후 main 인지 파이프라인을 실행하고,
  MJPEG는 최대 960px 폭/12fps/quality 70으로 별도 전달한다.
- Jetson `http://192.168.50.21:8080/health`, `/stream.mjpg`를 Pi가 유선으로 사용한다.
  Pi의 `50-front-camera.conf` drop-in에서 `CODRIVE_FRONT_CAMERA_STREAM`을 설정한다.

`--dry-run`은 main 앱의 로컬 버스 옵션이다. 실제 카메라와 TensorRT 추론은 실행하지만
인지 결과를 DDS에 발행하지 않는다. 기존 `codrive-front-standby`가 통신 heartbeat를 유지한다.
따라서 **현재 연결 완료 범위는 실제 외부 영상 표시**이며 외부 인지 결과의 Pi 융합·XGBoost 입력은
아직 보류다. CAN과 음향 자동 경고 정책도 그대로 유지한다.

HMI는 영상 health에서 sequence>0, frame_age_ms<1500ms와 실제 이미지 디코딩을 확인한다.
오류·오래된 프레임은 영상을 숨기고 '외부 영상 대기'로 바꾸며 자동 재시도한다.
Pi의 실제 모니터 Firefox 전체 화면에서 영상과 '외부 영상 수신'을 캡처로 확인했다.
확인 자료: `artifacts/reviews/front-camera/`.

## 중지·복구

Jetson에서 `systemctl --user disable --now codrive-front-camera`.
Pi HMI 변경 복구는 다음 스크립트다.

```bash
/home/rp/codrive-backups/front-camera-20261021.tcznapkl/rollback.sh
```

카메라용 Pi drop-in을 제거하고 직전 앱·JS·배포 manifest를 복구해 재시작한다.
원본 보드 드라이버·전원·센서 레지스터 설정은 변경하지 않았다.
