# 레이턴시 계측

소스 기준: ESW2026-latest. 실제 카메라 입력을 사용한다. CARLA→Jetson HTTP 영상 전송, BBOX 디버그·오버레이, 상세 프레임 로그·음향 진단, 시나리오 기록기의 10Hz Pi 조회를 끈다. 정상 HMI 영상과 센서·CAN·판단 경로는 유지한다.

`tools/latency/run_session.py`는 OFF/ON 비교와 별도 본 측정을 수행하고 끝나면 계측을 비활성화한다. 보드 서비스 재시작과 동일 워밍업이 포함된다. 실행 전 카메라 구도와 장면을 고정한다. script의 HOSTS는 현재 보드 주소·인증된 SSH control socket을 사용한다. 비밀번호를 파일에 저장하지 않는다.

계측 활성화 환경변수:
- `CODRIVE_LATENCY_FILE`: 해당 보드의 로컬 JSONL 경로
- `CODRIVE_LATENCY_SESSION`: 세션 ID
- `CODRIVE_LATENCY_EVERY`: 상세 처리 루프 표본 간격. Pi 기본 측정 10회 중 1회, edge는 매 프레임/윈도우

보드 내부는 perf_counter_ns, 보드 간은 프레임 ID·동기화 시각으로 대응한다. 추가 GPU fence는 사용하지 않는다. 기록은 2048개 제한 큐에 put_nowait로 넣고 별도 스레드가 최대 1초 단위로 저장한다. 큐가 차면 계측 기록을 버리고 본 처리를 막지 않는다. 누락 카운터를 결과에 기록한다.

결과 생성:

```bash
python3 tools/latency/report.py artifacts/latency/SESSION
python3 tools/latency/end_to_end.py artifacts/latency/SESSION
python3 tools/latency/overhead.py artifacts/latency/SESSION
python3 tools/latency/clock_bounds.py artifacts/latency/SESSION/clock-bounds.json
python3 tools/latency/summarize.py artifacts/latency/SESSION
```

전체·중분류·끝단 초/%는 같은 경로의 같은 표본 집합을 분모로 사용한다. 중첩 호출은 자식 시간을 빼서 중복 합산하지 않는다. 캐시로 실행하지 않은 분기는 그 프레임의 실제 작업량 0으로 계산하지만, 보정·타임스탬프·이벤트가 없어 측정하지 못한 항목은 별도 미측정 목록에 둔다. 병렬 경로와 분위수를 더해 전체 지연을 만들지 않는다.

HMI 끝점은 localhost 브라우저의 두 RAF 이후 기회이며 물리 화면 발광 시점이 아니다. 실제 위험 경고 원인과의 인과관계는 프레임이 상태에 포함됐다는 사실만으로 확정하지 않는다. 실제 음성 이벤트가 있으면 준비·대기·paplay 호출을 기록하며 실제 소리 발생은 별도 장비가 필요하다. 현재 F1 음성은 사전 생성 WAV 캐시를 재생한다.

센서 노출/전송/입력 버퍼, 송신 시각이 없는 CAN bus 전체, 유효한 실물 카메라 거리 보정이 없는 TTC, 실제 경고가 없는 TTS는 숫자를 만들지 않는다. DMS 확인 대기·음향 분석 윈도우도 계산 시간과 구분한다.

시나리오 제어를 유지하면서 Pi 조회를 끄려면 `run-carla-scenario.sh --no-pi-recorder`를 사용한다. 해당 옵션 없이 실행 중인 시나리오 기록기나 CARLA relay/debug가 있으면 계측 실행기는 시작을 거부한다.
