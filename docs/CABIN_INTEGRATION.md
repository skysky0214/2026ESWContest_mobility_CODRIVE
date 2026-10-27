# Jetson 2 → Pi 내부 카메라 통합

2026-10-21 네트워크 전환은 [유선 DDS 안내](WIRED_DDS.md)를 따른다. 아래 네트워크 표와
검증 수치는 최초 Wi-Fi 통합 시점의 기록이다.

2026-10-19 기준, 외부 GMSL 카메라는 정격 전원 미확보로 제외한다.
STM32·CAN 시뮬레이터·INS는 이번 단계에서 연결하지 않는다.

## 코드 출처와 보존

- 노트북의 `a95f92a`를 기준으로 유지하고, Jetson 2 `b69ceb3`의 DMS 추정기·실행·미리보기·진단
  코드와 카메라별 클래스 매핑을 이식했다. 이후 음향 코드를 별도로 추가했으며
  [음향 통합 안내](AUDIO_INTEGRATION.md)에 출처·프로필·검증을 기록했다.
  Jetson 2의 전체 위험 엔진 재구성은 가져오지 않았다.
- DMS는 `board_runtime/src/codrive/sensor_input/driver/dms.py`, 실행은 `board_runtime/src/codrive/apps/dms.py`다.
- 기존 Jetson 2 작업 폴더와 미커밋 음향 작업은 변경하지 않는다.
- 원본 작업 폴더 백업은 노트북의 `/home/khj/2026ws/codrive-integration-backups/20261019/`에 있다.
- DMS 모델 SHA-256은 `5a48bc82c6509270278c62eba50dc2eb6c6a14a336f3e70423020d3f02440304`다.
- 데이터 계약은 [DATA_CONTRACT.md](DATA_CONTRACT.md)를 따른다.

## 실제 배치

| 장치 | 계정·IP | 통합 폴더 | Wi-Fi NIC |
|---|---|---|---|
| Jetson 2 | two@192.168.96.162 | /home/two/EWS/ESW2026-integration | wlP1p1s0 |
| Pi 5 | rp@192.168.99.21 | /home/rp/ESW2026-integration | wlan0 |
| 노트북 | 192.168.97.245 | 이 저장소 | wlo1 |

IP는 현재 DHCP 주소다. 바뀌면 `config/integration/cyclonedds-*.xml`의 peer 주소를 함께 갱신한다.
유선 고정 IP 설정은 아직 적용하지 않았다. 통합용 XML은 multicast 없이 static peer를 사용한다.

## 공통 시간 기준

재부팅 후 두 보드에서 NTP 동기화 표시가 켜져 있어도 약 43ms의 상대 시각 차이가 관측됐다.
Jetson은 지연이 큰 해외 DHCP NTP 소스, Pi는 다른 인터넷 소스를 사용하고 있었다.
현재는 두 보드의 systemd-timesyncd를 chrony 4.5로 대체했고, Jetson 2가 Pi의 시간을 따른다.
Pi의 UDP 123 응답은 두 Jetson의 현재 주소에 허용한다. 인터넷 시간은 Pi가 동기화한다.

- Pi 설정: `config/integration/chrony-pi.conf`
- Jetson 2 설정: `config/integration/chrony-jetson2.conf`
- 적용 위치: 각 보드의 `/etc/chrony/chrony.conf`
- 설치 직후 기본 설정 백업: `/etc/chrony/chrony.conf.codrive-before`
- 이전 timesyncd 설정·상태는 노트북 원본 백업 폴더의 `*-timesync-before.txt`에 보존했다.

```bash
chronyc tracking
chronyc sources -n
```

시각 차이는 SSH 요청·응답 중간 시각으로도 확인했다. 이 값은 왕복 지연의 절반만큼 불확실하며
하드웨어 PTP 계측을 대체하지 않는다. 배포 후의 시각 정렬과 실측 지연은 재검증해야 한다.
시간 설정 또는 강제 시각 보정을 변경할 때는 두 CODRIVE 서비스를 중지하고 보정 후 재시작한다.
Pi의 DHCP 주소가 바뀌면 DDS뿐 아니라 Jetson의 chrony server 주소도 함께 갱신한다.

