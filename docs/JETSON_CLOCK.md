# Jetson 부팅 시각 복원

두 Jetson의 2026-10-21 부팅 로그에서 PMIC `nvvrs-pseq-rtc`가 RTC0으로 등록되며
시스템 시각을 1970년으로 설정했다. 현재 RTC0의 `hctosys=1`이고,
RTC1(`tegra_rtc`)은 전원 인가 후 경과 시간만 표시했다. RTC1로 전환하지 않는다.
chrony는 이후 Pi `192.168.50.10`을 선택해 현재 시각으로 보정했다.

## 적용

유선 Pi NTP가 정상인 대상 Jetson의 integration 폴더에서 실행한다.

```bash
sudo bash scripts/install-jetson-clock.sh
```

- `codrive-clock-restore.service`: local-fs와 udev-trigger 이후, sysinit·chrony 이전에 실행한다.
  RTC0 장치가 생길 때까지 최대 30초 기다린다. PMIC 드라이버의 부팅 시각 설정이
  끝난 다음 저장된 시각보다 시스템 시각이 과거일 때 앞으로 복원한다.
  RTC가 허용 범위(2026년~2100년 미만) 밖의 비정상 미래 값을 반환할 때도 저장 시각으로 복원한다.
  범위 내의 더 최신 RTC 시각은 되돌리지 않는다.
- `codrive-clock-save.timer`: 부팅 후 45초부터 매 60초 저장을 시도한다.
  Pi 유선 NTP에서 최근 120초 이내 측정, Normal, 잔여 보정 5ms 이하일 때만 저장한다.
  디스크에 원자적으로 교체·fsync하고 RTC0에 UTC로 기록한다. RTC1은 변경하지 않는다.
- 상태 파일: `/var/lib/codrive-clock/last-sync`, root 소유.
- 설치 파일: `/usr/local/libexec/codrive-clock.py`, `/etc/systemd/system/codrive-clock-*`.
- 재설치 전 기존 파일: `/var/backups/codrive-clock-<설치시각>/`.

복원 시각은 마지막으로 동기화한 시각의 하한이다. 저장 시각을 정확한 현재 시각이나
NTP 동기화 완료로 간주하지 않는다. 기존 `wait-wired-clock.sh`를 유지해 실제 NTP 동기화
이후에 통신·DMS·음향 worker를 시작한다. chrony의 기존 Pi 유선 기준 설정도 유지한다.

## 한계와 확인

전원 차단 중 경과 시간까지 RTC가 유지하려면 정상적인 RTC 백업 전원이 필요하다.
이번 소프트웨어 처리는 1970년으로 사용자 서비스가 시작되는 문제를 방지하고,
Pi가 준비되면 현재 시각으로 자동 보정한다. 백업 배터리의 장착·정상 여부는 원격으로
확정하지 않았다. 완전 오프라인에서 정확한 절대 시각을 새로 얻는 기능은 아니다.

```bash
date -Is
chronyc tracking
sudo journalctl -b -u codrive-clock-restore -u chrony -o short-monotonic
systemctl status codrive-clock-save.timer
sudo cat /var/lib/codrive-clock/last-sync
```

참고: [NVIDIA carrier board 사양의 RTC 백업 연결 J3](https://developer.nvidia.com/downloads/assets/embedded/secure/jetson/orin_nano/docs/jetson_orin_nano_devkit_carrier_board_specification_sp.pdf),
[chronyc tracking 형식과 의미](https://chrony-project.org/doc/4.5/chronyc.html).

## 2026-10-21 적용·검증 결과

두 Jetson에 설치하고 restore 서비스·save timer·chrony를 모두 enable했다.
RTC0에 과거 시각(2020년 로컬)을 넣고 각 보드를 재부팅했다. 최종 부팅 로그에서
Jetson 1은 monotonic 8.91초, Jetson 2는 8.71초에 저장 시각 복원이 완료됐고,
두 경우 모두 chrony 시작보다 먼저였다. Jetson 2는 Pi 기준 시각 준비가 늦어진 동안에도
2026년 저장 시각을 유지했고, 저장 timer는 미동기화 시각으로 체크포인트를 덮어쓰지 않았다.
실제 NTP 준비 후 세 user worker가 자동으로 시작됐다.

최종 두 보드 모두 Pi 유선 주소 선택·Leap Normal, 관련 system unit 실패 0개를 확인했다.
Pi HMI의 front/link/DMS/audio 모두 연결됐고, 상태 776개·음향 678개·DMS 984개 수신 시점에
거부 0개였다. 신규 시각 복원 테스트 17개가 통과했다. 첫 RTC 초기값 주입 시험에서는
드라이버가 비정상 미래 시각을 반환해, 이 범위를 저장 시각으로 복원하는 검사를 추가했다.

원본 로그는 노트북의
`/home/khj/2026ws/codrive-integration-backups/20261021/clock/`에 있다.
검증은 RTC 과거값 주입과 소프트웨어 재부팅으로 했으며, 배터리 장착 상태나 실제 전원 완전
차단 후 RTC 유지 여부를 확인한 시험은 아니다. 카메라 구성·드라이버는 변경하지 않았다.
