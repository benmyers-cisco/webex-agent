import ast
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

from hourly_pulse import run

UTC = timezone.utc
NOW = datetime(2026, 9, 9, 18, 15, 3, tzinfo=UTC)

PREFS_TEXT = """
## Watchlist
- Rory Scott <rorscott@cisco.com>

### Priority 1 — Interrupt Me
- C3 + CUI

### Priority 2 — Tagged Only
- SCC - CII Discussion
"""

CAND = {
    "source": "webex", "channel": "C3 + CUI", "space_id": "abc",
    "from_name": "Rory Scott", "from_email": "rorscott@cisco.com",
    "at": "2026-09-09T18:02:11+00:00", "text": "Confirm before the pre-read?",
    "link": "https://web.webex.com/spaces/abc",
    "is_watchlist": True, "is_direct_mention": False, "is_watched_thread": False,
    "tier_hint": "p1",
}

PRIORITY_VERDICT = {
    "tier": "priority", "trigger": "watchlist", "why": "Ask.", "draft_reply": "On it.",
}


def _deps(tmp_path, **over):
    base = {
        "now": NOW,
        "tz_offset_hours": -4,
        "prefs_text": PREFS_TEXT,
        "output_dir": str(tmp_path / "output"),
        "state_path": str(tmp_path / ".pulse_seen.json"),
        "last_run_path": str(tmp_path / ".last_pulse_run"),
        "my_email": "benmyers@cisco.com",
        "my_names": ["Ben Myers"],
        "collect_webex": lambda **kw: ([dict(CAND)], "ok"),
        "collect_email": lambda **kw: ([], "ok"),
        "collect_calendar": lambda **kw: ([], "ok"),
        "classify": lambda **kw: [dict(PRIORITY_VERDICT)],
        "notify": lambda items: bool(items),
    }
    base.update(over)
    return base


def _boom(**kw):
    raise RuntimeError("webex unreachable")


def test_a_priority_item_produces_an_artifact_and_notifies(tmp_path):
    payload = run(_deps(tmp_path))
    assert payload["status"] == "ok"
    assert payload["notified_this_run"] is True
    assert payload["items"][0]["tier"] == "priority"
    assert payload["items"][0]["text"] == "Confirm before the pre-read?"


def test_the_same_item_on_a_second_run_does_not_notify_again(tmp_path):
    deps = _deps(tmp_path)
    run(deps)
    second = run(deps)
    assert second["notified_this_run"] is False
    assert second["items"][0]["tier"] == "priority"  # still shown, just silent


def test_no_candidates_means_no_notification(tmp_path):
    payload = run(_deps(tmp_path, collect_webex=lambda **kw: ([], "ok")))
    assert payload["notified_this_run"] is False
    assert payload["items"] == []


def test_only_panel_items_means_no_notification(tmp_path):
    deps = _deps(tmp_path, classify=lambda **kw: [
        {"tier": "panel", "trigger": "p1_channel", "why": "FYI", "draft_reply": None}
    ])
    payload = run(deps)
    assert payload["notified_this_run"] is False
    assert payload["items"][0]["tier"] == "panel"


def test_a_degraded_email_source_is_reported_and_webex_still_runs(tmp_path):
    deps = _deps(tmp_path, collect_email=lambda **kw: ([], "degraded: token expired"))
    payload = run(deps)
    assert payload["status"] == "ok"
    assert payload["sources"]["email"] == "degraded: token expired"
    assert len(payload["items"]) == 1


def test_a_degraded_webex_source_reaches_the_artifact_rather_than_reading_ok(tmp_path):
    """F9. A run where get_messages failed for every listed space produces zero
    Webex candidates. If `sources.webex` still says "ok", the panel renders a
    total blackout as a quiet hour. The orchestrator must pass collect's status
    through instead of hardcoding the literal.
    """
    deps = _deps(
        tmp_path,
        collect_webex=lambda **kw: ([], "degraded: 17 of 17 spaces could not be fetched"),
        classify=lambda **kw: [],
    )
    payload = run(deps)
    assert payload["sources"]["webex"] == "degraded: 17 of 17 spaces could not be fetched"
    on_disk = json.load(open(tmp_path / "output" / "pulse.json"))
    assert on_disk["sources"]["webex"] != "ok"


