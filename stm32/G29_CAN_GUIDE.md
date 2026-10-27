> 경로 안내: 이 문서는 과거 작업 기록입니다. 파일은 현재 저장소의 `stm32/` 아래에 있으며, `E:\stm32` 대신 현재 체크아웃의 `stm32` 경로를 사용하세요. 최신 실행 순서는 [README.md](README.md)를 참고하세요.

> Latest hardware test: D14/PB9 TX -> CTX, D15/PB8 RX -> CRX. MSP and IOC updated, built and flashed. ECU reports RX=11, valid=11, IDs=4/4/3, speed=12.34, brake=1, steering=-45.6. Initial burst received 2/3; subsequent paced test received 9/9. Historical LEC=2 observed; sustained reliability remains to be checked. Older test-status entries below describe earlier stages.

# CODRIVE G29 → Classic CAN 단계별 확인

## 현재 확인 결과

- 지정한 main.c, .ioc, MSP, interrupt, keyboard_test.py 파일 모두 존재합니다.
- 기존 VehicleState: float speed_kph, uint8_t brake_pressed, float steering_deg, uint32_t last_update_ms. 구조체를 유지했습니다.
- 기존 CAN 수신은 FIFO0 폴링입니다. NVIC/IRQ 핸들러는 있지만 수신 알림 활성화나 수신 콜백은 없습니다. 중복 수신 경로를 추가하지 않았습니다.
- 기존 CAN ID는 0x100/0x101/0x102, 표준 ID, 데이터 프레임, DLC 8입니다. speed uint16 LE ×0.01, brake byte0, steering int16 LE ×0.1 파싱을 재사용했습니다. 수신 브레이크 값은 이제 0/1만 허용합니다.
- 기존 USART3: 115200, 8N1, PD8 TX / PD9 RX. 수신 인터럽트와 1/0/T(t) → PB0 LED 제어, UART 오류 카운터를 유지했습니다. keyboard_test.py는 수정하지 않았습니다.
- FDCAN1: PB8 (D15) RX / PB9 (D14) TX, AF9. MSP와 IRQ 파일은 수정하지 않았습니다.
- 기존 PLL1Q=129 MHz, prescaler=4, seg1=56, seg2=7이므로 기존 속도는 503906.25 bit/s였습니다. .ioc 표시도 503906이었습니다.
- 수정: PLLN=128, PLL1Q=128 MHz, prescaler=4, 64 TQ → 정확한 명목 500000 bit/s, sample point 57/64=89.0625%, SJW=1 TQ. 실제 발진기 오차/버스 파형은 측정하지 않았습니다.
- CPU 및 UART 클럭은 HSI 64 MHz 경로를 유지합니다. PLL에서 파생되는 미사용 클럭 표시도 .ioc에 맞춰 수정했습니다.
- FDCAN1 Normal Mode, Classic CAN. FIFO0 3개, 전체 표준 ID 마스크 필터를 유지합니다. 다른 ID는 상태를 갱신하지 않고 카운트합니다. 확장 ID와 remote는 기존 global filter에서 거부합니다.
- 더미 송신은 hfdcan1.Init.Mode가 INTERNAL_LOOPBACK일 때만 실행됩니다. 현재 Normal Mode에서는 실행되지 않습니다.

## USB 장비 식별

| 구분 | 실제 조회 결과 |
|---|---|
| NUCLEO | NUCLEO-H723ZG, ST-LINK V3J17M11 |
| ST-LINK USB | VID 0483 / PID 374E |
| UART 포트 | COM7, STMicroelectronics 드라이버 2.1.0.0 |
| USB-CAN 포트 | COM8 (재연결 시 바뀔 수 있음) |
| USB-CAN ID | VID 16D0 / PID 117E, serial 2062369C3945 |
| USB 장치 설명 | CANable2 b158aa7 github.com/normaldotcom/canable2.git |
| 실제 V 명령 응답 | 16e7497-dirty github.com/normaldotcom/canable2.git |
| Windows 드라이버 | Microsoft usbser, usbser.inf, 10.0.26100.9278 |
| 선택한 API | python-can 4.6.1 slcan + pyserial 3.5 |

USB 설명과 버전 명령 응답이 서로 다른 문자열임을 그대로 기록했습니다. CANable2 호환 펌웨어와 SLCAN 통신은 확인했지만, 제품 외장의 제조사/판매 모델 및 회로 리비전은 미확인입니다. 펌웨어를 바꾸거나 Zadig 드라이버를 설치하지 않았습니다.

