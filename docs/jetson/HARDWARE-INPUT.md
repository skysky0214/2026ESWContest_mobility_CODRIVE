# 하드웨어 영상 입력 개선 — 2026-10-05

## 결과

동일 TensorRT FP16 엔진과 기존 차선·신호등·객체 후처리를 사용해 첫 300프레임을 프레임 생략 없이 처리했다. 기존 OpenCV CPU 디코딩/축소를 GStreamer nvv4l2decoder + nvvidconv 하드웨어 디코딩/축소로 대체했다. BGR 변환 후 1280×720으로 전달하고 네트워크 입력은 384×640이다.

| 항목 | 기존 입력 | 하드웨어 입력 |
|---|---:|---:|
| 전체 FPS | 10.30 | 25.84 |
| 평균 처리 ms | 97.1 | 38.7 |
| p95 처리 ms | 108.4 | 46.1 |
| p99 처리 ms | 135.1 | 64.5 |
| 입력 프레임 취득·축소 평균 ms | 39.7 | 2.1 |
| 인지·메시지 평균 ms | 57.4 | 36.6 |

처리량 약 2.51배. 66.7ms를 넘는 프레임 1.0%, 33.3ms를 넘는 프레임 85.3%. 평균 15 FPS 처리 예산은 충족하지만 고정 30 FPS는 미달이다.

GStreamer 입력은 디코딩과 추론이 비동기로 겹친다. appsink는 max-buffers=1, drop=false로 설정했다. 프레임 취득 2.1ms는 이미 준비된 프레임의 대기·복사 시간으로, 디코더 자체 지연이 2.1ms라는 뜻은 아니다. 내부 디코더 버퍼, 카메라 노출/캡처, 실제 DDS 네트워크, 화면/TTS 지연은 측정하지 않았다. 파일을 원본 FPS에 맞춰 재생하지 않은 처리량 시험이며 이 수치를 카메라 capture-to-HMI 지연으로 해석하면 안 된다.

## 결과 비교

동일 인덱스 300프레임의 평균 픽셀 절대 오차 1.48/255. 하드웨어 색 변환·축소 방식 차이로 영상이 완전히 동일하지 않다. 기존 탐지 1624건 중 1576건(97.0%)이 새 경로와 같은 클래스 및 IoU≥0.5로 매칭됐다. 매칭 박스 평균 IoU 0.984. 새 경로 탐지 총 1615건. 정답 라벨 없는 일치도 비교로, 정확도 보증은 아니다.

기존 테스트 55개 통과. 모델·차선 판정 로직은 수정하지 않았다. 한 프레임 프로파일에서 탐지 약 14ms, 차선 후처리 약 18ms였으며 OpenCV 스레드를 1/2개로 줄이면 차선 처리가 더 느려 6개를 유지했다. 다음 최적화 후보는 차선 ROI 처리이지만 판정 변화 검증이 필요하다.

## 실행

```bash
cd /home/one/ws/adas
source activate.sh
PYTHONPATH=src python tools/benchmarks/front_perception.py --model artifacts/models/vision/yolo11n.engine --decoder jetson --output test-results/benchmark-trt-hwdecode.json
```

`jetson_video.py`: Jetson H.264 MP4 전용 입력 클래스. NVIDIA GStreamer 플러그인과 시스템 PyGObject 필요. 이 장비에서 제공되는 기능을 사용하며 OpenCV 패키지는 교체하지 않았다. `--decoder opencv`로 기존 입력 경로 선택 가능.

`compare_decode.py`: 300프레임 픽셀 비교와 저장된 탐지 결과 일치도 계산.

PyTorch의 Orin 지원 경고는 여전히 남아 있다. 실시간 카메라는 연결되지 않아 검증하지 못했다.