def test_a_webex_failure_produces_a_failed_artifact_not_an_empty_one(tmp_path):
    payload = run(_deps(tmp_path, collect_webex=_boom))
    assert payload["status"] == "failed"
    assert "webex unreachable" in payload["reason"]
    on_disk = json.load(open(tmp_path / "output" / "pulse.json"))
    assert on_disk["status"] == "failed"


def test_the_artifact_lands_on_disk(tmp_path):
    run(_deps(tmp_path))
    path = tmp_path / "output" / "pulse.json"
    assert json.load(open(path))["items"][0]["from"]["name"] == "Rory Scott"


def test_last_pulse_run_is_written_and_last_run_is_never_touched(tmp_path):
    deps = _deps(tmp_path)
    shared = tmp_path / ".last_run"
    shared.write_text("2026-09-08T20:00:00+00:00")
    before = shared.read_text()
    run(deps)
    assert os.path.exists(deps["last_run_path"])
    assert shared.read_text() == before


def _identifiers(tree):
    """Every name-like token in a parsed module."""
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            out.add(node.id)
        elif isinstance(node, ast.Attribute):
            out.add(node.attr)
        elif isinstance(node, ast.alias):
            out.add(node.name)
            if node.asname:
                out.add(node.asname)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.add(node.name)
        elif isinstance(node, ast.arg):
            out.add(node.arg)
        elif isinstance(node, ast.keyword) and node.arg:
            out.add(node.arg)
    return out


def _docstring_node_ids(tree):
    ids = set()
    for node in ast.walk(tree):
        if not isinstance(
            node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        ):
            continue
        body = getattr(node, "body", None)
        if not body:
            continue
        first = body[0]
        if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) \
                and isinstance(first.value.value, str):
            ids.add(id(first.value))
    return ids


def test_no_pulse_module_references_the_shared_state_files():
    """The real guard. A unit test can only prove this run didn't write them;
    this proves no code path can. `.last_run` and `.watched_threads.json` belong
    to daily_summary.py, and a pulse write to either corrupts the daily briefing.

    This walks the AST rather than scanning raw text, because the modules
    document the prohibition in their docstrings and comments — a text scan
    reddens on a correct implementation that explains itself. Docstrings and
    comments are prose; only executable string constants and identifiers can
    actually touch a file.
    """
    forbidden_names = ("save_run_timestamp", "save_watched_threads", "prune_watched_threads")

    # hourly_pulse.py plus the eight lib/pulse_*.py modules. Hardcoded on
    # purpose, and NOT len(list(glob(...))): a count derived the same way the
    # scan derives it agrees with a broken glob and the guard stays green while
    # protecting nothing. `assert sources` was a tautology for the same reason —
    # the first element is hardcoded, so it could never fire. A rename or a
    # move must break this test loudly, because what it guards is .last_run,
    # the daily briefing's watermark.
    expected_module_count = 9

    scripts = Path(__file__).resolve().parent.parent / "scripts"
    sources = [scripts / "hourly_pulse.py", *sorted((scripts / "lib").glob("pulse_*.py"))]
    assert len(sources) == expected_module_count, (
        f"guard scanned {len(sources)} module(s), expected {expected_module_count}: "
        f"{[p.name for p in sources]}. Either a pulse module moved or was renamed "
        f"(update this count deliberately), or the glob broke and this guard is vacuous."
    )
    for path in sources:
        assert path.exists(), f"{path} does not exist — the guard would scan nothing"

    for path in sources:
        tree = ast.parse(path.read_text())
        docstrings = _docstring_node_ids(tree)

        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            if id(node) in docstrings:
                continue
            residue = node.value.replace(".last_pulse_run", "")
            assert ".last_run" not in residue, f"{path.name} references .last_run"
            for name in forbidden_names:
                assert name not in residue, f"{path.name} names {name} in a string"

        names = _identifiers(tree)
        for name in forbidden_names:
            assert name not in names, f"{path.name} references {name}"


