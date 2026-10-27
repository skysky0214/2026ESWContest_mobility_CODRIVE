Signal formulas in codrive/eq900_obd.py are adapted from:
- skskelwl/eq900-canable-monitor, live_obd_monitor.py, commit fa477535338e82ba7f17a6e1847a3d10acaaec47.
- OBDb/Genesis contributors, signalsets/v3/default.json, commit 7a12559e6aa26f3afc95dbda2467d630d990ca1c.

The adapted diagnostic signal definitions/formulas are offered under CC-BY-SA-4.0 (see THIRD_PARTY_GENESIS_LICENSE). Changes: headless Pi worker, application VehicleState conversion, fixed read request guard, bounded ISO-TP reassembly, per-signal request-start timestamps, existing HMI integration. The source's stationary input-validation scope is preserved in input_validation.json. No third-party Hyundai DBC is redistributed or required by this diagnostic adapter.
