# 유선 DDS 전환

2026-10-21, 세 보드가 ipTIME H8005-IGMP 스위치에 연결된 시연망을 대상으로 준비한다.
CARLA·CAN 작업은 별도로 진행한다. Wi-Fi 원격 접속과 인터넷 기본 경로는 유지한다.

## 주소와 설정

| 장치 | 유선 주소 | 인터페이스 |
|---|---|---|
| Pi 5 | 192.168.50.10/24 | eth0 |
| Jetson 1 | 192.168.50.21/24 | enP8p1s0 |
| Jetson 2 | 192.168.50.22/24 | enP8p1s0 |

`config/integration/wired-network.yaml`이 주소 계획이다. 보드의 NetworkManager 프로필은
`codrive-lan`이며 gateway와 DNS는 설정하지 않는다. 같은 /24의 보드 간 통신만 유선으로 간다.
기존 Wi-Fi 프로필과 기존 Wired connection 프로필을 삭제하지 않는다.

루트 권한으로 대상 보드에서 실행한다:

```bash
sudo bash scripts/configure-wired.sh enP8p1s0 192.168.50.21/24
```

위 예시는 Jetson 1이다. 장치별 NIC와 주소를 위 표와 대조해야 한다.
프로필은 부팅 시 자동 활성화되며, 스크립트는 IPv4 주소가 실제로 준비됐는지 확인한다.
DHCP를 제공하지 않는 스위치 전용망에서도 세 보드는 정적 주소로 통신한다.

## DDS와 시각

- 유선 설정: `config/integration/cyclonedds-{jetson1,jetson2,pi}-wired.xml`
- Wi-Fi 롤백 설정: 기존 `cyclonedds-{jetson1,jetson2,pi}.xml`
- 유선 XML은 해당 Ethernet NIC에만 바인딩하고 세 유선 주소만 static peer로 사용한다.
- domain은 실제 상태 42, 통신 시험 43, 합성 인지 검증 44를 유지한다.
- 시각 설정: `chrony-{jetson1,jetson2,pi}-wired.conf`
- 두 Jetson은 Pi의 유선 주소를 따른다. Pi의 NTP 허용 대상도 두 유선 주소로 제한한다.
- Pi의 인터넷 시각 동기화는 기존 Wi-Fi 인터넷 경로를 이용할 수 있다.

세 보드의 유선 SSH와 ping을 먼저 확보하고, 관련 CODRIVE 서비스를 중지한 뒤
시간 동기화와 실행 프로필을 바꾼다. 시각이 안정된 후 서비스를 재시작한다.
유선 IP만 설정한 상태는 DDS 전환 완료가 아니다.

실행 스크립트와 integration YAML은 유선 XML을 선택한다. Pi의 실행 인자도
`cyclonedds-pi-wired.xml`을 사용한다. Wi-Fi 원본 XML을 삭제하거나 덮어쓰지 않는다.

Jetson 2에는 `codrive-cabin-link.service`를 추가해 C922 연결 여부와 독립적으로
`jetson-cabin.link` heartbeat를 발행한다. 이 worker는 Pi와 Jetson 1의 heartbeat를 받으며
DriverState나 AudioEvent는 만들지 않는다. DMS·음향 서비스와 source_id를 공유하지 않는다.
HMI는 보드 통신과 센서 입력을 구분한다. C922 미연결 상태를 정상 센서 값으로 대체하지 않는다.

두 Jetson에서 재부팅 후 chrony가 disabled 상태였으므로 유선 설정 적용과 함께
`systemctl enable --now chrony`를 실행한다. 초기 시각 보정 동안 센서·통신 앱은 정지하고
Pi의 유선 기준 서버가 선택된 뒤 시작한다.