def test_a_new_day_archives_yesterdays_artifact(tmp_path):
    run(_deps(tmp_path))
    tomorrow = datetime(2026, 9, 10, 18, 15, tzinfo=UTC)
    run(_deps(tmp_path, now=tomorrow))
    assert (tmp_path / "output" / "pulse-2026-09-09.json").exists()


def test_a_new_day_clears_the_seen_store_and_can_notify_again(tmp_path):
    deps = _deps(tmp_path)
    run(deps)
    tomorrow = datetime(2026, 9, 10, 18, 15, tzinfo=UTC)
    payload = run(_deps(tmp_path, now=tomorrow))
    assert payload["notified_this_run"] is True


# --- Ruling 31: the failure path must not advance the watermark -------------

def test_a_failed_run_does_not_create_the_pulse_watermark(tmp_path):
    """A failed run that advanced .last_pulse_run would permanently discard its
    own window: nothing would ever re-scan it and an urgent message is lost.
    """
    deps = _deps(tmp_path, collect_webex=_boom)
    payload = run(deps)
    assert payload["status"] == "failed"
    assert not os.path.exists(deps["last_run_path"])


def test_a_failed_run_leaves_an_existing_watermark_unchanged(tmp_path):
    run(_deps(tmp_path))
    watermark = tmp_path / ".last_pulse_run"
    before = watermark.read_text()

    later = datetime(2026, 9, 9, 19, 15, 3, tzinfo=UTC)
    payload = run(_deps(tmp_path, now=later, collect_webex=_boom))
    assert payload["status"] == "failed"
    assert watermark.read_text() == before


def test_the_next_run_after_a_failure_rescans_the_failed_window(tmp_path):
    """Proof that leaving the watermark alone actually recovers the window: the
    run after a failure reaches back to the last SUCCESSFUL run, so the failed
    run's hour is scanned rather than discarded.
    """
    first = datetime(2026, 9, 9, 17, 15, 3, tzinfo=UTC)
    run(_deps(tmp_path, now=first))  # succeeds, watermark = 17:15:03

    run(_deps(tmp_path, collect_webex=_boom))  # 18:15:03, fails

    seen_since = []

    def capture(**kw):
        seen_since.append(kw["since"])
        return []

    later = datetime(2026, 9, 9, 19, 15, 3, tzinfo=UTC)
    run(_deps(tmp_path, now=later, collect_webex=capture))
    assert seen_since[0] == first


# --- Ruling 32: the notified flag is sticky ---------------------------------

def test_an_item_notified_on_run_one_still_reads_notified_on_run_three(tmp_path):
    first = run(_deps(tmp_path))
    assert first["notified_this_run"] is True
    assert first["items"][0]["notified"] is True

    quiet = run(_deps(tmp_path, collect_webex=lambda **kw: ([], "ok")))
    assert quiet["items"] == []

    third = run(_deps(tmp_path))
    assert third["notified_this_run"] is False
    assert third["items"][0]["notified"] is True


def test_a_priority_item_whose_banner_failed_is_retried_on_the_next_run(tmp_path):
    """The gap: without this, an urgent message whose banner hit a TCC dialog or
    a timeout never interrupts Ben at all. Transient failure is what a retry is
    for. Paired with the test below, which pins the opposite guarantee.
    """
    first = run(_deps(tmp_path, notify=lambda items: False))
    assert first["notified_this_run"] is False

    state = json.load(open(tmp_path / ".pulse_seen.json"))
    assert state["seen"]
    assert all(entry["notified"] is False for entry in state["seen"].values())

    calls = []

    def counting_notify(items):
        calls.append(list(items))
        return bool(items)

    second = run(_deps(tmp_path, notify=counting_notify))
    assert second["notified_this_run"] is True
    assert len(calls) == 1
    assert [item["text"] for item in calls[0]] == ["Confirm before the pre-read?"]


def test_a_priority_item_whose_banner_succeeded_is_not_retried_on_the_next_run(tmp_path):
    """The regression the sticky flag exists to prevent: Ben must not be
    interrupted twice about the same message.
    """
    first = run(_deps(tmp_path))
    assert first["notified_this_run"] is True

    calls = []

    def counting_notify(items):
        calls.append(list(items))
        return bool(items)

    second = run(_deps(tmp_path, notify=counting_notify))
    assert second["notified_this_run"] is False
    assert calls == [[]]
    assert second["items"][0]["tier"] == "priority"  # still shown, just silent


