import json
import os

import pytest

from lib.pulse_output import (
    build_items,
    build_payload,
    failure_payload,
    write_payload,
    archive_if_new_day,
)

NOW = "2026-09-09T18:15:03+00:00"

CANDIDATES = [{
    "source": "webex", "channel": "C3 + CUI", "space_id": "abc",
    "from_name": "Rory Scott", "from_email": "rorscott@cisco.com",
    "at": "2026-09-09T14:02:11+00:00", "text": "Confirm before the pre-read?",
    "link": "https://web.webex.com/spaces/abc",
    "is_watchlist": True, "is_direct_mention": False, "is_watched_thread": False,
    "tier_hint": "p1",
}]
VERDICTS = [{"tier": "priority", "trigger": "watchlist", "why": "Watchlist ask.",
             "draft_reply": "On it."}]


def test_item_carries_the_verbatim_text_not_a_summary():
    items = build_items(CANDIDATES, VERDICTS, {"day": "2026-09-09", "seen": {}}, NOW)
    assert items[0]["text"] == "Confirm before the pre-read?"


def test_item_shape_matches_the_contract():
    item = build_items(CANDIDATES, VERDICTS, {"day": "2026-09-09", "seen": {}}, NOW)[0]
    for key in ("id", "tier", "source", "trigger", "channel", "from", "at", "text",
                "why", "draft_reply", "link", "first_seen", "notified", "resolved"):
        assert key in item, key
    assert item["from"] == {"name": "Rory Scott", "email": "rorscott@cisco.com"}


def test_a_brand_new_priority_item_is_not_yet_marked_notified():
    """`notified` is history, not a prediction.

    build_items runs BEFORE the notification is attempted, so inferring
    notified from `tier == "priority"` would claim an interruption that has not
    happened — and would keep claiming it on a run where the banner failed.
    The orchestrator sets this true afterwards, only if a banner really fired.
    """
    items = build_items(CANDIDATES, VERDICTS, {"day": "2026-09-09", "seen": {}}, NOW)
    assert items[0]["tier"] == "priority"
    assert items[0]["notified"] is False
    assert items[0]["first_seen"] == NOW


def test_a_panel_item_is_never_marked_notified():
    verdicts = [{"tier": "panel", "trigger": "p1_channel", "why": "FYI", "draft_reply": None}]
    items = build_items(CANDIDATES, verdicts, {"day": "2026-09-09", "seen": {}}, NOW)
    assert items[0]["notified"] is False


def test_a_priority_item_notified_on_an_earlier_run_stays_notified():
    from lib.pulse_state import fingerprint
    fid = fingerprint(CANDIDATES[0])
    state = {"day": "2026-09-09", "seen": {fid: {"first_seen": "earlier", "notified": True}}}
    items = build_items(CANDIDATES, VERDICTS, state, NOW)
    assert items[0]["notified"] is True


def test_a_prior_entry_that_was_never_notified_stays_not_notified():
    """The failed-banner case: the prior run recorded notified=False, and
    build_items must not upgrade it just because the tier is priority.
    """
    from lib.pulse_state import fingerprint
    fid = fingerprint(CANDIDATES[0])
    state = {"day": "2026-09-09", "seen": {fid: {"first_seen": "earlier", "notified": False}}}
    items = build_items(CANDIDATES, VERDICTS, state, NOW)
    assert items[0]["notified"] is False


def test_an_already_seen_item_keeps_its_original_first_seen():
    from lib.pulse_state import fingerprint
    fid = fingerprint(CANDIDATES[0])
    state = {"day": "2026-09-09", "seen": {fid: {"first_seen": "earlier", "notified": True}}}
    items = build_items(CANDIDATES, VERDICTS, state, NOW)
    assert items[0]["first_seen"] == "earlier"


def test_payload_reports_sources_and_window():
    payload = build_payload([], {"webex": "ok", "email": "degraded: x"},
                            "2026-09-09T17:15:00+00:00", NOW, NOW, notified=False)
    assert payload["sources"]["email"] == "degraded: x"
    assert payload["window"]["from"] == "2026-09-09T17:15:00+00:00"
    assert payload["status"] == "ok"
    assert payload["notified_this_run"] is False


def test_payload_uses_the_local_day_when_one_is_given():
    # 01:15 UTC on the 10th is still the evening of the 9th in Ben's timezone.
    payload = build_payload([], {}, NOW, NOW, "2026-09-10T01:15:00+00:00",
                            notified=False, day="2026-09-09")
    assert payload["day"] == "2026-09-09"


def test_payload_falls_back_to_the_utc_day_when_none_is_given():
    payload = build_payload([], {}, NOW, NOW, NOW, notified=False)
    assert payload["day"] == "2026-09-09"


def test_failure_payload_says_it_failed_rather_than_looking_empty():
    payload = failure_payload("network unreachable", NOW)
    assert payload["status"] == "failed"
    assert "network unreachable" in payload["reason"]
    assert payload["items"] == []


def test_write_then_read_round_trips(tmp_path):
    payload = build_payload([], {"webex": "ok"}, NOW, NOW, NOW, notified=False)
    path = write_payload(payload, str(tmp_path))
    assert os.path.basename(path) == "pulse.json"
    assert json.load(open(path))["status"] == "ok"


