import json
import re

import pytest

from lib.pulse_classify import build_prompt, parse_response, classify

CANDIDATES = [
    {
        "channel": "C3 + CUI", "from_name": "Rory Scott", "from_email": "rorscott@cisco.com",
        "at": "2026-09-09T14:02:11+00:00", "text": "Can you confirm before the pre-read?",
        "is_watchlist": True, "is_direct_mention": False, "is_watched_thread": False,
        "tier_hint": "p1", "source": "webex",
    },
    {
        "channel": "SCC - CII Discussion", "from_name": "Someone", "from_email": "s@cisco.com",
        "at": "2026-09-09T14:05:00+00:00", "text": "FYI the doc moved",
        "is_watchlist": False, "is_direct_mention": False, "is_watched_thread": False,
        "tier_hint": "p2", "source": "webex",
    },
]


def test_prompt_states_the_actual_test_verbatim():
    prompt = build_prompt(CANDIDATES, "prefs", "2026-09-09T18:15:00+00:00",
                          "today 16:00", "a few hours", [])
    assert "can this wait until today 16:00" in prompt.lower()
    assert "a few hours" in prompt


def _candidate_block(prompt: str, index: int) -> str:
    """Slice out just one candidate's rendered block, so flag assertions
    can't be satisfied by the static rule text elsewhere in the prompt."""
    marker = f"[{index}] "
    start = prompt.index(marker) + len(marker)
    rest = prompt[start:]
    next_marker = re.search(r"\n\[\d+\] ", rest)
    end = next_marker.start() if next_marker else rest.find("\nRespond with JSON only")
    if end == -1:
        end = len(rest)
    return rest[:end]


# Candidate 0 has every flag; candidate 1 has none — lets each test assert
# both presence (in the flagged block) and absence (in the unflagged one).
FLAG_CANDIDATES = [
    {
        "channel": "X", "from_name": "A", "from_email": "a@cisco.com", "at": "n",
        "text": "flagged", "is_watchlist": True, "is_direct_mention": True,
        "is_watched_thread": True, "tier_hint": "p1", "source": "webex",
    },
    {
        "channel": "Y", "from_name": "B", "from_email": "b@cisco.com", "at": "n",
        "text": "unflagged", "is_watchlist": False, "is_direct_mention": False,
        "is_watched_thread": False, "tier_hint": "p2", "source": "webex",
    },
]


def test_prompt_carries_the_watchlist_flag_within_its_own_candidate_block_only():
    prompt = build_prompt(FLAG_CANDIDATES, "prefs", "n", "today 16:00", "a few hours", [])
    assert "WATCHLIST" in _candidate_block(prompt, 0)
    assert "WATCHLIST" not in _candidate_block(prompt, 1)


def test_prompt_numbers_candidates_so_responses_can_be_matched_back():
    prompt = build_prompt(CANDIDATES, "prefs", "n", "today 16:00", "a few hours", [])
    assert "[0]" in prompt and "[1]" in prompt


def test_prompt_lists_imminent_meetings_when_present():
    prompt = build_prompt(CANDIDATES, "prefs", "n", "today 16:00", "a few hours",
                          [{"subject": "Cert sync", "start": "2026-09-09T19:00:00+00:00"}])
    assert "Cert sync" in prompt


def test_prompt_omits_the_meeting_block_when_there_are_none():
    prompt = build_prompt(CANDIDATES, "prefs", "n", "today 16:00", "a few hours", [])
    assert "Meetings starting soon" not in prompt


def test_parse_response_assigns_tier_and_reason():
    raw = json.dumps({"items": [
        {"index": 0, "tier": "priority", "trigger": "watchlist",
         "why": "Watchlist sender with a deadline-bound ask.", "draft_reply": "On it."},
        {"index": 1, "tier": "panel", "trigger": "p2_channel", "why": "FYI only."},
    ]})
    out = parse_response(raw, CANDIDATES)
    assert out[0]["tier"] == "priority"
    assert out[0]["trigger"] == "watchlist"
    assert out[0]["draft_reply"] == "On it."
    assert out[1]["tier"] == "panel"


def test_parse_response_tolerates_prose_around_the_json():
    raw = 'Sure, here you go:\n```json\n{"items": [{"index": 0, "tier": "panel", "why": "x"}]}\n```\nDone.'
    out = parse_response(raw, CANDIDATES[:1])
    assert out[0]["tier"] == "panel"


def test_unmentioned_candidates_default_to_panel_never_to_priority():
    # A model that forgets a candidate must not silently escalate it.
    raw = json.dumps({"items": [{"index": 0, "tier": "priority", "why": "x"}]})
    out = parse_response(raw, CANDIDATES)
    assert out[1]["tier"] == "panel"
    assert "not classified" in out[1]["why"].lower()