def test_the_artifact_never_claims_a_banner_fired_when_it_failed(tmp_path):
    """Assert against the file, not the return value: the artifact is what the
    Hub renders to Ben, and a false `notified: true` there is this design's
    central inversion — silence reading as success — reaching the user.
    """
    run(_deps(tmp_path, notify=lambda items: False))

    on_disk = json.load(open(tmp_path / "output" / "pulse.json"))
    assert on_disk["items"][0]["tier"] == "priority"
    assert on_disk["items"][0]["notified"] is False
    assert on_disk["notified_this_run"] is False


def test_the_artifact_records_the_banner_that_did_fire(tmp_path):
    """The mirror: build_items necessarily ran before the banner was attempted,
    so the artifact must carry the outcome rather than the guess it was built
    with — and must agree with the store the next run loads.
    """
    run(_deps(tmp_path))

    on_disk = json.load(open(tmp_path / "output" / "pulse.json"))
    assert on_disk["items"][0]["notified"] is True

    state = json.load(open(tmp_path / ".pulse_seen.json"))
    stored = state["seen"][on_disk["items"][0]["id"]]
    assert stored["notified"] is True


# --- Ruling 58: a degraded classifier surfaces through sources -------------

def test_a_degraded_classifier_is_reported_through_sources_and_stays_ok(tmp_path):
    """pulse_classify never raises: it returns all-panel verdicts carrying
    classification_failed. That is a degraded run, not a failed one.
    """
    candidates = [dict(CAND), dict(CAND, at="2026-09-09T18:04:00+00:00", text="Second")]
    deps = _deps(
        tmp_path,
        collect_webex=lambda **kw: ([dict(c) for c in candidates], "ok"),
        classify=lambda **kw: [
            {"tier": "panel", "trigger": "p1", "why": "Classification failed: boom",
             "draft_reply": None, "classification_failed": True}
            for _ in kw["candidates"]
        ],
    )
    payload = run(deps)

    assert payload["status"] == "ok"
    assert "classifier" in payload["sources"]
    assert payload["sources"]["classifier"] == (
        "degraded: the classifier failed; every message is shown but none was ranked"
    )
    assert len(payload["items"]) == len(candidates)
    assert [item["text"] for item in payload["items"]] == [c["text"] for c in candidates]


def test_a_healthy_classifier_adds_no_classifier_source_key(tmp_path):
    payload = run(_deps(tmp_path))
    assert "classifier" not in payload["sources"]


# --- Ruling 35: an unexpected exception still writes an artifact -----------

def test_a_poisoned_classifier_return_still_writes_a_failed_artifact(tmp_path):
    """Task 9 deliberately raises on hostile input rather than degrading.
    verdicts=None raises TypeError inside build_items, past the collection
    block, and silence must never read as success.
    """
    payload = run(_deps(tmp_path, classify=lambda **kw: None))
    assert payload["status"] == "failed"
    assert payload["reason"]

    on_disk = json.load(open(tmp_path / "output" / "pulse.json"))
    assert on_disk["status"] == "failed"
    assert on_disk["reason"] == payload["reason"]
    assert on_disk["day"] == "2026-09-09"


def test_a_corrupt_seen_store_still_writes_a_failed_artifact(tmp_path):
    """load_state guarantees the "seen" key exists, not that it is a dict."""
    state_path = tmp_path / ".pulse_seen.json"
    state_path.write_text(json.dumps({"day": "2026-09-09", "seen": "oops"}))

    payload = run(_deps(tmp_path))
    assert payload["status"] == "failed"
    assert json.load(open(tmp_path / "output" / "pulse.json"))["status"] == "failed"


def test_a_non_dict_verdict_still_writes_a_failed_artifact(tmp_path):
    payload = run(_deps(tmp_path, classify=lambda **kw: ["not a dict"]))
    assert payload["status"] == "failed"
    assert json.load(open(tmp_path / "output" / "pulse.json"))["status"] == "failed"


# --- One notification per run ---------------------------------------------

