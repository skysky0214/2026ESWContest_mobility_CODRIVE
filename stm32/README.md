# ESW2026 — CODRIVE ECU / G29 CAN 실험

Logitech G29의 조향·페달 입력을 PC에서 읽고, Classic CAN으로 STM32H723 ECU에 전달한 뒤 UART로 실제 수신 상태를 확인하는 프로젝트입니다.

## 구성

| 경로 | 역할 |
| --- | --- |
| `CODRIVE_ECU/` | STM32CubeIDE 펌웨어 프로젝트. STM32H723ZGT6, UART LED 제어, CAN 수신 및 VehicleState 보고 |
| `CODRIVE_ECU/Core/Src/main.c` | CAN 초기화·프레임 파싱·상태/오류 카운터·UART 텔레메트리 |
| `CODRIVE_ECU/Core/Inc/`, `Core/Startup/` | 헤더 및 시작 코드 |
| `CODRIVE_ECU/Drivers/` | STM32 HAL 및 CMSIS 드라이버 |
| `CODRIVE_ECU/CODRIVE_ECU.ioc` | CubeMX 핀·클럭·주변장치 설정 |
| `CODRIVE_ECU/.project`, `.cproject`, `.mxproject` | IDE 프로젝트 및 빌드 설정 |
| `CODRIVE_ECU/STM32H723ZGTX_*.ld` | Flash/RAM 링커 스크립트 |
| `CODRIVE_ECU/CODRIVE_ECU Debug.launch` | 기존 디버그 실행 설정. PC별 경로 확인 필요 |
| `g29_test.py` | G29 원시 입력 모니터 및 실측 보정 |
| `g29_axes.json` | 기존 장치의 축 번호·끝점·GUID 보정값. 다른 환경에서는 다시 보정 |
| `g29_can_sender.py` | CAN 인코딩, 송신 없는 미리보기, CANable2 SLCAN 실제 송신 |
| `g29_ecu_monitor.py` | ST-LINK UART로 ECU의 실제 수신값·신호별 수신 횟수·오류 표시 |
| `keyboard_test.py` | UART로 `1`/`0`/`T`를 전송해 PB0 LED 제어. `CODRIVE_ECU/`에도 같은 파일 존재 |
| `test_g29_can.py` | 장비 없이 실행하는 인코딩·보정·브레이크 히스테리시스·텔레메트리 테스트 |
| `requirements_g29.txt` | pygame 2.6.1, python-can 4.6.1, pyserial 3.5 |
| [G29_CAN_GUIDE.md](G29_CAN_GUIDE.md) | 기존 상세 절차와 당시 장비 확인 기록 |
| [G29_FULL_CODE.md](G29_FULL_CODE.md) | 기존 소스 스냅샷 문서. 현재 동작 확인은 실제 소스 기준 |
| `g29_changes.diff` | 과거 변경 내용 패치 기록 |
| `CODRIVE_ECU/Debug/`, `__pycache__/` | 기존 빌드 결과·Python 캐시. Git 업로드 제외 |
| `g29_backups/` | 현재 비어 있는 백업 폴더. 기존 가이드에 언급된 백업 파일은 이 사본에 없음 |

## 준비 환경

- PC: 기존 실장비 기록은 Windows, Python 3.12, Logitech G HUB 기반입니다.
- STM32CubeIDE 및 STM32H723ZG 보드/ST-LINK. `.ioc`의 펌웨어 패키지는 STM32Cube FW_H7 V1.13.0입니다.
- G29 휠·페달, CANable2 호환 SLCAN 어댑터, 3.3V CAN 트랜시버(기존 가이드: SN65HVD230).
- 기존 포트 예시는 ST-LINK `COM7`, USB-CAN `COM8`입니다. 실제 장치 관리자에서 확인하세요.

