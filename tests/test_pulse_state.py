import json
from datetime import datetime, timedelta, timezone

from lib.pulse_state import (
    fingerprint,
    pulse_window,
    next_briefing_at,
    load_state,
    save_state,
)

UTC = timezone.utc


def _cand(**kw):
    base = {
        "source": "webex", "channel": "C3 + CUI", "space_id": "abc",
        "from_email": "rorscott@cisco.com", "at": "2026-09-09T14:02:11+00:00",
        "text": "Is the cross-cluster walk verified?",
    }
    base.update(kw)
    return base


def test_fingerprint_is_stable_for_the_same_message():
    assert fingerprint(_cand()) == fingerprint(_cand())


def test_fingerprint_differs_when_text_differs():
    assert fingerprint(_cand()) != fingerprint(_cand(text="different"))


def test_fingerprint_differs_across_channels_for_identical_text():
    assert fingerprint(_cand()) != fingerprint(_cand(channel="Mini EC with CUI"))


def test_fingerprint_ignores_fields_that_change_between_runs():
    # `link` and display name get rebuilt each run; they must not shift the id.
    assert fingerprint(_cand(link="x", from_name="Rob")) == fingerprint(
        _cand(link="y", from_name="Robert")
    )


def test_window_uses_last_run_when_it_is_after_the_day_floor():
    now = datetime(2026, 9, 9, 18, 15, tzinfo=UTC)  # 14:15 ET
    last = datetime(2026, 9, 9, 17, 15, tzinfo=UTC)  # 13:15 ET
    assert pulse_window(last.isoformat(), now, tz_offset_hours=-4) == last


def test_first_run_of_the_day_floors_to_0830_local():
    # 13:15 UTC = 09:15 ET. Last run was yesterday afternoon.
    now = datetime(2026, 9, 9, 13, 15, tzinfo=UTC)
    last = datetime(2026, 9, 8, 21, 15, tzinfo=UTC)
    got = pulse_window(last.isoformat(), now, tz_offset_hours=-4)
    assert got == datetime(2026, 9, 9, 12, 30, tzinfo=UTC)  # 08:30 ET


def test_missing_last_run_falls_back_to_one_hour_but_never_before_the_floor():
    now = datetime(2026, 9, 9, 18, 15, tzinfo=UTC)
    assert pulse_window(None, now, tz_offset_hours=-4) == now - timedelta(hours=1)


def test_next_briefing_is_todays_4pm_before_1600_local():
    label, horizon = next_briefing_at(datetime(2026, 9, 9, 14, 15))
    assert label == "today 16:00"
    assert horizon == "a few hours"


def test_next_briefing_is_tomorrow_morning_at_or_after_1600_local():
    label, horizon = next_briefing_at(datetime(2026, 9, 9, 16, 15))
    assert label == "tomorrow 08:30"
    assert horizon == "overnight"


def test_state_loads_empty_when_the_file_is_absent(tmp_path, capsys):
    state = load_state(str(tmp_path / "nope.json"), "2026-09-09")
    assert state == {"day": "2026-09-09", "seen": {}}
    assert capsys.readouterr().err == ""


def test_state_resets_when_the_day_changed(tmp_path):
    path = tmp_path / "seen.json"
    path.write_text(json.dumps({"day": "2026-09-08", "seen": {"old": {}}}))
    state = load_state(str(path), "2026-09-09")
    assert state == {"day": "2026-09-09", "seen": {}}


def test_state_survives_within_the_same_day(tmp_path):
    path = tmp_path / "seen.json"
    path.write_text(json.dumps({"day": "2026-09-09", "seen": {"keep": {"tier": "priority"}}}))
    state = load_state(str(path), "2026-09-09")
    assert "keep" in state["seen"]


def test_state_resets_rather_than_crashing_on_corrupt_json(tmp_path):
    path = tmp_path / "seen.json"
    path.write_text("{not json")
    assert load_state(str(path), "2026-09-09") == {"day": "2026-09-09", "seen": {}}


def test_state_warns_on_stderr_when_the_file_is_corrupt(tmp_path, capsys):
    path = tmp_path / "seen.json"
    path.write_text("{not json")
    state = load_state(str(path), "2026-09-09")
    assert state == {"day": "2026-09-09", "seen": {}}
    err = capsys.readouterr().err
    assert str(path) in err
    assert "WARNING" in err


def test_save_then_load_round_trips(tmp_path):
    path = str(tmp_path / "seen.json")
    save_state(path, {"day": "2026-09-09", "seen": {"a": {"tier": "panel"}}})
    assert load_state(path, "2026-09-09")["seen"]["a"]["tier"] == "panel"