def test_archive_moves_yesterdays_artifact_aside(tmp_path):
    out = str(tmp_path)
    write_payload(
        build_payload([], {}, NOW, NOW, NOW, notified=False, day="2026-09-08"), out
    )
    archived = archive_if_new_day(out, "2026-09-09")
    assert archived and archived.endswith("pulse-2026-09-08.json")
    assert not os.path.exists(os.path.join(out, "pulse.json"))


def test_archive_is_a_noop_within_the_same_day(tmp_path):
    out = str(tmp_path)
    json.dump({"day": "2026-09-09"}, open(os.path.join(out, "pulse.json"), "w"))
    assert archive_if_new_day(out, "2026-09-09") is None
    assert os.path.exists(os.path.join(out, "pulse.json"))


def test_archive_is_a_noop_when_there_is_nothing_to_archive(tmp_path):
    assert archive_if_new_day(str(tmp_path), "2026-09-09") is None


# --- write_payload's atomic-write guarantee ---

def test_a_failed_write_leaves_the_previous_artifact_intact(tmp_path, monkeypatch):
    out = str(tmp_path)
    write_payload(
        build_payload([], {"webex": "ok"}, NOW, NOW, NOW, notified=False, day="2026-09-08"),
        out,
    )

    def boom(*a, **kw):
        raise RuntimeError("disk full")

    monkeypatch.setattr("lib.pulse_output.json.dump", boom)
    with pytest.raises(RuntimeError):
        write_payload(
            build_payload([], {}, NOW, NOW, NOW, notified=False, day="2026-09-09"), out
        )

    surviving = json.load(open(os.path.join(out, "pulse.json")))
    assert surviving["day"] == "2026-09-08"


# --- F12: two overlapping runs must not share one temp name ----------------
#
# StartCalendarInterval deliberately permits overlapping runs and nothing bounds
# a run's duration — there is no timeout on the Bedrock call or the Webex HTTP
# calls. Two concurrent writers truncating the same "pulse.json.tmp" can
# os.replace mixed bytes into place. Ruling 79 gave the *wrapper* a distinct temp
# name for exactly this reason; python-vs-python was left exposed.


def test_the_payload_temp_name_is_distinct_per_process(tmp_path, monkeypatch):
    captured = []
    real_replace = os.replace

    def spy(src, dst):
        captured.append(src)
        real_replace(src, dst)

    monkeypatch.setattr("lib.pulse_output.os.replace", spy)
    write_payload(build_payload([], {"webex": "ok"}, NOW, NOW, NOW, notified=False),
                  str(tmp_path))

    assert len(captured) == 1
    name = os.path.basename(captured[0])
    assert name != "pulse.json.tmp", "a shared temp name is the collision"
    assert str(os.getpid()) in name


def test_a_successful_write_leaves_no_tmp_file_behind(tmp_path):
    out = str(tmp_path)
    write_payload(build_payload([], {"webex": "ok"}, NOW, NOW, NOW, notified=False), out)
    leftovers = [f for f in os.listdir(out) if f.endswith(".tmp")]
    assert leftovers == []


# --- Ruling 1: build_items must not silently truncate on a short verdicts list ---

def test_build_items_pads_a_missing_verdict_with_a_visible_panel_item():
    second_candidate = {
        "source": "webex", "channel": "Other Space", "space_id": "def",
        "from_name": "Jane Doe", "from_email": "jdoe@cisco.com",
        "at": "2026-09-09T14:05:00+00:00", "text": "Second message, no verdict for this one",
        "link": "https://web.webex.com/spaces/def",
        "is_watchlist": False, "is_direct_mention": False, "is_watched_thread": False,
        "tier_hint": "p2",
    }
    candidates = CANDIDATES + [second_candidate]
    # Only one verdict for two candidates — the classifier dropped the tail.
    items = build_items(candidates, VERDICTS, {"day": "2026-09-09", "seen": {}}, NOW)
    assert len(items) == 2
    assert items[1]["tier"] == "panel"
    assert items[1]["why"]


def test_build_items_ignores_verdicts_beyond_the_candidate_count():
    extra_verdicts = VERDICTS + [
        {"tier": "priority", "trigger": "watchlist", "why": "phantom", "draft_reply": None}
    ]
    items = build_items(CANDIDATES, extra_verdicts, {"day": "2026-09-09", "seen": {}}, NOW)
    assert len(items) == 1


# --- Ruling 2: classification_failed propagates additively, never as False ---

def test_classification_failed_flag_propagates_from_verdict_to_item():
    verdicts = [{
        "tier": "panel", "trigger": "p1", "why": "Classification failed: boom",
        "draft_reply": None, "classification_failed": True,
    }]
    items = build_items(CANDIDATES, verdicts, {"day": "2026-09-09", "seen": {}}, NOW)
    assert items[0]["classification_failed"] is True


def test_classification_failed_key_is_absent_on_a_normal_verdict():
    items = build_items(CANDIDATES, VERDICTS, {"day": "2026-09-09", "seen": {}}, NOW)
    assert "classification_failed" not in items[0]