아래 명령은 저장소의 `stm32/` 폴더에서 실행합니다. 먼저 저장소 루트에서 `cd stm32`로 이동하세요. Windows PowerShell 예시:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements_g29.txt
```

Linux에서는 `python3 -m venv .venv`, `.venv/bin/python -m pip install -r requirements_g29.txt`를 사용하고 아래 Python 실행 경로를 `.venv/bin/python`으로 바꿉니다. 시리얼 포트는 실제 `/dev/ttyACM*` 등으로 지정합니다. Linux의 실장비 동작은 이번 작업에서 확인하지 않았으며 G29 보정도 다시 해야 합니다.

## 1. 장비 없이 코드 확인

```powershell
.\.venv\Scripts\python.exe -m unittest -v test_g29_can
.\.venv\Scripts\python.exe g29_can_sender.py --sample 12.34 1 -45.6
```

`--sample`은 G29와 보정 파일 없이 실행되며 `--send`가 없으면 실제 송신하지 않습니다. 기대 프레임:

```text
STD 0x100 CLASSIC DLC=8 D2 04 00 00 00 00 00 00
STD 0x101 CLASSIC DLC=8 01 00 00 00 00 00 00 00
STD 0x102 CLASSIC DLC=8 38 FE 00 00 00 00 00 00
```

## 2. ECU 빌드 및 다운로드

1. STM32CubeIDE에서 `File > Import > General > Existing Projects into Workspace`로 `CODRIVE_ECU`를 선택합니다.
2. Debug 구성을 선택하고 `Project > Build Project`로 빌드합니다. 산출물은 `CODRIVE_ECU/Debug/CODRIVE_ECU.elf`입니다.
3. ST-LINK로 보드를 연결하고 Debug/Run으로 다운로드합니다. 디버거가 멈춰 있으면 Resume으로 실행합니다.
4. 기존 `.launch`에 남은 PC별 경로가 맞지 않으면 현재 프로젝트와 ELF를 선택해 디버그 구성을 새로 만듭니다.

펌웨어는 FDCAN1 Normal Mode, Classic CAN 500 kbit/s입니다. CubeMX 재생성 시 수동 코드가 보존되는지 diff를 확인해야 합니다. `Debug/`는 재생성되는 폴더이므로 기존 ELF 대신 현재 소스로 빌드하세요.

## 3. 배선

전원을 끄고 연결합니다.

| STM32 / 트랜시버 | 연결 대상 |
| --- | --- |
| PB9 (D14), FDCAN1_TX | 트랜시버 D/TXD |
| PB8 (D15), FDCAN1_RX | 트랜시버 R/RXD |
| 3.3V | SN65HVD230 VCC |
| GND | 트랜시버 GND 및 비절연 어댑터의 CAN GND |
| 트랜시버 CANH / CANL | 어댑터 CANH / CANL |
| USART3 PD8 TX / PD9 RX | 기존 보드 ST-LINK VCP 경로 확인, 115200 8N1 |

CAN 버스 양 끝의 120Ω 종단을 확인합니다. 전원 OFF 상태에서 CANH-CANL 사이 약 60Ω가 기준이며 내장 종단과 중복하지 않습니다. 트랜시버 Rs가 standby로 고정되지 않았는지도 확인합니다.

## 4. G29 없이 실제 CAN 수신 확인

포트 목록 확인:

```powershell
.\.venv\Scripts\python.exe -m serial.tools.list_ports -v
```

터미널 1 — ECU 모니터:

```powershell
.\.venv\Scripts\python.exe g29_ecu_monitor.py --port COM7
```

터미널 2 — 고정값 3프레임 한 번 송신:

```powershell
.\.venv\Scripts\python.exe g29_can_sender.py --send --port COM8 --sample 12.34 1 -45.6
```

모니터에서 `speed=12.34`, `brake=1`, `steering=-45.6`, `RX`/`valid` 및 ID별 카운터 증가를 확인합니다. 1회 송신 후 1초가 지나면 `STALE`로 바뀌는 것은 정상입니다. 세 신호가 모두 1초 이내 갱신돼야 `LIVE`입니다.

송신기는 VID/PID `16D0:117E`와 `normaldotcom/canable2`가 포함된 버전 응답을 확인합니다. 모니터는 ST-LINK VID/PID `0483:374E`를 확인합니다. 다른 어댑터는 포트 지정만으로 지원되지 않습니다. UART 모니터와 `keyboard_test.py`는 같은 포트를 동시에 열지 마세요.

## 5. G29 입력 확인 및 보정

G HUB에서 G29를 인식시키고 조향 범위를 900°로 설정합니다.

```powershell
.\.venv\Scripts\python.exe g29_test.py
.\.venv\Scripts\python.exe g29_test.py --calibrate
```

보정 창을 클릭한 뒤 안내에 따라 중심, 최대 좌/우, 페달 해제, 가속 최대, 브레이크 최대 위치를 유지하고 Enter를 누릅니다. 마지막 Enter로 `g29_axes.json`을 저장하며 Esc로 취소합니다. 기존 보정 파일은 변경되므로 필요하면 먼저 복사하세요. 축이 모호하면 원시 입력으로 확인한 번호를 `--steering-axis N --throttle-axis N --brake-axis N`으로 지정합니다. 공유 페달 축은 지원하지 않습니다.

## 6. G29 미리보기 및 지속 송신

```powershell
# G29는 연결하지만 CAN은 송신하지 않음
.\.venv\Scripts\python.exe g29_can_sender.py --dry-run --virtual-speed-kph 12.34 --seconds 10

