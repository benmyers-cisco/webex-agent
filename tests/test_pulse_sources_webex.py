from datetime import datetime, timezone

from lib.pulse_prefs import parse_prefs
from lib.pulse_sources_webex import collect, mentions_ben, space_is_eligible, to_candidate

PREFS = parse_prefs("""
## Watchlist
- Rory Scott <rorscott@cisco.com>

### Priority 1 — Interrupt Me
- C3 + CUI

### Priority 2 — Tagged Only
- SCC - CII Discussion
""")

MY_EMAIL = "benmyers@cisco.com"
MY_NAMES = ["Ben Myers", "Ben"]


def test_mention_by_email_is_detected():
    assert mentions_ben("cc benmyers@cisco.com please look", MY_EMAIL, MY_NAMES) is True


def test_mention_by_display_name_is_detected():
    assert mentions_ben("Ben Myers can you confirm?", MY_EMAIL, MY_NAMES) is True


def test_bare_first_name_alone_is_not_a_mention():
    # "Ben" appears in "Ben Gaspar" and in ordinary prose. Too loose to trust.
    assert mentions_ben("Ben Gaspar shared the doc", MY_EMAIL, MY_NAMES) is False


def test_unrelated_text_is_not_a_mention():
    assert mentions_ben("deploy finished", MY_EMAIL, MY_NAMES) is False


def test_multiword_name_does_not_match_as_a_substring_of_a_longer_name():
    # "Ben Myers" must not match inside "Ben Myerson" — that's what the \b
    # word-boundary guards against. Without it, this is a substring hit.
    assert mentions_ben("Ben Myerson signed off on it", MY_EMAIL, MY_NAMES) is False


def test_p1_space_is_eligible():
    ok, tier = space_is_eligible({"title": "C3 + CUI", "type": "group", "id": "1"}, PREFS, {})
    assert (ok, tier) == (True, "p1")


def test_p2_space_is_eligible_and_tagged_as_p2():
    ok, tier = space_is_eligible(
        {"title": "SCC - CII Discussion", "type": "group", "id": "2"}, PREFS, {}
    )
    assert (ok, tier) == (True, "p2")


def test_direct_space_is_eligible_as_dm():
    ok, tier = space_is_eligible({"title": "Rory Scott", "type": "direct", "id": "3"}, PREFS, {})
    assert (ok, tier) == (True, "dm")


def test_unlisted_space_is_skipped():
    ok, tier = space_is_eligible({"title": "Random Chat", "type": "group", "id": "4"}, PREFS, {})
    assert ok is False


def test_unlisted_space_with_a_watched_thread_is_eligible():
    ok, tier = space_is_eligible(
        {"title": "Random Chat", "type": "group", "id": "4"}, PREFS, {"4": ["thread-1"]}
    )
    assert (ok, tier) == (True, "p2")


# --- F16: the spec says DMs AND GROUP CHATS are treated as P1 --------------
#
# spec :102 and decision log :431. Only `type == "direct"` was special-cased, so
# a Webex ad-hoc group chat — `type: "group"` with a participant-name title —
# fell through tier_of() to "unlisted" and was skipped. The migration then
# deleted the one such entry from preferences.md on the grounds that group chats
# are scanned unconditionally, which is true of daily_summary and was false
# here, so that space went from watched to invisible.


def test_a_group_chat_is_eligible_as_a_dm():
    ok, tier = space_is_eligible(
        {"title": "Aamir Yousufzai, Ben Gaspar, Mike Wojan", "type": "group", "id": "g"},
        PREFS, {},
    )
    assert (ok, tier) == (True, "dm")


def test_a_two_person_group_chat_is_eligible_as_a_dm():
    ok, tier = space_is_eligible(
        {"title": "Di Yin Lu, Adam Greer", "type": "group", "id": "g2"}, PREFS, {}
    )
    assert (ok, tier) == (True, "dm")


def test_a_comma_bearing_channel_name_is_not_mistaken_for_a_group_chat():
    """The rule is a title heuristic, so its false positives matter: a listed
    channel name containing a comma must not be promoted to DM treatment.
    """
    ok, tier = space_is_eligible(
        {"title": "AI Canvas, UAIA, C3 - Product Integration Execution (PM/Eng)",
         "type": "group", "id": "g3"},
        PREFS, {},
    )
    assert (ok, tier) == (False, "unlisted")


