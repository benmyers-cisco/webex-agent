import json
import os

import pytest

from lib.pulse_output import (
    build_items,
    partition_dropped,
    build_payload,
    failure_payload,
    write_payload,
    archive_if_new_day,
    carry_forward,
    state_entry,
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


# --- The relevance filter -------------------------------------------------


def test_partition_dropped_splits_on_the_drop_tier():
    items = [
        {"id": "a", "tier": "priority"},
        {"id": "b", "tier": "drop"},
        {"id": "c", "tier": "panel"},
        {"id": "d", "tier": "drop"},
    ]
    kept, dropped = partition_dropped(items)
    assert [i["id"] for i in kept] == ["a", "c"]
    assert [i["id"] for i in dropped] == ["b", "d"]


def test_build_items_still_returns_every_candidate_including_drops():
    # The invariant that partition_dropped exists to protect: filtering inside
    # build_items would make "fewer items than candidates" mean two things.
    candidates = [{"source": "email", "text": "newsletter", "at": "2026-09-10T14:00:00+00:00"}]
    verdicts = [{"tier": "drop", "trigger": "irrelevant", "why": "vendor marketing"}]
    items = build_items(candidates, verdicts, {"seen": {}}, "2026-09-10T14:05:00+00:00")
    assert len(items) == 1
    assert items[0]["tier"] == "drop"


def test_the_payload_reports_how_many_were_filtered():
    # An aggressive filter must never be able to pass for a quiet hour.
    payload = build_payload(
        [], {}, "2026-09-10T13:00:00+00:00", "2026-09-10T14:00:00+00:00",
        "2026-09-10T14:00:00+00:00", False, day="2026-09-10", filtered=14,
    )
    assert payload["filtered"] == 14


def test_a_failed_run_reports_zero_filtered_rather_than_omitting_the_key():
    payload = failure_payload("boom", "2026-09-10T14:00:00+00:00")
    assert payload["filtered"] == 0


# --- state_entry() / carry_forward() — the artifact is a view of the day, not
# of the last hour. See the 2026-09-15 case in carry_forward's docstring. ---

PANEL_VERDICTS = [{"tier": "panel", "trigger": "dm", "why": "Asks to confirm.",
                   "draft_reply": None}]
DROP_VERDICTS = [{"tier": "drop", "trigger": "irrelevant", "why": "Newsletter.",
                  "draft_reply": None}]


def _item(tier="panel", **over):
    verdicts = PANEL_VERDICTS if tier == "panel" else VERDICTS
    item = build_items(CANDIDATES, verdicts, {"day": "2026-09-09", "seen": {}}, NOW)[0]
    item.update(over)
    return item


def _store(item, **over):
    entry = state_entry(item)
    entry.update(over)
    return {"day": "2026-09-09", "seen": {item["id"]: entry}}


def test_build_items_carries_the_space_id():
    """carry_forward rebuilds from the stored item alone, and resolve_answered
    needs to know which space to ask about."""
    assert build_items(CANDIDATES, VERDICTS, {"day": "2026-09-09", "seen": {}}, NOW)[0][
        "space_id"
    ] == "abc"


def test_a_panel_item_stores_its_whole_payload():
    entry = state_entry(_item())
    assert entry["item"]["text"] == "Confirm before the pre-read?"
    assert entry["tier"] == "panel"


def test_a_dropped_item_stores_only_its_flags():
    """It never reaches the artifact, so a payload for it is dead weight in a
    file every run reads — and its absence is what makes it uncarryable."""
    dropped = build_items(CANDIDATES, DROP_VERDICTS, {"day": "2026-09-09", "seen": {}}, NOW)[0]
    entry = state_entry(dropped)
    assert "item" not in entry
    assert entry["tier"] == "drop"


def test_the_stored_payload_is_a_copy_not_a_reference():
    item = _item()
    entry = state_entry(item)
    item["text"] = "mutated after the store was built"
    assert entry["item"]["text"] == "Confirm before the pre-read?"


def test_an_unresolved_panel_item_is_carried_into_a_later_run():
    carried = carry_forward(_store(_item()), fresh_ids=set())
    assert len(carried) == 1
    assert carried[0]["text"] == "Confirm before the pre-read?"
    assert carried[0]["carried_forward"] is True
    assert carried[0]["first_seen"] == NOW


def test_a_carried_item_keeps_its_original_tier_and_is_never_escalated():
    """Restoration, not re-judgement. Omission must never escalate, and neither
    may age — an item that could wait at 13:15 has not become urgent by 14:15."""
    carried = carry_forward(_store(_item()), fresh_ids=set())
    assert carried[0]["tier"] == "panel"


def test_an_item_this_run_collected_again_is_not_also_carried():
    """The fresh copy has this run's verdict and `why`. Carrying it too would
    show Ben the same message twice on one panel."""
    item = _item()
    assert carry_forward(_store(item), fresh_ids={item["id"]}) == []


def test_a_resolved_item_is_not_carried():
    assert carry_forward(_store(_item(), resolved=True), fresh_ids=set()) == []


def test_an_item_resolved_this_run_is_not_carried():
    item = _item()
    assert carry_forward(_store(item), fresh_ids=set(), resolved_ids={item["id"]}) == []


def test_an_entry_with_no_stored_payload_is_skipped():
    """A store written before carry_forward existed, or a dropped item. There is
    nothing to rebuild, and a placeholder would put a hollow row on a glance
    surface."""
    state = {"day": "2026-09-09", "seen": {
        "old": {"first_seen": NOW, "notified": False, "resolved": False, "tier": "panel"},
    }}
    assert carry_forward(state, fresh_ids=set()) == []


def test_a_drop_tier_entry_is_never_carried_even_with_a_payload():
    item = _item()
    state = _store(item, tier="drop")
    assert carry_forward(state, fresh_ids=set()) == []


def test_the_store_flags_win_over_the_payload_snapshot():
    """The embedded copy is a snapshot of the run that wrote it; the top-level
    flags are what later runs update in place. A banner that fired on run two
    must not read as un-notified on run three."""
    item = _item(tier="priority")
    assert item["notified"] is False
    carried = carry_forward(_store(item, notified=True), fresh_ids=set())
    assert carried[0]["notified"] is True


def test_a_malformed_store_entry_does_not_break_the_run():
    state = {"day": "2026-09-09", "seen": {"junk": "not a dict", "also": None}}
    assert carry_forward(state, fresh_ids=set()) == []


def test_an_empty_store_carries_nothing():
    assert carry_forward({"day": "2026-09-09", "seen": {}}, fresh_ids=set()) == []
    assert carry_forward({}, fresh_ids=set()) == []


def test_the_payload_reports_how_many_items_were_carried():
    payload = build_payload([], {}, "f", "t", NOW, False, carried=3)
    assert payload["carried"] == 3


def test_the_payload_defaults_carried_to_zero():
    assert build_payload([], {}, "f", "t", NOW, False)["carried"] == 0


def test_a_failed_run_reports_nothing_carried():
    """A failed run does not get to speak for earlier ones."""
    assert failure_payload("boom", NOW)["carried"] == 0