def test_an_out_of_range_index_is_ignored():
    raw = json.dumps({"items": [{"index": 99, "tier": "priority", "why": "x"}]})
    out = parse_response(raw, CANDIDATES)
    assert all(item["tier"] == "panel" for item in out)


def test_an_unknown_tier_value_falls_back_to_panel():
    raw = json.dumps({"items": [{"index": 0, "tier": "URGENT!!", "why": "x"}]})
    assert parse_response(raw, CANDIDATES)[0]["tier"] == "panel"


def test_unparseable_response_raises_so_the_run_reports_failure():
    with pytest.raises(ValueError):
        parse_response("the model said nothing useful", CANDIDATES)


def test_classify_returns_an_empty_list_without_calling_the_model():
    calls = []

    class Spy:
        class messages:
            @staticmethod
            def create(**kw):
                calls.append(kw)
                raise AssertionError("must not be called")

    assert classify(Spy(), [], "prefs", "n", "today 16:00", "a few hours", []) == []
    assert calls == []


# --- build_prompt: prove the rendered prompt actually contains what it claims ---

def test_prompt_contains_candidate_text_and_prefs_text():
    prompt = build_prompt(CANDIDATES, "PREFS_SENTINEL_XYZ", "n", "today 16:00", "a few hours", [])
    assert "Can you confirm before the pre-read?" in prompt
    assert "PREFS_SENTINEL_XYZ" in prompt


def test_prompt_carries_direct_mention_and_watched_thread_flags_within_their_own_block_only():
    prompt = build_prompt(FLAG_CANDIDATES, "prefs", "n", "today 16:00", "a few hours", [])
    flagged_block = _candidate_block(prompt, 0)
    unflagged_block = _candidate_block(prompt, 1)
    assert "DIRECT-MENTION" in flagged_block
    assert "THREAD-YOU-ARE-IN" in flagged_block
    assert "DIRECT-MENTION" not in unflagged_block
    assert "THREAD-YOU-ARE-IN" not in unflagged_block


def test_prompt_renders_with_no_candidates_and_no_meetings():
    prompt = build_prompt([], "prefs", "n", "today 16:00", "a few hours", [])
    assert isinstance(prompt, str) and prompt


# --- parse_response: hostile input must never raise anything but ValueError,
# and must never manufacture a priority ---

@pytest.mark.parametrize("raw", [
    "",
    "   ",
    "not json at all",
    "{not: valid json}",
    None,
])
def test_parse_response_hostile_unparseable_inputs_raise_valueerror_only(raw):
    with pytest.raises(ValueError):
        parse_response(raw, CANDIDATES)


def test_parse_response_items_missing_defaults_everything_to_panel():
    raw = json.dumps({"no_items_key": True})
    out = parse_response(raw, CANDIDATES)
    assert all(item["tier"] == "panel" for item in out)
    assert len(out) == len(CANDIDATES)


@pytest.mark.parametrize("items_value", ["a string", 42, {"index": 0}, None, True])
def test_parse_response_items_not_a_list_does_not_raise(items_value):
    raw = json.dumps({"items": items_value})
    out = parse_response(raw, CANDIDATES)
    assert all(item["tier"] == "panel" for item in out)


def test_parse_response_non_dict_entries_in_items_are_skipped_not_fatal():
    raw = json.dumps({"items": ["oops", 1, None, {"index": 0, "tier": "priority", "why": "x"}]})
    out = parse_response(raw, CANDIDATES)
    assert out[0]["tier"] == "priority"
    assert out[1]["tier"] == "panel"


def test_parse_response_negative_index_is_ignored():
    raw = json.dumps({"items": [{"index": -1, "tier": "priority", "why": "x"}]})
    out = parse_response(raw, CANDIDATES)
    assert all(item["tier"] == "panel" for item in out)


def test_parse_response_string_index_is_ignored():
    raw = json.dumps({"items": [{"index": "0", "tier": "priority", "why": "x"}]})
    out = parse_response(raw, CANDIDATES)
    assert all(item["tier"] == "panel" for item in out)


def test_parse_response_missing_tier_key_defaults_to_panel():
    raw = json.dumps({"items": [{"index": 0, "why": "no tier given"}]})
    out = parse_response(raw, CANDIDATES)
    assert out[0]["tier"] == "panel"


def test_parse_response_duplicated_index_does_not_raise():
    raw = json.dumps({"items": [
        {"index": 0, "tier": "panel", "why": "first"},
        {"index": 0, "tier": "priority", "why": "second"},
    ]})
    out = parse_response(raw, CANDIDATES)
    assert out[0]["tier"] in ("panel", "priority")