def test_the_group_chat_rule_is_daily_summarys_rule_and_not_a_third_copy():
    """The tier vocabulary already lives in three places; a fourth copy of the
    group-chat heuristic is how it drifts. This pins that the pulse's answer is
    literally daily_summary's answer, so a change there cannot silently diverge.
    """
    from daily_summary import _is_group_chat

    titles = [
        "Aamir Yousufzai, Ben Gaspar, Mike Wojan",
        "Di Yin Lu, Adam Greer",
        "Random Chat",
        "AI Canvas, UAIA, C3 - Product Integration Execution (PM/Eng)",
        "help-identity-security-intelligence",
        "",
    ]
    for title in titles:
        space = {"title": title, "type": "group", "id": "probe"}
        ok, tier = space_is_eligible(space, PREFS, {})
        if _is_group_chat(space):
            assert (ok, tier) == (True, "dm"), title
        else:
            assert tier != "dm", title


def test_candidate_flags_a_watchlist_sender():
    cand = to_candidate(
        {"personEmail": "rorscott@cisco.com", "personDisplayName": "Rory Scott",
         "created": "2026-09-09T14:02:11.000Z", "text": "hello"},
        {"title": "C3 + CUI", "id": "1"}, "p1", PREFS, MY_EMAIL, MY_NAMES, set(),
    )
    assert cand["is_watchlist"] is True
    assert cand["tier_hint"] == "p1"
    assert cand["channel"] == "C3 + CUI"
    assert cand["text"] == "hello"


def test_candidate_flags_a_direct_mention():
    cand = to_candidate(
        {"personEmail": "someone@cisco.com", "personDisplayName": "Someone",
         "created": "2026-09-09T14:02:11.000Z", "text": "Ben Myers thoughts?"},
        {"title": "SCC - CII Discussion", "id": "2"}, "p2", PREFS, MY_EMAIL, MY_NAMES, set(),
    )
    assert cand["is_direct_mention"] is True
    assert cand["is_watchlist"] is False


def test_a_reply_in_a_thread_ben_is_tagged_in_is_flagged():
    cand = to_candidate(
        {"personEmail": "s@cisco.com", "personDisplayName": "S", "parentId": "thread-1",
         "created": "2026-09-09T14:02:11.000Z", "text": "following up"},
        {"title": "SCC - CII Discussion", "id": "2"}, "p2", PREFS, MY_EMAIL, MY_NAMES,
        {"thread-1"},
    )
    assert cand["is_watched_thread"] is True


def test_a_reply_in_an_unrelated_thread_is_not_flagged():
    # Any threaded reply would otherwise flag as watched-thread, which escalates
    # every P2 conversation Ben has never touched.
    cand = to_candidate(
        {"personEmail": "s@cisco.com", "personDisplayName": "S", "parentId": "thread-99",
         "created": "2026-09-09T14:02:11.000Z", "text": "unrelated chatter"},
        {"title": "SCC - CII Discussion", "id": "2"}, "p2", PREFS, MY_EMAIL, MY_NAMES,
        {"thread-1"},
    )
    assert cand["is_watched_thread"] is False


def test_a_top_level_message_is_never_a_watched_thread():
    cand = to_candidate(
        {"personEmail": "s@cisco.com", "personDisplayName": "S",
         "created": "2026-09-09T14:02:11.000Z", "text": "new topic"},
        {"title": "SCC - CII Discussion", "id": "2"}, "p2", PREFS, MY_EMAIL, MY_NAMES,
        {"thread-1"},
    )
    assert cand["is_watched_thread"] is False


def test_candidate_timestamp_is_iso_with_offset():
    cand = to_candidate(
        {"personEmail": "a@cisco.com", "personDisplayName": "A",
         "created": "2026-09-09T14:02:11.000Z", "text": "x"},
        {"title": "C3 + CUI", "id": "1"}, "p1", PREFS, MY_EMAIL, MY_NAMES, set(),
    )
    assert cand["at"] == "2026-09-09T14:02:11+00:00"


def test_candidate_link_points_at_the_space():
    cand = to_candidate(
        {"personEmail": "a@cisco.com", "personDisplayName": "A",
         "created": "2026-09-09T14:02:11.000Z", "text": "x"},
        {"title": "C3 + CUI", "id": "space-uuid"}, "p1", PREFS, MY_EMAIL, MY_NAMES, set(),
    )
    assert cand["link"] == "https://web.webex.com/spaces/space-uuid"


