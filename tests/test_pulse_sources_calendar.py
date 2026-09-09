import json
import subprocess
from datetime import datetime, timedelta, timezone

from lib import pulse_sources_calendar as calendar_mod
from lib.pulse_sources_calendar import LOOKAHEAD_HOURS, imminent_meetings, collect

UTC = timezone.utc
NOW = datetime(2026, 9, 9, 18, 15, tzinfo=UTC)


def _event(subject, start):
    return {"subject": subject, "start": start}


def _dict_start(date_time, time_zone="America/New_York"):
    return {
        "date_time": date_time,
        "time_zone": time_zone,
        "formatted": "irrelevant",
        "date": date_time[:10],
    }


def test_meeting_inside_the_window_is_returned():
    events = [_event("Cert sync", "2026-09-09T19:00:00+00:00")]
    assert [m["subject"] for m in imminent_meetings(events, NOW, 2)] == ["Cert sync"]


def test_meeting_beyond_the_window_is_excluded():
    events = [_event("Tomorrow thing", "2026-09-09T23:00:00+00:00")]
    assert imminent_meetings(events, NOW, 2) == []


def test_meeting_already_started_is_excluded():
    events = [_event("Started", "2026-09-09T17:00:00+00:00")]
    assert imminent_meetings(events, NOW, 2) == []


def test_unparseable_start_is_skipped_not_fatal():
    events = [_event("Broken", "not a date"), _event("Good", "2026-09-09T19:00:00+00:00")]
    assert [m["subject"] for m in imminent_meetings(events, NOW, 2)] == ["Good"]


def test_collect_degrades_rather_than_raising():
    def boom(_a):
        raise RuntimeError("cookie expired")

    meetings, status = collect(NOW, runner=boom)
    assert meetings == []
    assert status.startswith("degraded:")


def test_collect_parses_a_successful_run():
    payload = json.dumps({"events": [_event("Cert sync", "2026-09-09T19:00:00+00:00")]})
    meetings, status = collect(NOW, runner=lambda _a: payload)
    assert status == "ok"
    assert meetings[0]["subject"] == "Cert sync"


# --- Boundary decisions: both edges strict, so the pulse errs quiet. A
# meeting starting exactly "now" is not treated as still-ahead (it reads the
# same as already-started), and a meeting starting exactly at the lookahead
# edge is excluded rather than included. ---


def test_meeting_starting_exactly_now_is_excluded():
    events = [_event("Right now", NOW.isoformat())]
    assert imminent_meetings(events, NOW, 2) == []


def test_meeting_starting_exactly_at_the_lookahead_edge_is_excluded():
    edge = (NOW + timedelta(hours=2)).isoformat()
    events = [_event("Right at the edge", edge)]
    assert imminent_meetings(events, NOW, 2) == []


def test_meeting_one_second_inside_the_edge_is_included():
    inside = (NOW + timedelta(hours=2) - timedelta(seconds=1)).isoformat()
    events = [_event("Just inside", inside)]
    assert [m["subject"] for m in imminent_meetings(events, NOW, 2)] == ["Just inside"]


# --- The real msgraph shape: start is a dict with a naive local date_time and
# a separate time_zone key, not an ISO string. ---


def test_dict_shaped_start_with_explicit_time_zone_is_parsed():
    # 19:00 America/New_York == 23:00 UTC, well outside a 2h window from
    # 18:15 UTC — use a time that lands inside the window instead.
    # 14:30 America/New_York == 18:30 UTC (EDT, UTC-4) — inside the window.
    events = [_event("Local time meeting", _dict_start("2026-09-09T14:30:00"))]
    result = imminent_meetings(events, NOW, 2)
    assert [m["subject"] for m in result] == ["Local time meeting"]


def test_dict_shaped_start_with_missing_time_zone_is_unparseable_not_utc():
    start = _dict_start("2026-09-09T14:30:00")
    del start["time_zone"]
    events = [_event("No zone", start), _event("Good", "2026-09-09T19:00:00+00:00")]
    assert [m["subject"] for m in imminent_meetings(events, NOW, 2)] == ["Good"]


def test_dict_shaped_start_with_bogus_time_zone_is_unparseable_not_utc():
    events = [
        _event("Bogus zone", _dict_start("2026-09-09T14:30:00", time_zone="Not/AZone")),
        _event("Good", "2026-09-09T19:00:00+00:00"),
    ]
    assert [m["subject"] for m in imminent_meetings(events, NOW, 2)] == ["Good"]


def test_a_bare_naive_iso_string_with_no_offset_is_unparseable_not_assumed_utc():
    # No offset and no separate time_zone key at all — the hard constraint
    # is "never naive datetimes", so this must be skipped, not defaulted.
    events = [_event("Naive", "2026-09-09T19:00:00"), _event("Good", "2026-09-09T19:00:00+00:00")]
    assert [m["subject"] for m in imminent_meetings(events, NOW, 2)] == ["Good"]