def test_notify_is_called_exactly_once_no_matter_how_many_priority_items(tmp_path):
    """pulse_notify.notify fires one banner per call, so 'one notification per
    run' is the orchestrator's obligation: call it at most once.
    """
    candidates = [
        dict(CAND, at=f"2026-09-09T18:0{i}:00+00:00", text=f"Ask {i}") for i in range(3)
    ]
    calls = []

    def counting_notify(items):
        calls.append(list(items))
        return bool(items)

    deps = _deps(
        tmp_path,
        collect_webex=lambda **kw: ([dict(c) for c in candidates], "ok"),
        classify=lambda **kw: [dict(PRIORITY_VERDICT) for _ in kw["candidates"]],
        notify=counting_notify,
    )
    payload = run(deps)

    assert len(calls) == 1
    assert len(calls[0]) == 3
    assert payload["notified_this_run"] is True


def test_notify_is_never_called_more_than_once_on_a_quiet_run(tmp_path):
    calls = []

    def counting_notify(items):
        calls.append(list(items))
        return bool(items)

    run(_deps(tmp_path, collect_webex=lambda **kw: ([], "ok"), notify=counting_notify))
    assert len(calls) <= 1


# --- Ruling 33: the local offset comes from the OS, now -------------------

def test_the_local_offset_is_the_offset_in_effect_right_now():
    """time.daylight is a build-time flag, not "is DST in effect now", so it
    returns the wrong offset for part of the year. Ben is US Eastern: that is a
    one-hour error in the local day boundary for eight months.
    """
    from datetime import datetime as dt

    from hourly_pulse import _local_offset_hours

    expected = dt.now().astimezone().utcoffset().total_seconds() / 3600
    assert _local_offset_hours() == expected


@pytest.mark.parametrize("bad", [
    "not a number", None, float("nan"), float("inf"), float("-inf"), 99999, [], object(),
])
def test_a_malformed_timezone_offset_still_writes_an_artifact(tmp_path, bad):
    """The offset is consumed before ruling 35's guard can cover anything — the
    guard needs `today` to write a failure artifact at all. So an unusable
    offset must degrade, not raise: raising leaves no artifact, and no artifact
    reads as a quiet hour.
    """
    payload = run(_deps(tmp_path, tz_offset_hours=bad))

    on_disk = json.load(open(tmp_path / "output" / "pulse.json"))
    assert "status" in on_disk
    assert on_disk["status"] == "ok"
    assert payload["status"] == "ok"
    assert on_disk["items"][0]["tier"] == "priority"


def test_a_malformed_offset_is_loud_on_stderr(tmp_path, capsys):
    run(_deps(tmp_path, tz_offset_hours="nonsense"))
    assert "tz_offset_hours" in capsys.readouterr().err


def test_a_malformed_offset_falls_back_to_the_utc_day_boundary(tmp_path):
    """UTC is the fallback because it cannot escalate anything: a day boundary
    off by hours only shifts when the artifact archives and the seen store
    resets. This pins that the fallback is really UTC and not the good offset,
    using a time where the two disagree about the date.
    """
    late = datetime(2026, 9, 9, 2, 15, 3, tzinfo=UTC)  # 22:15 on 09-08 at -4

    good_dir = tmp_path / "good"
    good_dir.mkdir()
    assert run(_deps(good_dir, now=late))["day"] == "2026-09-08"

    bad_dir = tmp_path / "bad"
    bad_dir.mkdir()
    assert run(_deps(bad_dir, now=late, tz_offset_hours="nonsense"))["day"] == "2026-09-09"


def test_a_valid_offset_is_still_honoured(tmp_path):
    """The coercion must not flatten every offset to UTC — that would silently
    move the day boundary for a correct caller.
    """
    late = datetime(2026, 9, 9, 2, 15, 3, tzinfo=UTC)
    assert run(_deps(tmp_path, now=late, tz_offset_hours=-4))["day"] == "2026-09-08"

    other = tmp_path / "other"
    other.mkdir()
    assert run(_deps(other, now=late, tz_offset_hours=5.5))["day"] == "2026-09-09"