# --- classify: must degrade, never raise, on any model-call failure ---

class _RaisingClient:
    class messages:
        @staticmethod
        def create(**kw):
            raise TimeoutError("model call timed out")


class _EmptyContentClient:
    class _Response:
        content = []

    class messages:
        @staticmethod
        def create(**kw):
            return _EmptyContentClient._Response()


class _WeirdShapeClient:
    class _Block:
        pass  # no .text attribute at all

    class messages:
        @staticmethod
        def create(**kw):
            class Response:
                pass
            resp = Response()
            resp.content = [_WeirdShapeClient._Block()]
            return resp


class _GarbageTextClient:
    class _Block:
        text = "the model said nothing useful"

    class messages:
        @staticmethod
        def create(**kw):
            class Response:
                pass
            resp = Response()
            resp.content = [_GarbageTextClient._Block()]
            return resp


@pytest.mark.parametrize("client_cls", [
    _RaisingClient, _EmptyContentClient, _WeirdShapeClient, _GarbageTextClient,
])
def test_classify_degrades_to_all_panel_never_raises(client_cls):
    out = classify(client_cls(), CANDIDATES, "prefs", "n", "today 16:00", "a few hours", [])
    assert len(out) == len(CANDIDATES)
    assert all(item["tier"] == "panel" for item in out)


def test_classify_degrade_reports_the_failure_in_why():
    out = classify(_RaisingClient(), CANDIDATES, "prefs", "n", "today 16:00", "a few hours", [])
    assert any("fail" in item["why"].lower() for item in out)


# --- classification_failed marker: machine-readable, additive-only ---

@pytest.mark.parametrize("client_cls", [
    _RaisingClient, _EmptyContentClient, _WeirdShapeClient, _GarbageTextClient,
])
def test_classify_degrade_sets_classification_failed_marker_on_every_item(client_cls):
    out = classify(client_cls(), CANDIDATES, "prefs", "n", "today 16:00", "a few hours", [])
    assert len(out) == len(CANDIDATES)
    assert all(item.get("classification_failed") is True for item in out)


def test_classify_success_never_carries_the_classification_failed_key():
    class _OkClient:
        class _Block:
            text = json.dumps({"items": [
                {"index": 0, "tier": "priority", "why": "x"},
                {"index": 1, "tier": "panel", "why": "y"},
            ]})

        class messages:
            @staticmethod
            def create(**kw):
                class Response:
                    pass
                resp = Response()
                resp.content = [_OkClient._Block()]
                return resp

    out = classify(_OkClient(), CANDIDATES, "prefs", "n", "today 16:00", "a few hours", [])
    assert len(out) == len(CANDIDATES)
    for item in out:
        assert "classification_failed" not in item


# --- The relevance verdict -------------------------------------------------


def test_drop_is_a_verdict_the_model_can_return():
    raw = json.dumps({"items": [
        {"index": 0, "tier": "drop", "trigger": "irrelevant",
         "why": "Dark Reading newsletter — industry news, no bearing on Ben's work."},
    ]})
    assert parse_response(raw, CANDIDATES)[0]["tier"] == "drop"


def test_an_omitted_candidate_defaults_to_panel_and_is_never_dropped():
    # Omission must not silence, any more than it may escalate. Only the
    # visible middle is a safe default, because both failure modes at the
    # edges are invisible.
    raw = json.dumps({"items": [{"index": 0, "tier": "drop", "why": "x"}]})
    out = parse_response(raw, CANDIDATES)
    assert out[1]["tier"] == "panel"


def test_a_degraded_classifier_cannot_drop_anything():
    # This is what makes a model outage loud instead of quiet: every verdict is
    # panel, so partition_dropped removes nothing and Ben sees the lot.
    class Boom:
        class messages:
            @staticmethod
            def create(**_kw):
                raise RuntimeError("bedrock is down")

    out = classify(Boom(), CANDIDATES, "prefs", "n", "today 16:00", "a few hours", [])
    assert [v["tier"] for v in out] == ["panel"] * len(CANDIDATES)


def test_the_prompt_names_the_noise_ben_called_out_and_the_safe_default():
    prompt = build_prompt(CANDIDATES, "prefs", "n", "today 16:00", "a few hours", [])
    low = prompt.lower()
    assert "dark reading" in low
    assert "webinar" in low
    # The asymmetry has to be stated, or the model treats the two errors as
    # equally bad: a wrongly-panelled item costs a glance, a wrongly-dropped
    # one is invisible.
    assert "wrongly-dropped" in low
    # A colleague's question is never droppable, whatever else the mail says.
    assert "never dropped" in low