# --- collect() — exercised against a fake client, never a live Webex call ---


class FakeWebexClient:
    """Stub matching the two WebexClient methods collect() depends on.

    webex_client.py has no `list_messages` — the real method is
    `get_messages(room_id, before=None, after=None, max_results=...)`, which
    already filters by `after` client-side. collect() was adjusted in Step 5
    to call this real signature instead of the brief's placeholder name.
    """

    def __init__(self, spaces, messages_by_room, broken_rooms=None):
        self._spaces = spaces
        self._messages_by_room = messages_by_room
        self._broken_rooms = broken_rooms or set()

    def list_spaces(self, max_results=400):
        return self._spaces

    def get_messages(self, room_id, before=None, after=None, max_results=100):
        if room_id in self._broken_rooms:
            raise RuntimeError("simulated Webex API failure")
        return self._messages_by_room.get(room_id, [])


SINCE = datetime(2026, 9, 9, 12, 0, 0, tzinfo=timezone.utc)


def _msg(email, text, minutes_after_since=5, name="Someone"):
    at = SINCE.replace(minute=SINCE.minute)
    ts = SINCE.isoformat().replace("+00:00", "Z")
    # Build a created timestamp strictly after SINCE.
    created = (SINCE.timestamp() + minutes_after_since * 60)
    dt = datetime.fromtimestamp(created, tz=timezone.utc)
    return {
        "personEmail": email,
        "personDisplayName": name,
        "created": dt.isoformat().replace("+00:00", "Z"),
        "text": text,
    }


def test_collect_gathers_only_from_eligible_spaces():
    spaces = [
        {"title": "C3 + CUI", "type": "group", "id": "p1-space"},
        {"title": "Random Chat", "type": "group", "id": "unlisted-space"},
    ]
    messages = {
        "p1-space": [_msg("a@cisco.com", "watch this")],
        "unlisted-space": [_msg("b@cisco.com", "ordinary chatter")],
    }
    webex = FakeWebexClient(spaces, messages)
    out, _status = collect(webex, PREFS, SINCE, MY_EMAIL, MY_NAMES, {})
    assert len(out) == 1
    assert out[0]["channel"] == "C3 + CUI"
    assert out[0]["text"] == "watch this"


def test_collect_excludes_bens_own_messages():
    spaces = [{"title": "C3 + CUI", "type": "group", "id": "p1-space"}]
    messages = {
        "p1-space": [
            _msg(MY_EMAIL, "my own reply"),
            _msg("a@cisco.com", "someone else"),
        ]
    }
    webex = FakeWebexClient(spaces, messages)
    out, _status = collect(webex, PREFS, SINCE, MY_EMAIL, MY_NAMES, {})
    assert len(out) == 1
    assert out[0]["text"] == "someone else"


def test_collect_excludes_messages_at_or_before_since():
    spaces = [{"title": "C3 + CUI", "type": "group", "id": "p1-space"}]
    messages = {
        "p1-space": [
            _msg("a@cisco.com", "too old", minutes_after_since=0),
            _msg("a@cisco.com", "fresh", minutes_after_since=5),
        ]
    }
    webex = FakeWebexClient(spaces, messages)
    out, _status = collect(webex, PREFS, SINCE, MY_EMAIL, MY_NAMES, {})
    assert [c["text"] for c in out] == ["fresh"]


def test_collect_survives_a_broken_room_and_warns_on_stderr(capsys):
    spaces = [
        {"title": "C3 + CUI", "type": "group", "id": "broken-space"},
        {"title": "SCC - CII Discussion", "type": "group", "id": "ok-space"},
    ]
    messages = {"ok-space": [_msg("a@cisco.com", "still here")]}
    webex = FakeWebexClient(spaces, messages, broken_rooms={"broken-space"})
    out, _status = collect(webex, PREFS, SINCE, MY_EMAIL, MY_NAMES, {})
    assert [c["text"] for c in out] == ["still here"]
    err = capsys.readouterr().err
    assert "C3 + CUI" in err or "broken-space" in err


# --- F9: collect must be able to report its own degradation ----------------
#
# Email and calendar both return (candidates, status). Webex returning a bare
# list is what let a run where get_messages failed for EVERY space write
# `sources: {"webex": "ok"}` and zero items — a total blackout rendering as a
# quiet hour. These three tests are the whole point of the tuple.