공식 펌웨어 소스: https://github.com/normaldotcom/canable2-fw

python-can SLCAN 문서: https://python-can.readthedocs.io/en/stable/interfaces/slcan.html

송신 시 USB ID와 V 응답을 다시 확인하므로 COM7 ST-LINK를 CAN 장치로 잘못 열지 않습니다. S6=500 kbps, M0=Normal, A0=자동 재송신 비활성화를 사용합니다. 이 테스트에서는 ACK가 없을 때 과거 프레임의 재송신을 피합니다. FD 및 BRS는 꺼져 있습니다. USB 직렬 속도 115200은 CAN 속도 500000과 별개의 값입니다.

이 펌웨어는 SLCAN 명령별 ACK/NACK를 제공하지 않습니다. Python의 제출 성공은 CAN 버스 ACK나 STM32 수신을 증명하지 않습니다. can_*_rx_count를 따로 확인해야 합니다. _write 확장 명령을 사용하므로 requirements에 python-can 버전을 고정했습니다.

## 실제 수행한 검증 / 남은 검증

| 단계 | 상태 |
|---|---|
| 기존 소스 분석 | 완료 |
| ARM GCC 14.3 펌웨어 컴파일·링크 | 실시간 UART 출력 버전 성공, 경고 없음. text 44460 / data 100 / bss 2692 bytes |
| Python 오프라인 계약 테스트 | 9개 통과. 실제 장치 측정값이 아닌 합성 보정 fixture 사용 |
| USB-CAN 버전 조회 | COM8에서 실제 응답 수신 |
| USB-CAN 제출 | 12.34 km/h / brake=1 / -45.6° 세 프레임 제출, 종료 시 채널 닫음 |
| 실제 버스 송신·ACK | 아직 독립 확인하지 않음 |
| 최신 G29 연결 조회 | USB 허브 연결 후 Windows에서 G HUB G29 정상 인식 |
| G29 실제 축 번호·방향·중심·rest 보정 | 아직 측정하지 않음. g29_axes.json을 임의 생성하지 않았음 |
| 수정 펌웨어 다운로드 | 실시간 UART 출력 버전을 ST-LINK로 다운로드·검증·리셋 완료 |
| STM32 외부 CAN 수신·VehicleState | UART 실시간 보고 성공. 고정값 제출 후에도 RX=0/valid=0, 외부 CAN 수신은 아직 실패 |
| UART/LED 하드웨어 재시험 | 기존 코드 유지 및 빌드 확인, 동작 재시험은 남음 |
| CubeMX 재생성 | 수행하지 않음. .ioc 설정과 HAL 코드를 대조했으나 GUI 재계산 결과는 미검증 |

## 설치 및 단계 1: G29 원시값과 보정

Python 3.12에 필요한 패키지는 설치했습니다. 다른 환경은 다음으로 설치합니다.

```powershell
py -3.12 -m pip install -r E:\stm32\requirements_g29.txt
py -3.12 E:\stm32\g29_test.py
```

G HUB에 G29가 보이도록 연결하고 steering operating range를 900°로 설정합니다. raw indexed axes에서 조향·가속·브레이크를 하나씩 움직여 실제 번호를 확인합니다.

```powershell
py -3.12 E:\stm32\g29_test.py --calibrate
```

창을 클릭하고 안내된 위치에서 0.3초 이상 정지한 뒤 Enter를 누릅니다. 순서: 중심/페달 미입력 → 최대 좌 → 최대 우 → 양 페달 해제 → 가속만 최대 → 양 페달 해제 → 브레이크만 최대. 마지막에 측정 번호와 값을 확인하고 창에서 Enter로 저장합니다. Esc는 취소합니다. 기본 파일은 E:\stm32\g29_axes.json입니다.

보정은 가장 크게 움직인 축을 선택하고, 다른 축도 크게 움직이면 오류로 중단합니다. 번호가 애매하면 raw에서 확인한 실제 번호를 --steering-axis N --throttle-axis N --brake-axis N으로 지정하고 다시 보정합니다. 임의의 기본 번호는 없습니다. 공유/combined 페달 축은 거부하므로 G HUB에서 개별 축 설정을 확인해야 합니다.

조향은 실제 center와 좌/우 끝점을 각각 사용해 -450..+450°로 변환합니다. 페달은 측정한 rest와 pressed를 이용하므로 +1→-1 및 -1→+1 방향을 모두 지원합니다. 모든 결과는 범위를 제한합니다. 장치 GUID가 바뀌면 재보정을 요구합니다.

```powershell
py -3.12 E:\stm32\g29_test.py
```

