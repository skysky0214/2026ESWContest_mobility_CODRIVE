"""Fresh weather and solar API context for road risk; never creates a hazard."""
from .models import Weather
from .sensor_features import environment_validity


def environment_context(env, *, now_ns, policy):
    if env is None or not 0 <= now_ns - env.header.capture_time_ns <= policy.environment_ttl_ms * 1_000_000:
        return 1.0, ("environment=missing_or_stale",)
    weather_valid, daylight_valid = environment_validity(env, now_ns)
    visibility, surface, evidence = 1.0, 1.0, []
    if daylight_valid:
        evidence.append(f"night={str(env.is_night).lower()}")
        if env.is_night:
            visibility = policy.night_weight
        for name in ("sunrise_time_ns", "sunset_time_ns"):
            value = getattr(env, name, None)
            if value is not None:
                evidence.append(f"{name}={value}")
    if weather_valid:
        evidence.append(f"weather={env.weather.value}")
        if env.weather is Weather.FOG:
            visibility = max(visibility, policy.fog_weight)
        if env.visibility_m is not None and env.visibility_m < policy.visibility_warning_m:
            fraction = min(1.0, max(0.0, (policy.visibility_warning_m - env.visibility_m)
                           / (policy.visibility_warning_m - policy.visibility_critical_m)))
            visibility = max(visibility, 1 + fraction * (policy.visibility_max_weight - 1))
            evidence.append(f"visibility_m={env.visibility_m:.0f}")
        if env.weather is Weather.RAIN or env.precipitation_mm_h > 0:
            surface = policy.rain_weight
        if env.weather is Weather.SNOW or env.snowfall_cm_h > 0:
            surface = max(surface, policy.snow_weight)
        if env.weather is Weather.THUNDER:
            surface = max(surface, policy.thunder_weight)
    if not weather_valid:
        evidence.append("weather=unavailable")
    if not daylight_valid:
        evidence.append("daylight=unavailable")
    return min(policy.environment_weight_cap, visibility * surface), tuple(evidence)
