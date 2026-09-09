"""Window computation and the within-day de-duplication store.

Two rules from the spec live here:

1. The pulse owns 08:30 to the last run of the day. The first run of a day
   floors its window at 08:30 local rather than reaching back to yesterday,
   because overnight traffic belongs to the 08:30 daily briefing. Without the
   floor, the 09:15 pulse re-serves everything the briefing just delivered.
2. De-duplication is within-day only. The store resets on the first run of a
   new day, and an item already surfaced never fires a second notification.
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timedelta, timezone

DAY_FLOOR_HOUR = 8
DAY_FLOOR_MINUTE = 30
AFTERNOON_BRIEFING_HOUR = 16
FALLBACK_LOOKBACK_H = 1

# Fields that identify a message. `link` and `from_name` are deliberately
# excluded: both get rebuilt each run and neither changes what was said.
_FINGERPRINT_FIELDS = ("source", "channel", "space_id", "from_email", "at", "text")


def fingerprint(candidate: dict) -> str:
    raw = "\x1f".join(str(candidate.get(f, "")) for f in _FINGERPRINT_FIELDS)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def pulse_window(last_run_iso: str | None, now: datetime, tz_offset_hours: float) -> datetime:
    """Start of this run's window, in UTC."""
    offset = timedelta(hours=tz_offset_hours)
    local_now = now + offset
    floor_local = local_now.replace(
        hour=DAY_FLOOR_HOUR, minute=DAY_FLOOR_MINUTE, second=0, microsecond=0
    )
    floor_utc = floor_local - offset

    if not last_run_iso:
        return now - timedelta(hours=FALLBACK_LOOKBACK_H)

    try:
        last = datetime.fromisoformat(last_run_iso)
    except (TypeError, ValueError):
        return now - timedelta(hours=FALLBACK_LOOKBACK_H)

    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)

    return max(last, floor_utc)


def next_briefing_at(now_local: datetime) -> tuple[str, str]:
    """Which briefing this run is measuring 'can it wait' against.

    The bar rises after 16:00: the next briefing is then tomorrow morning, so
    an item has to be unable to wait overnight rather than unable to wait a
    couple of hours.
    """
    if now_local.hour < AFTERNOON_BRIEFING_HOUR:
        return "today 16:00", "a few hours"
    return "tomorrow 08:30", "overnight"


def load_state(path: str, today: str) -> dict:
    empty = {"day": today, "seen": {}}
    if not os.path.exists(path):
        return empty
    try:
        with open(path) as fh:
            state = json.load(fh)
    except (OSError, ValueError):
        return empty
    if not isinstance(state, dict) or state.get("day") != today:
        return empty
    state.setdefault("seen", {})
    return state


def save_state(path: str, state: dict) -> None:
    tmp = f"{path}.tmp"
    with open(tmp, "w") as fh:
        json.dump(state, fh, indent=2)
    os.replace(tmp, path)