중심≈0°, 양 페달 해제≈0%, 각각 최대 입력≈100%를 확인합니다. 실제 G29가 900°로 설정됐는지와 끝점까지 도달했는지는 사람이 확인해야 합니다. 장치 해제는 종료하며 자동으로 옛 입력을 재송신하지 않습니다.

## 단계 2: CAN 바이트 검증 (장비 없이 가능)

```powershell
py -3.12 E:\stm32\g29_can_sender.py --sample 12.34 1 -45.6
```

```text
STD 0x100 CLASSIC DLC=8 D2 04 00 00 00 00 00 00
STD 0x101 CLASSIC DLC=8 01 00 00 00 00 00 00 00
STD 0x102 CLASSIC DLC=8 38 FE 00 00 00 00 00 00
```

```powershell
py -3.12 E:\stm32\g29_can_sender.py --dry-run --virtual-speed-kph 12.34
```

세 프레임을 20 Hz로 생성합니다. 가속 입력을 속도로 취급하지 않습니다. 고정 가상 차속의 기본값은 0입니다. --virtual-ramp는 페달과 무관하게 시간만으로 0→60→0 km/h, 40초 주기를 생성합니다. throttle_pct는 로컬에 유지·표시하고 미정 CAN ID는 추가하지 않습니다. 브레이크는 5% 이상에서 ON, 2% 이하에서 OFF로 전환하는 히스테리시스를 사용합니다.

## 단계 3: 배선 및 USB-CAN 제출

| NUCLEO / 어댑터 | 연결 대상 |
|---|---|
| PB9 (D14) FDCAN1_TX | SN65HVD230 D / TXD |
| PB8 (D15) FDCAN1_RX | SN65HVD230 R / RXD |
| 3.3 V | SN65HVD230 VCC |
| GND | 트랜시버 GND 및 비절연 CAN 어댑터의 CAN GND |
| 트랜시버 CANH | 어댑터 CANH |
| 트랜시버 CANL | 어댑터 CANL |
| USB-CAN USB | PC USB |
| NUCLEO ST-LINK USB | PC USB, 디버깅/UART용 |

CAN 버스 양 끝에 120Ω씩 필요합니다. 모듈/어댑터 내장 저항 유무를 먼저 확인해 중복 설치하지 않습니다. 전원을 끄고 CANH-CANL 사이 약 60Ω인지 확인합니다. SN65HVD230 Rs가 standby로 고정돼 있지 않아야 합니다. 실제 배선·종단은 아직 직접 확인하지 않았습니다. 이 트랜시버에는 Classic CAN 500 kbps를 사용합니다.

TI 데이터시트: https://www.ti.com/lit/ds/symlink/sn65hvd230.pdf

먼저 1회 고정값만 보내는 독립 테스트를 사용합니다.

```powershell
py -3.12 E:\stm32\g29_can_sender.py --send --port COM8 --sample 12.34 1 -45.6
```

USB 제출만 성공해도 STM32가 내부 루프백 펌웨어라면 외부 메시지는 수신되지 않습니다. 다음 단계에서 수정 펌웨어로 수신을 확인합니다.

## 단계 4: 펌웨어 다운로드 및 STM32 수신

실시간 UART 출력 버전을 이미 ST-LINK로 다운로드·검증했습니다. 이후 코드를 바꾸면 CubeIDE에서 Build 후 Debug/Run으로 다운로드합니다. 산출물은 Debug\CODRIVE_ECU.elf입니다. 이전 보드 플래시 1 MB는 g29_backups\20261016_214232_live\flash_before.bin에 백업했습니다.

디버거에서 실행을 재개하고 다음을 봅니다.

- can_init_stage == 4
- can_kernel_clock_hz == 128000000
- can_nominal_bitrate == 500000
- hfdcan1.Init.Mode == FDCAN_MODE_NORMAL
- can_tx_count == 0 (이 코드의 더미 송신 없음)

위 1회 송신 명령을 재실행합니다. 수신 시 can_rx_count와 can_valid_rx_count가 3씩, can_speed_rx_count / can_brake_rx_count / can_steering_rx_count가 각각 1씩 증가해야 합니다. STM32가 디버거에 정지돼 있으면 시험하지 말고 Resume 상태에서 송신합니다.

오류 확인: can_rx_error_count(HAL 읽기 오류), can_invalid_frame_count(형식/브레이크 값 오류), can_unknown_id_count, can_fifo_lost_count, can_protocol_error_count, can_last_protocol_error, can_bus_off, can_bus_off_count, can_rx_error_counter, can_tx_error_counter, can_last_hal_error. FIFO lost와 protocol error는 관측 횟수이며 모든 개별 오류/유실 프레임의 정확한 합계는 아닙니다. bus-off 자동 복구는 구현하지 않았습니다.

