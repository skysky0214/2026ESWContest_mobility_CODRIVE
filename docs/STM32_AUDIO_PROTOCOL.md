# STM32 USART audio wire protocol v1

외부 마이크 PCM은 Jetson B의 USART로 전달한다. 기본 형식은 16 kHz, mono, signed PCM16,
big-endian **header**와 little-endian **PCM sample**이다. 권장 UART는 921600 baud, 8N1,
flow control 없음이다. 20 ms frame의 PCM payload는 640 bytes다.

| offset | size | field | value |
|---:|---:|---|---|
| 0 | 4 | magic | ASCII `CAUD` |
| 4 | 1 | version | `1` |
| 5 | 4 | sequence | uint32, big-endian |
| 9 | 8 | timestamp_us | uint64, big-endian, STM32 boot 기준 |
| 17 | 4 | sample_rate_hz | uint32, `16000` |
| 21 | 1 | channels | uint8, `1` |
| 22 | 4 | payload_length | uint32, big-endian |
| 26 | 4 | crc32 | uint32, big-endian |
| 30 | N | PCM | signed PCM16 little-endian |

CRC32는 `magic`부터 `payload_length`까지의 26-byte header와 PCM payload를 이어 붙인 값에
IEEE CRC-32를 적용한다. CRC field 자체는 계산에서 제외한다. 최대 payload는 65536 bytes지만
실시간 동작에서는 640 bytes로 고정한다.

수신기는 임의 chunk 경계, 앞부분 garbage, CRC 오류 이후 다음 `CAUD` magic을 찾아
재동기화한다. `sequence` gap은 UART drop으로 health에 기록하고, STM32 timestamp는 Jetson의
첫 수신 시각과 주기적으로 대응시켜 drift를 추정한다. 장치 간 융합에는 보정된 PTP 시각을 쓴다.

Python 기준 encoder/decoder는 `codrive.hardware.audio.stm32`에 있고 같은 구현을 이용한
fragmentation, CRC failure, resync 시험이 포함되어 있다. STM32 firmware에서는 packed C struct를
그대로 전송하지 말고 각 정수를 명시적으로 big-endian으로 써서 compiler padding을 피한다.
