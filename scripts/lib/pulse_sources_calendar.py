"""Today's imminent meetings, used only as a priority trigger.

A message about a meeting starting soon is priority at any tier. This module
supplies the meeting list; the classifier decides whether a given message is
about one of them.

The msgraph CLI's real `start` shape is a dict with a *naive* local
`date_time` and the zone in a separate `time_zone` key — not an ISO string.
`datetime.fromisoformat` on that naive string would violate the project's
hard "never naive datetimes" constraint and then blow up (`TypeError`) the
instant it's compared against an aware `now`. We attach the zone explicitly
via `ZoneInfo` and treat a missing or unrecognized `time_zone` as
unparseable rather than assuming UTC. A plain ISO string with an explicit
offset (or trailing "Z") is also accepted, since that is what upstream
callers and tests may still hand in.
"""
from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

MSGRAPH = os.path.expanduser("~/.config/claude-graph/bin/msgraph")
LOOKAHEAD_HOURS = 2
TIMEOUT_S = 60


def _parse_start(start) -> datetime | None:
    """Return an aware datetime, or None if it can't be parsed as one.

    Never returns a naive datetime — a naive local time with no known zone
    is unparseable, not a UTC guess.
    """
    if isinstance(start, dict):
        date_time = start.get("date_time")
        tz_name = start.get("time_zone")
        if not date_time or not tz_name:
            return None
        try:
            naive = datetime.fromisoformat(date_time)
        except (TypeError, ValueError):
            return None
        if naive.tzinfo is not None:
            return naive
        try:
            tz = ZoneInfo(tz_name)
        except (ZoneInfoNotFoundError, ValueError, KeyError):
            return None
        return naive.replace(tzinfo=tz)

    if isinstance(start, str):
        try:
            parsed = datetime.fromisoformat(start.replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
        if parsed.tzinfo is None:
            # No offset and no separate time_zone key to consult — skip
            # rather than assume UTC.
            return None
        return parsed

    return None


def imminent_meetings(events: list[dict], now: datetime, lookahead_hours: int) -> list[dict]:
    """Meetings starting strictly after `now` and strictly before the
    lookahead edge. Both edges are exclusive: a meeting starting exactly
    "now" reads the same as already-started, and a meeting starting exactly
    at the lookahead boundary is left for the next cycle rather than fired
    early — the quieter answer at both edges.

    One unparseable event never discards the good events beside it.
    """
    horizon = now + timedelta(hours=lookahead_hours)
    out = []
    for event in events or []:
        start = _parse_start(event.get("start"))
        if start is None:
            continue
        if now < start < horizon:
            out.append({"subject": event.get("subject") or "(untitled)", "start": start.isoformat()})
    return out


def _run_msgraph(args: list[str]) -> str:
    try:
        return subprocess.run(
            [MSGRAPH, *args], capture_output=True, text=True, timeout=TIMEOUT_S, check=True
        ).stdout
    except subprocess.CalledProcessError as exc:
        # The msgraph CLI prints its own reason as {"error": "..."} on
        # stdout and exits 1. Under check=True that collapses to a
        # CalledProcessError whose str() is just "returned non-zero exit
        # status 1" — useless to whoever reads the degraded status. Unwrap
        # the real reason out of stdout (falling back to stderr, then the
        # exception itself) before it ever reaches the caller.
        try:
            reason = json.loads(exc.stdout or "").get("error")
        except (ValueError, AttributeError):
            reason = None
        reason = reason or (exc.stdout or "").strip() or (exc.stderr or "").strip() or str(exc)
        raise RuntimeError(reason) from exc


def collect(now: datetime, runner=_run_msgraph) -> tuple[list[dict], str]:
    """Returns (imminent_meetings, status). Never raises.

    `status` is "ok" or "degraded: <reason>". A degraded fetch always comes
    back with an empty meeting list, so it can never be mistaken for "no
    meetings soon" — a failed calendar fetch must be loud, not silent.

    Fetches two days of calendar (not one) so a pulse running late at night
    still sees a meeting just after midnight within the lookahead window;
    the window filter below discards everything else.
    """
    try:
        raw = runner(["calendar", "2"])
    except Exception as exc:  # noqa: BLE001 — any failure degrades, none propagates
        return [], f"degraded: {exc}"

    if not raw:
        return [], "degraded: empty output from msgraph"

    try:
        payload = json.loads(raw)
        events = payload.get("events", [])
    except (ValueError, TypeError, AttributeError) as exc:
        return [], f"degraded: unparseable calendar output ({exc})"

    if not isinstance(events, list):
        return [], "degraded: unparseable calendar output (events is not a list)"

    return imminent_meetings(events, now, LOOKAHEAD_HOURS), "ok"