이전 방식으로 되돌리려면 보존한 timesyncd 설정을 참고해 `systemd-timesyncd`를 다시 설치한다.
공통 시각 없이 측정한 음수 수신 지연은 유효한 성능 수치로 사용하지 않는다.

## 의존성

두 보드는 Ubuntu 24.04 / Python 3.12 / aarch64이며 시스템 Python을 사용한다.
Jetson의 기존 CUDA/PyTorch/Ultralytics/OpenCV 환경은 유지한다. OpenCV wheel은 추가 설치하지 않는다.

설치한 DDS 조합은 Ubuntu `cyclonedds-dev` 0.10.4와 Python `cyclonedds==0.10.4`다.
Ubuntu의 multiarch lib 경로를 인식시키기 위해 다음 사용자 prefix를 사용한다.

```bash
sudo apt-get install cyclonedds-dev
mkdir -p "$HOME/.local/opt/cyclonedds"
ln -s /usr/lib/aarch64-linux-gnu "$HOME/.local/opt/cyclonedds/lib"
ln -s /usr/include "$HOME/.local/opt/cyclonedds/include"
ln -s /usr/bin "$HOME/.local/opt/cyclonedds/bin"
CYCLONEDDS_HOME="$HOME/.local/opt/cyclonedds" python3 -m pip install --user --break-system-packages cyclonedds==0.10.4
```

Pi에는 python3-pip, espeak-ng, pulseaudio-utils도 설치했다. 한국어 합성은 로컬 espeak-ng의 ko 음성,
재생은 기존 PipeWire의 PulseAudio 호환 서버를 사용한다. 기본 sink가 auto_null이면 출력 실패로
표시한다. 실제 스피커에서 들리는지는 별도 청취 확인이 필요하다.

