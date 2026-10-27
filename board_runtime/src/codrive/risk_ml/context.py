"""Frozen template context from origin/XGBoost at 8545fbb."""

DEFAULT_CONTEXT_POLICY = {
    "moving_speed_kmh": 5.0,
    "accelerator_threshold_pct": 15.0,
    "night_multiplier": 1.10,
    "weather_multipliers": {
        "CLEAR": 1.0,
        "CLOUDY": 1.0,
        "RAIN": 1.15,
        "SNOW": 1.30,
        "FOG": 1.20,
        "THUNDER": 1.30,
    },
}