# 실제 CAN 송신: ECU 모니터도 별도 터미널에서 실행
.\.venv\Scripts\python.exe g29_can_sender.py --send --port COM8 --virtual-speed-kph 12.34
```

기본 주기는 초당 20회이며 매회 3프레임을 생성합니다. `--rate-hz`로 변경하고 `--seconds`로 제한합니다. 가속 페달은 로컬 백분율 표시만 하며 차속으로 변환하지 않습니다. 차속 기본값은 0이며 `--virtual-ramp`는 시간에 따라 40초 주기로 0→60→0 km/h를 만듭니다. 브레이크는 5% 이상 ON, 2% 이하 OFF입니다. Ctrl+C 또는 창 닫기로 종료합니다.

## 7. UART LED 확인

루트 `keyboard_test.py`의 `PORT = "COM7"`을 실제 ST-LINK 포트로 바꾸고 ECU 모니터를 종료한 뒤 실행합니다.

```powershell
.\.venv\Scripts\python.exe keyboard_test.py
```

`1`은 PB0 LED ON, `0`은 OFF, `T`는 토글, `Q`는 종료입니다.

## CAN 데이터 규약

표준 11-bit ID, Classic CAN, DLC 8, CAN FD/BRS 비활성화입니다. 사용하지 않는 바이트는 0입니다.

| ID | 데이터 | 단위 / 범위 |
| --- | --- | --- |
| `0x100` | byte 0–1: uint16, little-endian | 원시값 × 0.01 km/h; 송신 범위 0–655.35 |
| `0x101` | byte 0: 0 또는 1 | 브레이크 해제 / 작동 |
| `0x102` | byte 0–1: int16, little-endian | 원시값 × 0.1°; 송신 범위 -450–450° |

## 확인 상태와 문제 해결

- 2026-10-21 이번 정리에서 기존 오프라인 단위 테스트 9개가 모두 통과했습니다. 하드웨어 송신·펌웨어 빌드/다운로드는 이번 환경에서 수행하지 않았습니다.
- 기존 2026-10-16 가이드에는 어댑터 제출과 UART 보고는 확인했지만 외부 CAN 수신은 `RX=0/valid=0`으로 미해결이라고 기록되어 있습니다. 현재 소스만으로 실제 수신 성공을 보장할 수 없습니다.
- `Submitted`와 `PC decode`는 어댑터 제출/PC 인코딩 결과입니다. 수신 성공은 ECU 모니터의 카운터로 판단합니다.
- `NO ECU TELEMETRY`: 현재 펌웨어 다운로드 여부, Resume 상태, 포트 및 다른 프로그램의 포트 점유를 확인합니다.
- `RX=0`: CANH/L, 공통 GND, 종단, 트랜시버 D/R, 500 kbit/s, Normal Mode를 확인합니다.
- 보정 GUID 불일치나 페달 축 오류: G HUB/드라이버와 개별 페달 설정을 확인하고 다시 보정합니다.
- 펌웨어는 통신 중단 시 VehicleState를 자동으로 0으로 초기화하지 않습니다. 수신값과 함께 신호별 갱신 시각을 확인하세요.
- 기존 문서의 `E:\stm32`는 과거 PC 경로입니다. 현재 체크아웃의 `stm32/` 경로로 바꿔 사용하세요.