세 계정(one/two/rp)은 linger를 활성화해 SSH·데스크톱 로그인 없이 user manager를 시작한다.
유선 서비스의 ExecStartPre는 해당 고정 IP와 chrony 동기화를 기다린다. 부팅 직후 1970년
시각으로 ALSA sample-clock origin이 고정되는 문제를 피하기 위해 음향·DMS에도 같은 대기를 적용한다.
최대 180초 동안 준비되지 않으면 시작을 실패 처리하고 기존 Restart 정책으로 재시도한다.
Pi의 초기 절대 시각 확보는 기존 인터넷 NTP 경로에 의존하며, 완전 오프라인 초기 부팅의
정확한 절대 시각은 인터넷 기준이 필요하다. 두 Jetson은 [부팅 시각 복원](JETSON_CLOCK.md)을
추가해 마지막 동기화 시각을 먼저 복원한다. 이후 보드 간 DDS와 NTP는 유선 주소를 사용한다.

## 완료 판정

1. 각 보드에서 상대 주소의 route가 Ethernet NIC를 선택한다.
2. Pi↔각 Jetson과 두 Jetson 사이 domain 43 왕복이 성공한다.
3. domain 42에서 Pi가 front/cabin/audio heartbeat를 받고 기존 센서 결과를 수신한다.
4. chrony의 Jetson 기준 서버가 192.168.50.10으로 선택된다.
5. 유선 통신 프로세스 재시작 후 새 boot_id를 수용하고 미수신 경고가 복구된다.
6. 임시 주소 할당 프로세스를 사용했다면 제거하고도 세 정적 주소가 유지된다.

설정 변경 전 상태는 노트북의
`/home/khj/2026ws/codrive-integration-backups/20261021/`에 보존한다.
기존 원본 작업 폴더와 카메라 드라이버는 변경하지 않는다.

## 2026-10-21 검증 결과

| 항목 | 결과 |
|---|---|
| 실제 주소·경로 | 세 보드에서 고정 주소와 상대 보드에 대한 Ethernet 경로 확인 |
| 재부팅 후 주소 | 작업 중 재부팅 뒤 세 고정 주소와 codrive-lan 프로필 복구 확인 |
| Pi↔Jetson 1 | 분리된 domain 43 왕복 3회: 12.18 / 11.98 / 22.55ms |
| 두 Jetson | 분리된 domain 45 왕복 3회: 22.75 / 14.08 / 13.91ms |
| Pi↔Jetson 2 | 분리된 domain 46 왕복 3회: 11.87 / 12.05 / 12.01ms |
| Wi-Fi 분리 시험 | Jetson 2 Wi-Fi를 끈 동안 20회 관측 모두 두 보드 heartbeat 연결 유지, 수신 거부 0. 시험 후 Wi-Fi 복구 |
| 링크 프로세스 재시작 | 약 1.74초 뒤 미수신 표시, 새 boot_id로 복구. DMS·음향 입력 유지 |
| 최종 실제 입력 | Pi에서 front/link/DMS/audio 모두 연결. 상태 251개·DMS 311개·음향 230개 수신 시점 거부 0개 |
| 시간 기준 | 두 Jetson 모두 Pi 유선 주소 선택·Normal. 마지막 보고 offset 약 +0.019ms / -0.052ms이며 절대 정확도 보장은 아님 |
| 자동 실행 | 세 계정 Linger=yes, 관련 서비스 enabled·active, 최종 NRestarts=0 |
| 시작 전 준비 | 고정 주소·chrony 대기 ExecStartPre가 성공한 뒤 실제 서비스 시작 확인 |
| 배포 동일성 | 세 보드의 소스·설정·모델 manifest 검사 통과 |
| 회귀·문법 검사 | 노트북 86개 통과·TensorRT 1개 건너뜀, Pi 관련 12개 통과, Ruff·Bash·대시보드 JS 검사 통과 |

왕복 수치는 짧은 기능 시험이며 10ms polling과 응용 처리 시간을 포함한다. 순수 유선 지연이나
종단 인지 200ms 달성 증거로 사용하지 않는다. 전환 초기에는 C922가 없어 센서 프로세스가
실패했지만, 마지막 검증 시점에는 실제 DMS·음향 수신이 복구됐다. 인식 정확도 검증은 별도다.

검증 원본은 백업 폴더의 `wired-probes.json`, `wired-without-jetson2-wifi.json`,
`wired-link-restart.json`, `wired-live-final.json`, `*-wired-final.txt`에 있다.
배포 묶음은 `/tmp/codrive-integration-wired-20261021.tar.gz`다.