## 단계 5: VehicleState와 G29 연동

고정값 송신 후 기대값:

```text
vehicle_state.speed_kph       ≈ 12.34
vehicle_state.brake_pressed   = 1
vehicle_state.steering_deg    ≈ -45.6
```

float 표시는 작은 이진 반올림 오차가 있을 수 있습니다. last_update_ms는 어떤 신호든 수신하면 갱신되므로 세 신호의 신선도를 함께 보장하지 않습니다. 각 can_speed_last_ms / can_brake_last_ms / can_steering_last_ms와 ID별 카운터를 따로 봅니다. 통신 중단 시 상태를 자동 0으로 초기화하는 기능은 추가하지 않았습니다.

고정값 수신 성공 후 보정된 G29로 송신합니다.

```powershell
py -3.12 E:\stm32\g29_can_sender.py --send --port COM8 --virtual-speed-kph 12.34
```

가속은 로컬 %만 변화하고 speed CAN 값은 고정입니다. 핸들은 steering, 브레이크는 임계값 기반 brake_pressed에 반영되어야 합니다. Ctrl+C나 창 닫기로 종료하면 CAN 채널을 닫습니다. 연결 오류 시 자동 재시도하지 않고 종료합니다.

UART 회귀 확인은 기존 E:\stm32\keyboard_test.py를 실행하고 COM7에서 1/0/T LED 제어 및 rx_count를 확인합니다. COM7은 CAN 송신 포트가 아닙니다.

## 파일 및 복구

실시간 확인은 터미널 두 개로 실행합니다. COM7은 다른 시리얼 프로그램에서 닫아 둡니다.

```powershell
# 터미널 1: STM32가 실제로 파싱한 VehicleState 및 ID별 수신 카운터
py -3.12 E:\stm32\g29_ecu_monitor.py --port COM7
# 터미널 2: G29 없이 고정 CAN 값 3개를 한 번 제출
py -3.12 E:\stm32\g29_can_sender.py --send --port COM8 --sample 12.34 1 -45.6
```

정상 수신 시 모니터에 speed=12.34, brake=1, steering=-45.6, RX/valid 증가가 보입니다. 한 번만 송신하면 1초 후 STALE로 바뀌는 것이 정상입니다. 세 신호가 모두 1초 이내 갱신된 경우만 LIVE입니다. 펌웨어는 UART TX 인터럽트로 200ms마다 보고하며 기존 LED 명령용 RX 인터럽트를 유지합니다. 모니터는 PC 예상값을 ECU 결과로 표시하지 않습니다.

G29 보정 완료 후 터미널 2에서 --send --port COM8 --virtual-speed-kph 12.34로 지속 송신할 수 있습니다. 송신 창의 PC decode ONLY는 로컬 인코딩 검증이며, ECU 수신 여부는 COM7 모니터에서 확인합니다. 현재 고정값 시험에서는 RX=0이므로 CANH/L, 공통 GND, 종단, PB8 (D15)/PB9 (D14)와 D/R 연결 확인이 필요합니다. 원인은 아직 확정하지 않았습니다.

실시간 기능 추가 직전 main.c와 sender 백업은 g29_backups\20261016_214232_live에 있습니다. telemetry_tx_count는 UART 제출 횟수, telemetry_skip_count는 busy/오류로 보고를 건너뛴 횟수입니다.

- 수정: CODRIVE_ECU\Core\Src\main.c, CODRIVE_ECU\CODRIVE_ECU.ioc
- 추가: g29_test.py, g29_can_sender.py, test_g29_can.py, requirements_g29.txt, 이 가이드, G29_FULL_CODE.md, g29_changes.diff
- 원본 백업: E:\stm32\g29_backups\20261016_212217\main.c 및 CODRIVE_ECU.ioc
- MSP, IRQ, keyboard_test.py는 그대로입니다.
- G29_FULL_CODE.md에는 변경/추가 소스 파일의 전체 내용을 생략 없이 담았습니다.

기존 프로젝트의 main.c는 생성 코드와 수동 코드가 섞여 있습니다. CubeMX 재생성 후에는 기존 UART 사용자 함수 및 CAN 함수/메인 루프가 보존되는지 반드시 diff로 확인해야 합니다. 이번 작업은 CubeMX 재생성 없이 직접 빌드했습니다.
