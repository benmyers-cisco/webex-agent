import json

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


def test_prompt_carries_the_watchlist_flag_per_candidate():
    prompt = build_prompt(CANDIDATES, "prefs", "n", "today 16:00", "a few hours", [])
    assert "WATCHLIST" in prompt


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


def test_prompt_carries_direct_mention_and_watched_thread_flags():
    flagged = [{
        "channel": "C3 + CUI", "from_name": "X", "from_email": "x@cisco.com",
        "at": "n", "text": "hi", "is_watchlist": False, "is_direct_mention": True,
        "is_watched_thread": True, "tier_hint": "p2", "source": "webex",
    }]
    prompt = build_prompt(flagged, "prefs", "n", "today 16:00", "a few hours", [])
    assert "DIRECT-MENTION" in prompt
    assert "THREAD-YOU-ARE-IN" in prompt


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