def test_the_local_offset_follows_dst_rather_than_the_build_time_flag():
    """The discriminating half. A September-only assertion agrees with the
    buggy expression and proves nothing, so ask for both halves of the year.
    """
    import time

    from hourly_pulse import _local_offset_hours

    winter = datetime(2026, 1, 15, 12, 0)
    summer = datetime(2026, 7, 15, 12, 0)

    assert _local_offset_hours(winter) == \
        winter.astimezone().utcoffset().total_seconds() / 3600
    assert _local_offset_hours(summer) == \
        summer.astimezone().utcoffset().total_seconds() / 3600

    if not time.daylight:
        return  # a zone with no DST cannot exhibit the bug

    assert _local_offset_hours(winter) != _local_offset_hours(summer)

    # The brief's expression is a single constant for the whole year. It can
    # therefore match at most one of the two halves — which is the bug.
    build_flag_answer = -time.altzone / 3600 if time.daylight else -time.timezone / 3600
    matches_winter = _local_offset_hours(winter) == build_flag_answer
    matches_summer = _local_offset_hours(summer) == build_flag_answer
    assert matches_winter != matches_summer


# --- The artifact contract -----------------------------------------------

def test_the_window_and_sources_are_recorded_on_the_artifact(tmp_path):
    payload = run(_deps(tmp_path))
    assert payload["window"]["to"] == "2026-09-09T18:15:03+00:00"
    assert payload["window"]["from"] == "2026-09-09T17:15:03+00:00"
    assert payload["generated_at"] == "2026-09-09T18:15:03+00:00"
    assert payload["day"] == "2026-09-09"
    assert payload["sources"] == {"webex": "ok", "email": "ok", "calendar": "ok"}


def test_the_watermark_is_the_run_timestamp_and_bounds_the_next_window(tmp_path):
    run(_deps(tmp_path))
    assert (tmp_path / ".last_pulse_run").read_text() == "2026-09-09T18:15:03+00:00"

    seen_since = []

    def capture(**kw):
        seen_since.append(kw["since"])
        return []

    later = datetime(2026, 9, 9, 19, 15, 3, tzinfo=UTC)
    run(_deps(tmp_path, now=later, collect_webex=capture))
    assert seen_since[0] == NOW


def test_the_classifier_receives_the_briefing_horizon_for_the_local_hour(tmp_path):
    seen = {}

    def capture(**kw):
        seen.update(kw)
        return [dict(PRIORITY_VERDICT)]

    # 18:15 UTC at -4 is 14:15 local — before 16:00, so the next briefing is
    # today at 16:00 and the bar is "a few hours".
    run(_deps(tmp_path, classify=capture))
    assert seen["briefing_label"] == "today 16:00"
    assert seen["briefing_horizon"] == "a few hours"

    evening = datetime(2026, 9, 9, 21, 15, 3, tzinfo=UTC)  # 17:15 local
    run(_deps(tmp_path, now=evening, classify=capture))
    assert seen["briefing_label"] == "tomorrow 08:30"
    assert seen["briefing_horizon"] == "overnight"


def test_email_candidates_are_classified_alongside_webex_candidates(tmp_path):
    email_cand = {
        "source": "email", "channel": "Inbox", "space_id": None,
        "from_name": "Taylor", "from_email": "taylor@cisco.com",
        "at": "2026-09-09T18:05:00+00:00", "text": "EOD?",
        "link": None, "tier_hint": "email",
    }
    deps = _deps(
        tmp_path,
        collect_email=lambda **kw: ([dict(email_cand)], "ok"),
        classify=lambda **kw: [dict(PRIORITY_VERDICT) for _ in kw["candidates"]],
    )
    payload = run(deps)
    assert [item["source"] for item in payload["items"]] == ["webex", "email"]


def test_the_calendar_meetings_reach_the_classifier(tmp_path):
    meetings = [{"subject": "EC pre-read", "start": "2026-09-09T18:30:00+00:00"}]
    seen = {}

    def capture(**kw):
        seen.update(kw)
        return [dict(PRIORITY_VERDICT)]

    run(_deps(
        tmp_path,
        collect_calendar=lambda **kw: (list(meetings), "ok"),
        classify=capture,
    ))
    assert seen["meetings"] == meetings
