# CARLA driving profile — 2026-10-27

Active hero is Audi A2 (vehicle.audi.a2), replacing Tesla Model 3. The measured physics/bounding-box profile is config/carla/ego-vehicle.json. Wheel positions returned by CARLA are transformed from world centimetres into ego coordinates before deriving the 2.5065 m wheelbase and 1.2563 m rear axle offset. Corridor and lane motion models use this geometry. Pedestrian extra margin is 0.69 m to retain approximately the previous 2.03 m pedestrian half-width despite the smaller car. Front RGB attributes/mount and FOV 60 are preserved.

The latest workspace owns tools/g29_ros_control.py, g29_input.py, g29_ff.py and config/g29.json. scripts/run-carla-g29.sh executes these copies, retaining the existing CARLA ROS bridge/state-file locations and physical axis/button calibration.

Previous autocenter was fixed strength 45%, gain 65%. New gain is 55%; autocenter strength interpolates 8/12/21/30/36% at 0/10/30/60/100 km/h, with an 8 percentage-point/s slew limit, 40% hard ceiling, and a return toward low-speed strength if ROS speed is stale. This is speed-sensitive centering, not a tyre self-aligning torque simulation. Shutdown/disconnect clears autocenter.

Pedal application uses raw throttle ** 1.5 to soften partial pedal response, retaining full throttle at full pedal. Raw pedal values used for CAN/abnormal-pedal detection remain unchanged. There is no artificial speed cap. Both pedals must be released for one second after controller restart before driving re-arms.

XGBoost remains between VEHICLE and PIP; compacted speed/gap spacing shifts the explanation upward.

Validation: 18 corridor tests and 9 feedback/lane-association tests passed. All five local CARLA services were active; pixel relay returned ready, and G29 reported connected with fresh ROS status. Subjective steering feel and acceleration require driver assessment.