def test_collect_reports_ok_when_every_space_was_fetched():
    spaces = [{"title": "C3 + CUI", "type": "group", "id": "p1-space"}]
    webex = FakeWebexClient(spaces, {"p1-space": [_msg("a@cisco.com", "hi")]})
    _out, status = collect(webex, PREFS, SINCE, MY_EMAIL, MY_NAMES, {})
    # Exactly "ok": the Hub treats any other value as degraded, so a well-meant
    # "ok (1 space)" would light the panel up on a healthy run.
    assert status == "ok"


def test_collect_reports_degraded_when_some_spaces_could_not_be_fetched():
    spaces = [
        {"title": "C3 + CUI", "type": "group", "id": "broken-space"},
        {"title": "SCC - CII Discussion", "type": "group", "id": "ok-space"},
    ]
    messages = {"ok-space": [_msg("a@cisco.com", "still here")]}
    webex = FakeWebexClient(spaces, messages, broken_rooms={"broken-space"})
    out, status = collect(webex, PREFS, SINCE, MY_EMAIL, MY_NAMES, {})
    assert [c["text"] for c in out] == ["still here"]
    assert status == "degraded: 1 of 2 spaces could not be fetched"


def test_collect_reports_degraded_when_every_space_failed_rather_than_ok_and_empty():
    """The blackout case. 429s or post-membership-change 403s across the board
    produce zero candidates; without this, the run says `ok` and the panel says
    "nothing has needed you", which is invariant 1 inverted.
    """
    spaces = [
        {"title": "C3 + CUI", "type": "group", "id": "a"},
        {"title": "SCC - CII Discussion", "type": "group", "id": "b"},
    ]
    webex = FakeWebexClient(spaces, {}, broken_rooms={"a", "b"})
    out, status = collect(webex, PREFS, SINCE, MY_EMAIL, MY_NAMES, {})
    assert out == []
    assert status == "degraded: 2 of 2 spaces could not be fetched"


def test_collect_counts_only_eligible_spaces_in_the_denominator():
    """m is "spaces we tried", not "spaces Webex has". Counting the ~400
    unlisted spaces would make "3 of 400" read as a rounding error when it is
    in fact 3 of the 17 that matter.
    """
    spaces = [
        {"title": "C3 + CUI", "type": "group", "id": "broken-space"},
        {"title": "Random Chat", "type": "group", "id": "unlisted-space"},
    ]
    webex = FakeWebexClient(spaces, {}, broken_rooms={"broken-space"})
    _out, status = collect(webex, PREFS, SINCE, MY_EMAIL, MY_NAMES, {})
    assert status == "degraded: 1 of 1 spaces could not be fetched"


def test_collect_gathers_from_a_group_chat_that_appears_in_no_list():
    """End to end for F16: the space the migration deleted from Always Scan on
    the grounds that group chats are scanned unconditionally.
    """
    spaces = [
        {"title": "Aamir Yousufzai, Ben Gaspar, Mike Wojan", "type": "group", "id": "gc"},
    ]
    messages = {"gc": [_msg("ayousufz@cisco.com", "can you take the CUI slide?")]}
    webex = FakeWebexClient(spaces, messages)
    out, status = collect(webex, PREFS, SINCE, MY_EMAIL, MY_NAMES, {})
    assert [c["text"] for c in out] == ["can you take the CUI slide?"]
    assert out[0]["tier_hint"] == "dm"
    assert status == "ok"


def test_collect_uses_watched_space_threads_for_unlisted_space():
    spaces = [{"title": "Random Chat", "type": "group", "id": "unlisted-space"}]
    messages = {
        "unlisted-space": [
            {
                "personEmail": "a@cisco.com",
                "personDisplayName": "A",
                "parentId": "thread-1",
                "created": "2026-09-09T13:00:00.000Z",
                "text": "thread reply",
            }
        ]
    }
    webex = FakeWebexClient(spaces, messages)
    out, _status = collect(
        webex, PREFS, SINCE, MY_EMAIL, MY_NAMES, {"unlisted-space": ["thread-1"]}
    )
    assert len(out) == 1
    assert out[0]["is_watched_thread"] is True
    assert out[0]["tier_hint"] == "p2"
