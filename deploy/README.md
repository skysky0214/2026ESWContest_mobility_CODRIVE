# Board deployment entry points

| Board | Command | Device profile |
|---|---|---|
| `jetson-front` | `python -m codrive.apps.jetson_front` | `config/devices/jetson-front.yaml` |
| `jetson-cabin` DMS | `python -m codrive.apps.jetson_cabin.dms` | `config/devices/jetson-cabin.yaml` |
| `pi5` | `python -m codrive.apps.raspberry_pi` | `config/devices/pi5.yaml` |

The Pi process serves the browser HMI at `http://127.0.0.1:8090/` and maps fresh
`vehicle_state` data to speed, steering, brake, and accelerator displays.

The cabin audio process, PyQt/TTS output worker, and systemd units are not yet implemented. Keep
their future unit files grouped by board under `deploy/`; do not copy perception algorithms there.
