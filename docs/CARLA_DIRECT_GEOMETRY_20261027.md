# CARLA direct RGB geometry correction — 2026-10-27

Active source: CARLA rgb_front_hd, 1920×1080, horizontal FOV 60°, resized without crop to 1280×720. Camera mount: forward 1.8 m, right 0 m, height 1.6 m, pitch 0°. Physical lens / monitor recapture transforms are not used.

- Ground correspondences now come directly from the pinhole intrinsics and mount. The previous x=96…1184 image restriction is removed for this analytic source. Above-horizon / out-of-calibration metric projections remain rejected.
- Rendering projects left/right/centre curves independently and lets the existing SVG clip off-screen points. Drawing extrapolation never permits additional object measurements. Stationary path preview is 15 m; moving prediction remains bounded by 30 m / 4 s.
- Corridor calculations select capture-aligned CAN samples from a bounded one-second / 100-sample history, retaining the 200 ms alignment rejection bound. The displayed guide uses current steering.
- Only the CARLA demo unit sets external capture-age allowance to 500 ms, consistently in Pi fusion and team risk core. Other launches retain 250 ms. Capture timestamps are preserved. This is a demo transport budget, not a reduction in actual latency.
- Team FSM/TTC rules, lane confidence / duration / turn-signal conditions, and microphone settings are unchanged. Missing lane pairs produce unknown lane offset, not departure.

Validation: 24 corridor/fusion tests + 14 lane association/geometric tests passed. After deployment, external perception was present in 12/12 API samples, with actual capture ages 280–438 ms. A junction scene had no valid lane pair (offset null, confidence 0); lane recognition during a marked-road drive remains to be checked. No CARLA ground-truth object/lane positions are supplied to perception or decisions.


## Follow-up: missing / late warnings

A second 200 ms alignment gate in the team road rules still rejected delayed camera inputs. The live adapter now passes capture-aligned CAN history to both team road and object rules, without altering the team skew limit or replacing image timestamps. Regression test confirms a 300 ms-delayed lane measurement still produces lane departure after its original hold condition.

Dark-road-only gamma preprocessing (gamma 0.5, road V median <80) is applied to UFLD inputs, preserving all coordinates and daylight images. A recorded dark image improved from no lane to one boundary; no artificial missing boundary is generated. After deployment, live road measurements reached confidence 0.89–0.98 and approximately -1.1 m offset; lane_departure WARNING and EMERGENCY candidates were observed, along with red_light_approach warnings. Junction / missing-paint scenes still legitimately report unknown lanes.

Pedestrian motion fitting accepts three samples over at least 250 ms instead of four / 400 ms, with a stricter 0.2 m residual bound. Vehicle fitting remains unchanged. This minimum is not an end-to-end alarm latency guarantee. Actual pedestrian crossing / HMI onset timing remains to be validated.

Validation: 26 corridor/fusion tests, 16 lane tests, and 30 team-adapter / relaxation / environment tests passed.


## Pedestrian TTC follow-up

TTC is now reduced by the measured capture-to-decision age, preserving the original capture timestamp. Corridor diagnostic output includes capture_ttc_s and capture_age_ms. First swept-footprint entry is refined by six bisection steps within its 0.1 s interval; guide labels remain rounded for display.

Ground tracks no longer reset merely because a frame gap exceeds 250 ms (bounded reset gap now 750 ms). When narrow crossing-person boxes no longer overlap, identity may continue only with same-class, one-to-one metric association, maximum 500 ms gap and bounded lateral / longitudinal displacement; recent CAN motion compensates the previous ground point when available. This does not invent unseen detections or bypass collision ROI / confidence requirements.

Validation: 30 corridor/fusion/team-adapter tests and 8 lane/ground association tests passed. Dedicated tests cover zero-IoU pedestrian identity and 300 ms transport-age TTC correction. The 200-sample live recording contained no pedestrian detections; actual pedestrian alarm onset remains unverified. Raw recording: build/pedestrian-latency-review/live.jsonl.
