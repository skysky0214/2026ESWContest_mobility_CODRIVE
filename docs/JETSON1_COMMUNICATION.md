# Jetson 1 통신 전용 연결

2026-10-21 운영 프로필은 [유선 DDS](WIRED_DDS.md)로 전환한다. 아래는 10월 19일의
Wi-Fi 구성·검증 기록이며, 현재 주소와 실행 경로는 유선 안내를 우선한다.

2026-10-19, Jetson 1 `one@192.168.98.254`를 기존 Jetson 2·Pi DDS망에 추가한다.
원래 `/home/one/ws/ESW2026`와 카메라 bring-up 폴더는 유지하고
`/home/one/ws/ESW2026-integration`에서 통신 전용 프로세스를 실행한다.
외부 카메라는 전원 미확보로 열지 않고 ExternalPerception도 발행하지 않는다.

## 연결과 시각

| 장치 | 현재 주소 | DDS NIC | 역할 |
|---|---|---|---|
| Jetson 1 | 192.168.98.254 | wlP1p1s0 | camera-disabled heartbeat 및 peer 수신 |
| Jetson 2 | 192.168.96.162 | wlP1p1s0 | 실제 DMS·음향 입력 |
| Pi 5 | 192.168.99.21 | wlan0 | 융합·HMI 및 기준 시각 서버 |
| 노트북 | 192.168.97.245 | wlo1 | 개발·관측, 후속 CARLA 실행 예정 |

모든 통합용 `cyclonedds-*.xml`에 네 주소를 static peer로 명시한다. 현재는 같은 /22 Wi-Fi망이며
DHCP 주소 변경 시 함께 수정해야 한다. domain 42는 실제 상태, domain 43은 ping/echo 시험이다.
합성 인지 데이터는 실제 domain 42에 주입하지 않는다.

Jetson 1의 DDS 조합은 두 보드와 동일한 Ubuntu cyclonedds-dev 0.10.4 / Python cyclonedds 0.10.4다.
사용자 prefix는 `$HOME/.local/opt/cyclonedds`다. 카메라·GPU Python 환경을 새로 설치하지 않는다.

Jetson 1의 chrony는 Pi `192.168.99.21`을 따른다. 설정 원본은
`config/integration/chrony-jetson1.conf`, 실제 경로는 `/etc/chrony/chrony.conf`다.
Pi는 Jetson 1 주소를 설정 파일과 실행 중 chronyd의 allow 목록에 추가한다.
Pi의 chrony는 재시작하거나 강제로 시각을 보정하지 않는다.

Jetson 1의 이전 timesyncd 설정은 노트북 백업 폴더의 `jetson1-timesync-before.txt`,
설치 직후 기본 chrony 설정은 `/etc/chrony/chrony.conf.codrive-before`에 보존한다.
Pi의 이번 변경 전 설정은 `/etc/chrony/chrony.conf.codrive-before-jetson1`에 보존한다.

## 실행과 상태 의미

```bash
cd /home/one/ws/ESW2026-integration
systemctl --user status codrive-front-standby
tail -f logs/front-standby.log
chronyc sources -n
chronyc tracking
```

`codrive-front-standby.service`는 2Hz로 `jetson-front` NodeHealth를 보내고
Pi `pi5.hmi` 및 Jetson 2 `jetson-cabin`의 실제 heartbeat를 수신한다.
detail에는 `mode=communication_only`, `camera_enabled=false`, peer 연결 상태와 수신·거부 건수가 있다.
카메라 미사용이므로 worker 상태는 DEGRADED이며, 이것은 통신 두절을 뜻하지 않는다.
Pi와 standby 프로세스는 각각의 수신 사실을 기록하므로 단방향 송신만 성공한 상태와 구분한다.

Pi HMI의 `/api/state.nodes`는 worker 상태와 독립적으로 heartbeat의 최신성·연결 여부를 제공한다.
마지막 상태 수신 후 1.5초가 지나면 `connected=false`다. 카메라는 계속 입력 제외로 표시한다.
Pi 자체 heartbeat도 로그 주기와 분리해 2Hz로 발행한다.

서비스는 user default.target에서 활성화하며 로그인·user manager 시작 조건에 의존한다.
상시 부팅 운영과 watchdog은 별도 운영 단계다. 카메라 드라이버·설정·모델은 이번에 변경하지 않는다.

## 검증

- 세 보드의 소스·설정·모델 SHA-256을 deployment manifest로 비교한다.
- domain 43에서 Pi↔Jetson 1, Jetson 2↔Jetson 1의 ping/echo 왕복을 확인한다.
- domain 42에서 Pi의 Jetson 1 수신과 Jetson 1의 Pi·Jetson 2 수신을 확인한다.
- standby 중지 후 Pi의 heartbeat 누락 표시, 재시작 후 새 boot_id 수용을 확인한다.
- 기존 DMS·음향 수신과 HMI가 계속 동작하는지 확인한다.

이번 연결은 외부 카메라 인지와 CARLA 연결의 완료를 뜻하지 않는다. 다음 단계는 노트북에서
CARLA를 준비하고 CAN 시뮬레이터 조작·CARLA 실제 속도·Pi VehicleState 연결을 구성하는 일이다.

## 2026-10-19 검증 결과

| 항목 | 결과 |
|---|---|
| 회귀 시험 | 노트북 86개 통과·TensorRT 1개 건너뜀, Pi의 health/standby/HMI 관련 12개 통과 |
| Pi↔Jetson 1 | domain 43 왕복 3회 성공, 52.89 / 22.63 / 22.37ms |
| Jetson 2↔Jetson 1 | domain 43 왕복 3회 성공, 64.14 / 25.53 / 25.61ms |
| 실시간 상태 | Pi에서 front/cabin/audio 모두 연결, Jetson 1에서 Pi·Jetson 2 수신 확인 |
| Jetson 1 시간 | chrony가 Pi를 선택, 동기화 상태 Normal 확인. 순간 보정량은 약 0.7ms였으며 장시간 상한 보장은 아님 |
| 통신 프로세스 중지 | Pi에서 마지막 수신 약 1.72초 후 connected=false와 heartbeat_missing 표시 |
| 재시작 | 새 boot_id로 복구, 수신 거부 증가 없음 |
| 기존 입력 | 최종 관측에서 DMS 1,563개·음향 1,066개 수신, 전체 수신 거부 0개 |
| 카메라 | 미연결 상태 유지, ExternalPerception 발행 없음 |

원시 검증 상태는 노트북 백업 폴더의 `jetson1-restart-validation.json`,
`three-board-live-final.json`, `*-communication-final.txt`에 보존했다.
왕복 수치는 소수의 통신 확인 표본이며 종단 인지 지연이나 장시간 성능을 입증하지 않는다.