Ubuntu 제공 DDS C 라이브러리가 Tracing/OutputFile=stderr 설정에서 participant 생성 중 중단되어
통합 XML에는 Tracing을 넣지 않았다. 같은 증상의 [upstream 이슈](https://github.com/eclipse-cyclonedds/cyclonedds/issues/2043)가 있다.

## 실행·중지·로그

Jetson 2:

```bash
systemctl --user status codrive-dms
tail -f /home/two/EWS/ESW2026-integration/logs/dms.log
systemctl --user restart codrive-dms
systemctl --user stop codrive-dms
```

Pi:

```bash
systemctl --user status codrive-pi
tail -f /home/rp/ESW2026-integration/logs/pi.log
systemctl --user restart codrive-pi
systemctl --user stop codrive-pi
```

서비스 정의는 `deploy/systemd/user/`에 있고, 양쪽 user default.target에서 활성화했다.
장치의 journal 저장 설정과 관계없이 실행 로그를 각 통합 폴더의 logs/에 남긴다.
로그인 세션 또는 user manager의 시작 조건에 의존한다. 상시 운영용 boot/watchdog/linger
설계는 후속 운영 단계에서 다룬다.

수동 실행 시 user 서비스를 먼저 중지해 카메라와 HTTP 포트가 중복 점유되지 않도록 한다.

```bash
# Jetson 2 통합 폴더
bash scripts/run-cabin.sh --print-json --max-frames 30
# Pi 통합 폴더
bash scripts/run-pi.sh
```

HMI는 Pi에서 `http://127.0.0.1:8080`, 노트북에서 `http://192.168.99.21:8080`을 연다.
원본 영상 미리보기는 DDS HMI에 포함하지 않는다.

## 검증과 배포

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest -q
ruff check src tests scripts
python3 scripts/package-integration.py
python3 scripts/check-deployment.py
```

package-integration.py는 `/tmp/codrive-integration-20261019.tar.gz`와 SHA-256 manifest를 생성한다.
실행 파일 변경 후 다시 패키징하고 두 보드에 배포한 뒤 check-deployment.py로 동일성을 검사한다.
프로세스가 이미 실행 중이면 갱신된 소스를 적용하려면 서비스를 재시작해야 한다.

통신만 확인하려면 양쪽 통합 폴더에서 `PYTHONPATH=board_runtime/src`와 사용자 CYCLONEDDS_HOME을 설정한 뒤:

```bash
# Pi
python3 -m codrive.apps.dds_probe echo --dds-config config/integration/cyclonedds-pi.xml
# Jetson 2
python3 -m codrive.apps.dds_probe ping --dds-config config/integration/cyclonedds-jetson2.xml
```

합성 검증은 Pi 앱을 domain 44, port 8081로 별도 실행하고 Jetson에서 `scripts/cabin-fixture.py`를
실행한다. 실시간 domain 42의 화면·판정에 합성 값을 섞지 않는다.

지연은 USB read() 직후의 소프트웨어 시각부터 Pi 상태 갱신까지의 관측값이다. 카메라 노출부터
실제 디스플레이 frame swap·TTS dispatch까지의 200ms 종단 합격 증거로 사용하지 않는다.

## 이번 연결의 검증 결과

| 항목 | 2026-10-19 관측 결과 |
|---|---|
| 회귀 시험 | 노트북·Jetson 2·Pi에서 69개 통과 |
| 코드·모델 동일성 | 양쪽 배포 manifest 검사 통과, DMS 모델 SHA-256 일치 |
| 양방향 DDS | domain 43의 ping/echo 3회 성공, 정착 후 왕복 약 34ms |
| 실제 카메라 수신 | domain 42에서 1,313개 상태 수신 시점에 거부 0개 |
| 최근 수신 주기 | 약 12.3Hz. 목표 15Hz의 장시간 성능 검증은 미완료 |
| 소프트웨어 캡처→Pi 상태 갱신 | 최근 최대 1,000개 관측에서 p50 약 60ms, p95 약 80ms, p99 약 100ms |
| 시간 기준 | Jetson chrony가 Pi를 선택. SSH 상대 시각 추정 약 0.37ms, 왕복 지연에 따른 수 ms 불확실성 존재 |
| 발행자 중단 | driver TTL 만료·heartbeat 누락 표시, Pi 프로세스 유지 |
| 발행자 재시작 | 새 boot_id와 sequence 2부터 실시간 상태 복구, 거부 수 증가 없음 |
| Pi 재부팅 | 재접속·user 서비스 시작 뒤 DMS 수신 복구 확인 |
| INFO·TTS | domain 44의 합성 휴대폰 감지 → HMI → 한국어 합성·HDMI sink 재생 완료 |
| 화면 | Pi X11의 Firefox에 CODRIVE 창 생성 확인, HTTP GET/HEAD와 JSON API 검사 통과 |

실제 카메라 추론은 실행했지만 위 관측 구간에서는 운전자 행동이 검출되지 않았다.
따라서 학습 모델의 실제 운전자·휴대폰·졸음 인식 정확도 또는 눈 감김 측정을 검증했다고
주장하지 않는다. INFO 경고 경로의 긍정 사례는 분리한 합성 시험으로 확인했다.
HDMI sink의 재생 완료와 사람이 실제 스피커에서 들었다는 청취 확인도 구분한다.

원시 상태·시험 요약은 노트북 백업 폴더의 `live-final-*.json`, `isolated-hmi-*.json`,
`live-restart-summary.json`, `clock-offset*.json`, `*-time-and-service-final.txt`에 보존했다.


## 휴대폰 오탐 임계값 1차 조정 · 2026-10-21

사용자 요청으로 실제 integration 프로필의 `phone_min_confidence`만 0.45에서 0.65로 올렸다.
설정은 `config/integration/jetson-cabin.yaml`에 있으며 Jetson 2 DMS 재시작 후 적용한다.
YOLO 원시 후보 출력 임계값과 다른 클래스 기준은 바꾸지 않았다. 따라서 원시 박스가
표시되더라도 0.65 미만 phone 후보는 최종 phone_detected=true로 처리하지 않는다.

직전 동일 MJPEG 입력의 6개 진단 표본에서는 phone 후보 확률 0.258~0.382와 영상 가장자리의
박스가 관측됐다. 이 표본들은 기존 0.45 판정도 통과하지 않으므로, 이것만으로 이전 경고의
직접 원인을 확정하지 않는다. 운전자 구도에서 휴대폰 없음/사용/책상 위/손만 보임을
비교해 높은 확신도의 오탐과 실제 사용 누락을 확인한 뒤 추가 조정한다.
이 변경은 모델 정확도 검증 완료가 아닌 첫 조정 실험이다.