# --- collect() fetches two days, not one, so a pulse running late at night
# still sees meetings just after midnight within the lookahead window. ---


def test_collect_fetches_two_days_of_calendar():
    seen_args = []

    def capture(args):
        seen_args.append(args)
        return json.dumps({"events": []})

    collect(NOW, runner=capture)
    assert seen_args[0] == ["calendar", "2"]


# --- collect() must never raise, and a malformed payload must degrade
# loudly rather than silently returning "no meetings". ---


def test_collect_reports_degraded_on_unparseable_output():
    meetings, status = collect(NOW, runner=lambda _a: "not json")
    assert meetings == []
    assert status.startswith("degraded:")


def test_collect_reports_degraded_when_events_is_not_a_list():
    payload = json.dumps({"events": "oops"})
    meetings, status = collect(NOW, runner=lambda _a: payload)
    assert meetings == []
    assert status.startswith("degraded:")


def test_collect_reports_degraded_when_runner_returns_none():
    meetings, status = collect(NOW, runner=lambda _a: None)
    assert meetings == []
    assert status.startswith("degraded:")


def test_collect_reports_degraded_when_runner_times_out():
    def timeout(_args):
        raise subprocess.TimeoutExpired(cmd=["msgraph"], timeout=60)

    meetings, status = collect(NOW, runner=timeout)
    assert meetings == []
    assert status.startswith("degraded:")


def test_collect_ok_with_no_meetings_is_distinguishable_from_a_failure():
    payload = json.dumps({"events": []})
    meetings, status = collect(NOW, runner=lambda _a: payload)
    assert meetings == []
    assert status == "ok"


# --- Reuse of the email module's CalledProcessError-unwrapping: the msgraph
# CLI prints its reason as {"error": "..."} on stdout and exits 1, so a bare
# CalledProcessError str() ("returned non-zero exit status 1") must never be
# what reaches the status string. ---


def test_collect_surfaces_the_msgraph_cli_error_reason_not_just_exit_status(monkeypatch):
    def fake_subprocess_run(*args, **kwargs):
        raise subprocess.CalledProcessError(
            returncode=1, cmd=["msgraph"], output='{"error": "cookie expired"}'
        )

    monkeypatch.setattr(calendar_mod.subprocess, "run", fake_subprocess_run)
    meetings, status = collect(NOW, runner=calendar_mod._run_msgraph)
    assert meetings == []
    assert status.startswith("degraded:")
    assert "cookie expired" in status


def test_lookahead_hours_constant_is_two():
    assert LOOKAHEAD_HOURS == 2


# --- Bug fix round 1: a non-dict element must not raise, in
# imminent_meetings directly or via collect, and one bad element must not
# take the good ones down with it. ---


def test_imminent_meetings_skips_a_non_dict_element_directly():
    events = [1, _event("Good", "2026-09-09T19:00:00+00:00")]
    assert [m["subject"] for m in imminent_meetings(events, NOW, 2)] == ["Good"]


# --- Bug fix round 2: a non-empty payload where every element is unreadable
# must not be indistinguishable from a genuinely quiet calendar — mirrors
# pulse_sources_email.py's "no usable messages" rule, same status shape. ---


def test_collect_degrades_when_every_event_is_unreadable():
    # All elements are unusable — this must never raise (round 1), but it
    # also must not read as "ok, no meetings soon" (round 2): a shape change
    # in msgraph's output would otherwise make the pulse silently report a
    # quiet calendar forever.
    payload = json.dumps({"events": [1, 2, 3]})
    meetings, status = collect(NOW, runner=lambda _a: payload)
    assert meetings == []
    assert status == "degraded: no usable events in msgraph output"


def test_collect_returns_the_good_meeting_from_a_mixed_batch_not_empty():
    # Some elements usable, one is not — stays "ok" with the good meeting.
    # One bad element must never suppress the good ones or flip the run to
    # degraded.
    payload = json.dumps({"events": [1, _event("Good", "2026-09-09T19:00:00+00:00")]})
    meetings, status = collect(NOW, runner=lambda _a: payload)
    assert status == "ok"
    assert [m["subject"] for m in meetings] == ["Good"]


def test_collect_stays_ok_when_all_events_are_valid_but_outside_the_window():
    # Regression guard: "unreadable" must never be confused with "readable
    # but not imminent". A calendar full of perfectly valid meetings that
    # are all more than the lookahead away is the normal case, not a
    # degraded one — getting this backwards would make the pulse cry
    # degraded on almost every ordinary hour.
    payload = json.dumps({"events": [_event("Tomorrow thing", "2026-09-09T23:00:00+00:00")]})
    meetings, status = collect(NOW, runner=lambda _a: payload)
    assert meetings == []
    assert status == "ok"
