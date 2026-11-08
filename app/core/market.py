"""When background broker checks are worth making.

Orders and positions can only change while the exchange is trading, so the periodic broker
checks (order-book resync, strategy reconciliation, resume retries) run only in the window
set by `background_hours` (IST, weekdays), plus one last order-book check after it closes.
Outside it they would spend the broker's daily API quota for nothing. NSE holidays are not
known here: on those days the checks simply run as on any weekday.
"""

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from app.core.config import settings

IST = ZoneInfo("Asia/Kolkata")


def _window() -> tuple[time, time] | None:
    spec = settings.background_hours
    if not spec:
        return None  # empty: always
    start, end = (time.fromisoformat(part) for part in spec.split("-"))
    return start, end


def in_market_hours(now: datetime | None = None) -> bool:
    window = _window()
    if window is None:
        return True
    now = (now or datetime.now(IST)).astimezone(IST)
    return now.weekday() < 5 and window[0] <= now.time() < window[1]


def seconds_until_market_hours(now: datetime | None = None) -> float:
    """0 inside the window; otherwise seconds until it next opens (skipping weekends)."""
    window = _window()
    now = (now or datetime.now(IST)).astimezone(IST)
    if window is None or in_market_hours(now):
        return 0.0
    day = now.date()
    while True:
        opens = datetime.combine(day, window[0], IST)
        if day.weekday() < 5 and opens > now:
            return (opens - now).total_seconds()
        day += timedelta(days=1)
