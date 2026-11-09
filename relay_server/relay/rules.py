"""OK / KO rules, evaluated locally so they work while the main server is down."""
from datetime import datetime

DEFAULT_SETTINGS = {"max_in_time": "08:00", "min_out_time": "17:00"}


def _hm(value: str):
    h, m = value.split(":")
    return int(h), int(m)


def evaluate(direction: str, when: datetime, settings: dict):
    """Return (status, reason). `when` is local time."""
    s = {**DEFAULT_SETTINGS, **(settings or {})}
    now_hm = (when.hour, when.minute)
    if direction == "IN" and now_hm > _hm(s["max_in_time"]):
        return "KO", "late"
    if direction == "OUT" and now_hm < _hm(s["min_out_time"]):
        return "KO", "early"
    return "OK", None


def suggest_direction(last_checkin):
    """Alternate IN/OUT within the day."""
    return "OUT" if last_checkin and last_checkin["direction"] == "IN" else "IN"
