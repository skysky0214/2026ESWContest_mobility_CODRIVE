"""Short deterministic Korean warnings from observed conditions and model level.

These are local templates, not learned text, SHAP attribution or collision claims.
"""

from .context import DEFAULT_CONTEXT_POLICY


def warning_v3(sample, level):
    """Observed facts only; these templates are not model feature attribution."""
    legacy = {
        "driver_drowsy": sample["driver_behavior"] in {"drowsy", "head_drop"},
        "phone_usage": sample["phone_detected"] is True or sample["driver_behavior"] == "phone",
        "pedestrian_count": sample["pedestrian_count"],
        "accelerator_pct": sample["accelerator_pct"],
        "traffic_light": sample["traffic_light"],
        "eyes_on_road": sample["looking_forward"],
        "distraction_score": (
            None if sample["attention_score"] is None else 1 - sample["attention_score"]
        ),
        "audio_direction": (sample["audio_direction"] or "").upper(),
        "siren_detected": sample["audio_kind"] == "siren",
        "horn_detected": sample["audio_kind"] == "horn",
        "weather": sample["weather"],
        "is_night": sample["is_night"],
    }
    factors, message = warning(legacy, level)
    additions = []
    for field, factor, text in (
        ("stop_sign_detected", "STOP_SIGN", "정지 표지판이 검출되었습니다."),
        ("central_object_present", "CENTRAL_OBJECT", "화면 중앙에서 주변 객체가 검출되었습니다."),
    ):
        if sample[field] is True:
            factors.append(factor)
            additions.append(text)
    if sample["visibility_m"] is not None and sample["visibility_m"] < 1000:
        factors.append("LOW_VISIBILITY")
        additions.append("가시거리가 짧습니다.")
    if (sample["acceleration_duration_ms"] or 0) >= 300:
        factors.append("RAPID_ACCELERATION_OBSERVED")
        additions.append("차속의 급격한 증가가 관측되었습니다.")
    if (sample["pedal_rate_duration_ms"] or 0) >= 100:
        factors.append("RAPID_PEDAL_OBSERVED")
        additions.append("급격한 가속페달 조작이 관측되었습니다.")
    return factors, " ".join([message, *additions])


def warning(sample, level, context_policy=None):
    if level not in {"LOW", "MEDIUM", "HIGH"}:
        raise ValueError("Warning level must be LOW/MEDIUM/HIGH")
    policy = DEFAULT_CONTEXT_POLICY if context_policy is None else context_policy
    factors, details = [], []
    action = None
    if sample["driver_drowsy"] is True:
        factors.append("DRIVER_DROWSINESS")
        details.append("운전자 졸음이 감지되었습니다.")
        action = "안전한 곳에 정차해 휴식을 취하세요."
    if sample["phone_usage"] is True:
        factors.append("PHONE_USAGE")
        details.append("휴대폰 사용이 감지되었습니다.")
        if action is None:
            action = "휴대폰을 내려놓고 운전에 집중하세요."
    if (sample["pedestrian_count"] or 0) > 0:
        factors.append("PEDESTRIAN_OBSERVED")
        if (sample["accelerator_pct"] or 0) >= policy["accelerator_threshold_pct"]:
            factors.append("PEDESTRIAN_ACCELERATING")
            details.append("전방 영상에서 보행자가 검출됐고 가속페달 입력이 있습니다.")
            if action is None:
                action = "가속페달 입력을 확인하고 주변을 살피세요."
        else:
            details.append("전방 영상에서 보행자가 검출되었습니다.")
    if sample["traffic_light"] == "RED":
        factors.append("RED_LIGHT")
        details.append("적색 신호가 감지되었습니다.")
    if sample["eyes_on_road"] is False or (sample["distraction_score"] or 0) >= 0.6:
        factors.append("DISTRACTION")
        details.append("전방 주의 분산이 감지되었습니다.")
        if action is None:
            action = "전방을 주시하세요."
    direction = {"LEFT": "왼쪽에서 ", "RIGHT": "오른쪽에서 "}.get(sample["audio_direction"], "")
    audio = []
    if sample["siren_detected"] is True:
        factors.append("SIREN")
        audio.append("사이렌")
    if sample["horn_detected"] is True:
        factors.append("HORN")
        audio.append("경적")
    # Always retain the observed direction in the spoken warning when audio exists.
    selected = details[:1] if audio else details[:2]
    if audio:
        selected.append(direction + "·".join(audio) + "이 감지되었습니다.")
    weather = {
        "RAIN": "비가 오는",
        "SNOW": "눈이 오는",
        "FOG": "안개가 낀",
        "THUNDER": "뇌우가 있는",
    }.get(sample["weather"])
    if weather:
        factors.append("ADVERSE_WEATHER")
    if sample["is_night"] is True:
        factors.append("NIGHT")
    if weather and sample["is_night"] is True:
        context = weather + " 야간입니다."
    elif weather:
        context = weather + " 날씨입니다."
    elif sample["is_night"] is True:
        context = "야간입니다."
    else:
        context = None
    prefix = {"LOW": "NORMAL", "MEDIUM": "CAUTION", "HIGH": "WARNING"}[level]
    parts = [prefix, *selected]
    if context and (selected or level != "LOW"):
        parts.append(context)
    if selected or level != "LOW":
        parts.append("주변을 확인하세요." if action is None else action)
    return factors, " ".join(parts)
