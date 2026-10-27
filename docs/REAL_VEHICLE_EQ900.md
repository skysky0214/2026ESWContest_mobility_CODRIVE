# EQ900 / CANable real vehicle acquisition mode

## Active profile

Reference: private repository `skskelwl/eq900-canable-monitor`, commit `fa477535338e82ba7f17a6e1847a3d10acaaec47`. The original source was checked out read-only under build/eq900-canable-monitor-reference. Source mappings, validation scope, and CC-BY-SA-4.0 attribution/license are kept in config/can/eq900-reference. No unverified generic Hyundai DBC signals are substituted.

The verified reference uses **CANable 2 SLCAN, 500 kbps, serial 115200 baud**, USB VID/PID 16d0:117e. This is **not listen-only**: normal CAN ACKs, three fixed diagnostic data-read requests, and transaction-bound ISO-TP flow control are transmitted. No arbitrary frames, diagnostic session changes, resets, DTC clearing, ECU writes, or actuation commands are exposed. Maximum total request rate is 10/s; one outstanding transaction, 400 ms timeout, no retries; ISO-TP payloads are bounded to 256 bytes and sequence-checked.

## Signals

Byte offsets are after removing positive response SID and echoed PID/DID, zero-based:

|Signal|Request / response IDs|Request|Payload decoding|
|---|---|---|---|
|Steering wheel angle|7D4 / 7DC|21 01|data[9:11], signed big-endian / 10 degrees|
|Brake switch|7A0 / 7A8|22 B0 08|data[6] bit 0x10|
|Speed|7E1 / 7E9|22 01 A0|data[6] km/h, converted to m/s|
|Relative accelerator|7E1 / 7E9|22 01 A0|data[8] * 100 / 255 percent|

The source compared brake, steering and relative accelerator on one stationary **2018 EQ900**. That is not validation of another car or moving speed. Brake is a binary switch; accelerator is relative input. RPM is available in the source but not part of the existing VehicleState/HMI contract. Turn signals, gear, yaw and INS are not decoded by this profile.

Reference updates are approximately 0.3 s per input; this is diagnostic polling, not a tens-of-ms broadcast feed. The Pi/XGBoost vehicle freshness budget is 750 ms for this mode, with individual request-start timestamps retained. Missing/stale speed never becomes valid CAN data. No response means CAN waiting / zero presentation, not a measured zero.

## Modes

```
scripts/select-integration-mode.sh real-vehicle
scripts/select-integration-mode.sh carla
```

The real mode override is separate from all CARLA files and services. It disables the simulator ECU receiver on Pi and rejects laptop scenario weather/daylight and G29 indicator injection. API weather/daylight resumes. The physical GMSL source uses its lens correction only; CARLA images, monitor transforms, Audi geometry and metric homography are excluded. Metric corridor is disabled pending actual installation calibration. HMI is labeled `실차 수신 시험 · 거리 보정 전`.

CARLA mode restores the original virtual camera / corridor / simulation environment profile, simulator ECU receiver and 500 ms front capture-age budget. Laptop CARLA programs and scenario files are retained.

## Connection and limits

At deployment the physical camera streamed correctly, but Pi had no CANable connected. The worker waits and retries device discovery every 2 seconds. Exactly one matching USB adapter is required. A narrowly matched udev rule grants its serial node to rp; no unrelated serial devices are changed. python-can 4.6.1 was installed in Pi's existing runtime venv.

Follow the reference hardware documentation: OBD CAN-H 6, CAN-L 14, signal GND 5; adapter TERM/120Ω off for vehicle attachment, normal BOOT/DFU state. Verify the actual adapter/vehicle wiring. First compare inputs while stationary in P with parking brake, including steering direction and true speed zero. No vehicle CAN response or moving test has been validated by this deployment.

`tools/configure_eq900_can.py` and hardware/socketcan.py belong to the earlier passive SocketCAN alternative; they are **not used** by this confirmed SLCAN diagnostic profile.

Validation: 16 protocol/guard/ISO-TP/decoding/fusion/isolation tests passed. Pi API confirmed real-vehicle, scenario_context null, source pi5.eq900_obd configured, waiting_device; physical front perception reports yolo11n.engine without carla_rgb_relay. Hardware signal reception remains pending CANable/car connection.


## Pedal-only warning, explicitly requested for real-car testing

In eq900-obd real-vehicle mode only: accelerator >=90%, fresh accelerator/brake query timestamps (<=750 ms), brake released, and >=300 ms sustained distinct pedal captures produce full_accelerator WARNING. Speed may be zero and INS is not required. Repeated UI ticks / identical pedal capture times cannot advance confirmation. Release, brake, missing/stale input clear the condition. It is not an unintended-acceleration or measured-acceleration claim. Female F1 cached voice says: 가속페달 입력이 큽니다. 가속페달을 확인하세요. CARLA acceleration rules retain their original speed-derived 4m/s² + 90% logic. Four dedicated pedal tests plus ten relaxation tests passed.
