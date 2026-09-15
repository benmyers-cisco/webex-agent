from datetime import datetime, timezone

from lib.pulse_prefs import parse_prefs
from lib.pulse_sources_webex import (
    collect,
    mentions_ben,
    resolve_answered,
    space_is_eligible,
    to_candidate,
)

PREFS = parse_prefs("""
## Watchlist
- Rory Scott <rorscott@cisco.com>

### Priority 1 — Interrupt Me
- C3 + CUI

### Priority 2 — Tagged Only
- SCC - CII Discussion

## Mentions Only
- Duo PM Sync
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


def test_a_mentions_only_space_is_eligible_and_carries_its_own_tier():
    """F4. It used to resolve to "unlisted" and be skipped before a single
    message was fetched, which made "Mentions only" and "Never scan" the same
    thing in the pulse. `collect` translates this tier to a p2 hint.
    """
    ok, tier = space_is_eligible(
        {"title": "Duo PM Sync", "type": "group", "id": "m"}, PREFS, {}
    )
    assert (ok, tier) == (True, "mentions")


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


def test_collect_takes_only_mentions_and_watchlist_from_a_mentions_only_space():
    """The half that makes the label true rather than merely making the space P2.
    A mentions-tier space must not dump its backlog into the panel — that is the
    noise Ben demoted it to escape.
    """
    spaces = [{"title": "Duo PM Sync", "type": "group", "id": "m"}]
    messages = {
        "m": [
            _msg("noisy@cisco.com", "sprint board updated"),
            _msg("someone@cisco.com", "Ben Myers can you take this?"),
            _msg("rorscott@cisco.com", "no mention, but I'm on the watchlist"),
        ]
    }
    webex = FakeWebexClient(spaces, messages)
    out, status = collect(webex, PREFS, SINCE, MY_EMAIL, MY_NAMES, {})
    assert [c["text"] for c in out] == [
        "Ben Myers can you take this?",
        "no mention, but I'm on the watchlist",
    ]
    assert {c["tier_hint"] for c in out} == {"p2"}
    assert status == "ok"


def test_collect_returns_nothing_from_a_quiet_mentions_only_space():
    spaces = [{"title": "Duo PM Sync", "type": "group", "id": "m"}]
    messages = {"m": [_msg("noisy@cisco.com", "standup notes")]}
    webex = FakeWebexClient(spaces, messages)
    out, status = collect(webex, PREFS, SINCE, MY_EMAIL, MY_NAMES, {})
    assert out == []
    # Filtered, not failed: the space was reached, so nothing is degraded.
    assert status == "ok"


def test_a_broken_mentions_only_space_still_counts_toward_the_degraded_status():
    """Its messages are filtered, but its reachability is not — an unfetchable
    mentions space is one where an @mention would be missed silently.
    """
    spaces = [{"title": "Duo PM Sync", "type": "group", "id": "m"}]
    webex = FakeWebexClient(spaces, {}, broken_rooms={"m"})
    out, status = collect(webex, PREFS, SINCE, MY_EMAIL, MY_NAMES, {})
    assert out == []
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


def test_a_tagged_thread_reply_survives_the_mentions_only_filter():
    """Demoting a space to `## Mentions Only` must not cost coverage Ben already
    had by leaving it unlisted.

    An unlisted space with a thread he's been tagged in is eligible at p2, and
    the tagged-thread trigger is one of the two ways a p2 space can notify. Since
    the mentions filter only kept @mentions and watchlist senders, moving that
    same space into Mentions Only made the tagged-thread path unreachable — a
    demotion that *narrowed* coverage instead of narrowing noise. The spec's tier
    table says mentions survivors are judged as P2, and P2 includes tagged
    threads, so the filter has to admit them for the table to be true.
    """
    spaces = [{"title": "Duo PM Sync", "type": "group", "id": "m"}]
    reply = _msg("noisy@cisco.com", "picking this back up")
    reply["parentId"] = "thread-ben-is-in"
    messages = {"m": [_msg("noisy@cisco.com", "unrelated chatter"), reply]}
    webex = FakeWebexClient(spaces, messages)
    out, status = collect(
        webex, PREFS, SINCE, MY_EMAIL, MY_NAMES, {"m": ["thread-ben-is-in"]}
    )
    assert [c["text"] for c in out] == ["picking this back up"]
    assert out[0]["is_watched_thread"] is True
    assert out[0]["tier_hint"] == "p2"
    assert status == "ok"


def test_a_reply_in_a_thread_ben_is_not_in_does_not_survive_the_mentions_filter():
    """The other half: `is_watched_thread` is membership, not "is a reply". If
    any threaded reply passed, a mentions-only space would leak every
    conversation in it — the exact noise the demotion exists to stop.
    """
    spaces = [{"title": "Duo PM Sync", "type": "group", "id": "m"}]
    reply = _msg("noisy@cisco.com", "replying to someone else")
    reply["parentId"] = "thread-ben-is-not-in"
    webex = FakeWebexClient(spaces, {"m": [reply]})
    out, status = collect(
        webex, PREFS, SINCE, MY_EMAIL, MY_NAMES, {"m": ["thread-ben-is-in"]}
    )
    assert out == []
    assert status == "ok"


def test_an_explicit_never_scan_listing_beats_the_group_chat_heuristic():
    """`## Never Scan` is Ben saying "not this one", and a title heuristic must
    not overrule it.

    `is_group_chat` is a guess from the title — Webex ad-hoc rooms are typed
    "group" and titled with participant names, so a room legitimately named like
    people trips it. Evaluating it before `tier_of` meant an explicitly excluded
    space could be fetched and escalated to P1, which is the loudest possible way
    to ignore an instruction. Never Scan is checked first now.
    """
    prefs = parse_prefs("""
## Never Scan
- Sam Betlej, Adam Greer
""")
    space = {"title": "Sam Betlej, Adam Greer", "type": "group", "id": "n"}
    ok, tier = space_is_eligible(space, prefs, {})
    assert (ok, tier) == (False, "never")


# --- Already answered ------------------------------------------------------
# Webex exposes no read state for the authenticated user, so "Ben spoke after
# it" is the only available evidence that he has dealt with a message. It is
# also the stronger signal: a reply is action, a read receipt is only exposure.


def test_a_message_ben_replied_to_is_not_surfaced():
    spaces = [{"title": "C3 + CUI", "type": "group", "id": "p1-space"}]
    messages = {
        "p1-space": [
            _msg("a@cisco.com", "can you confirm the cert date?", minutes_after_since=5),
            _msg(MY_EMAIL, "yes, November", minutes_after_since=9),
        ]
    }
    webex = FakeWebexClient(spaces, messages)
    out, status = collect(webex, PREFS, SINCE, MY_EMAIL, MY_NAMES, {})
    assert out == []
    assert status == "ok"


def test_a_message_arriving_after_bens_last_word_still_surfaces():
    # The half that matters most: replying earlier in the hour must not silence
    # everything that lands afterwards.
    spaces = [{"title": "C3 + CUI", "type": "group", "id": "p1-space"}]
    messages = {
        "p1-space": [
            _msg("a@cisco.com", "old question", minutes_after_since=3),
            _msg(MY_EMAIL, "answered", minutes_after_since=5),
            _msg("a@cisco.com", "one more thing", minutes_after_since=9),
        ]
    }
    webex = FakeWebexClient(spaces, messages)
    out, _status = collect(webex, PREFS, SINCE, MY_EMAIL, MY_NAMES, {})
    assert [c["text"] for c in out] == ["one more thing"]


def test_suppression_is_per_space():
    # Ben answering in one space says nothing about another. Computing the
    # cutoff once across every space would silence the whole run.
    spaces = [
        {"title": "C3 + CUI", "type": "group", "id": "p1-space"},
        {"title": "Rory Scott", "type": "direct", "id": "dm"},
    ]
    messages = {
        "p1-space": [_msg(MY_EMAIL, "answered here", minutes_after_since=9)],
        "dm": [_msg("rorscott@cisco.com", "still waiting on you", minutes_after_since=5)],
    }
    webex = FakeWebexClient(spaces, messages)
    out, _status = collect(webex, PREFS, SINCE, MY_EMAIL, MY_NAMES, {})
    assert [c["text"] for c in out] == ["still waiting on you"]


def test_an_equal_timestamp_keeps_the_message_visible():
    # Webex timestamps can tie, and a tie is no evidence Ben's reply came
    # second. The safe reading of an ambiguous order is the visible one.
    spaces = [{"title": "C3 + CUI", "type": "group", "id": "p1-space"}]
    messages = {
        "p1-space": [
            _msg("a@cisco.com", "same second", minutes_after_since=5),
            _msg(MY_EMAIL, "also same second", minutes_after_since=5),
        ]
    }
    webex = FakeWebexClient(spaces, messages)
    out, _status = collect(webex, PREFS, SINCE, MY_EMAIL, MY_NAMES, {})
    assert [c["text"] for c in out] == ["same second"]


# --- resolve_answered() — the carried-item counterpart to collect()'s
# answered_through, which only ever sees messages inside the fetch window ---


class RecordingWebexClient(FakeWebexClient):
    """FakeWebexClient that remembers what was asked of it.

    resolve_answered's cost claim is "one fetch per space, from the oldest
    carried item in it". That is only checkable if the calls are observable.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.calls = []

    def get_messages(self, room_id, before=None, after=None, max_results=100):
        self.calls.append({"room_id": room_id, "after": after})
        return super().get_messages(room_id, before=before, after=after, max_results=max_results)


def _carried(item_id, space_id, at, source="webex"):
    return {"id": item_id, "source": source, "space_id": space_id, "at": at.isoformat()}


AT_1251 = datetime(2026, 9, 15, 16, 51, 19, tzinfo=timezone.utc)


def _from_me(at):
    return {
        "personEmail": MY_EMAIL,
        "personDisplayName": "Ben Myers",
        "created": at.isoformat().replace("+00:00", "Z"),
        "text": "on it",
    }


def test_a_carried_item_ben_replied_to_afterwards_is_resolved():
    webex = RecordingWebexClient(
        [], {"dm": [_from_me(datetime(2026, 9, 15, 18, 31, 17, tzinfo=timezone.utc))]}
    )
    resolved = resolve_answered(webex, [_carried("aanjan", "dm", AT_1251)], MY_EMAIL)
    assert resolved == {"aanjan"}


def test_a_carried_item_with_no_reply_from_ben_stays_unresolved():
    webex = RecordingWebexClient([], {"dm": [_msg("aaravi@cisco.com", "any update?", 10)]})
    assert resolve_answered(webex, [_carried("aanjan", "dm", AT_1251)], MY_EMAIL) == set()


def test_a_reply_ben_sent_before_the_item_does_not_resolve_it():
    """His 09:00 message is no answer to a 12:51 question."""
    webex = RecordingWebexClient(
        [], {"dm": [_from_me(datetime(2026, 9, 15, 13, 0, 0, tzinfo=timezone.utc))]}
    )
    assert resolve_answered(webex, [_carried("aanjan", "dm", AT_1251)], MY_EMAIL) == set()


def test_an_identical_timestamp_does_not_resolve_the_item():
    """Strictly-earlier, matching collect(). Equal timestamps are no evidence
    Ben's reply came second, and the safe reading keeps the item visible."""
    webex = RecordingWebexClient([], {"dm": [_from_me(AT_1251)]})
    assert resolve_answered(webex, [_carried("aanjan", "dm", AT_1251)], MY_EMAIL) == set()


def test_one_fetch_per_space_starting_from_the_oldest_carried_item():
    older = datetime(2026, 9, 15, 14, 0, 0, tzinfo=timezone.utc)
    webex = RecordingWebexClient([], {"dm": [], "p1": []})
    resolve_answered(
        webex,
        [
            _carried("a", "dm", AT_1251),
            _carried("b", "dm", older),
            _carried("c", "p1", AT_1251),
        ],
        MY_EMAIL,
    )
    assert len(webex.calls) == 2
    by_room = {c["room_id"]: c["after"] for c in webex.calls}
    assert by_room["dm"] == older
    assert by_room["p1"] == AT_1251


def test_only_the_items_older_than_the_reply_are_resolved():
    late = datetime(2026, 9, 15, 19, 0, 0, tzinfo=timezone.utc)
    webex = RecordingWebexClient(
        [], {"dm": [_from_me(datetime(2026, 9, 15, 18, 31, 17, tzinfo=timezone.utc))]}
    )
    resolved = resolve_answered(
        webex, [_carried("early", "dm", AT_1251), _carried("later", "dm", late)], MY_EMAIL
    )
    assert resolved == {"early"}


def test_a_failed_fetch_leaves_the_item_on_the_panel():
    """Degrade toward visible. A stale panel item costs a glance; a resolved-by-
    accident one is invisible, which is the failure this module exists against."""
    webex = RecordingWebexClient([], {}, broken_rooms={"dm"})
    assert resolve_answered(webex, [_carried("aanjan", "dm", AT_1251)], MY_EMAIL) == set()


def test_one_broken_space_does_not_stop_another_from_resolving():
    webex = RecordingWebexClient(
        [],
        {"ok-space": [_from_me(datetime(2026, 9, 15, 18, 31, 17, tzinfo=timezone.utc))]},
        broken_rooms={"dm"},
    )
    resolved = resolve_answered(
        webex, [_carried("a", "dm", AT_1251), _carried("b", "ok-space", AT_1251)], MY_EMAIL
    )
    assert resolved == {"b"}


def test_non_webex_items_are_never_fetched_for():
    """Email has no reply signal available here, so asking Webex about it would
    be a call that cannot answer the question."""
    webex = RecordingWebexClient([], {})
    assert resolve_answered(webex, [_carried("e", None, AT_1251, source="email")], MY_EMAIL) == set()
    assert webex.calls == []


def test_a_webex_item_with_no_space_id_is_skipped_rather_than_guessed_at():
    webex = RecordingWebexClient([], {})
    assert resolve_answered(webex, [_carried("old", None, AT_1251)], MY_EMAIL) == set()
    assert webex.calls == []


def test_an_unparseable_timestamp_is_skipped_rather_than_resolved():
    webex = RecordingWebexClient([], {"dm": []})
    item = {"id": "x", "source": "webex", "space_id": "dm", "at": "not a timestamp"}
    assert resolve_answered(webex, [item], MY_EMAIL) == set()
    assert webex.calls == []


def test_nothing_carried_means_nothing_asked():
    webex = RecordingWebexClient([], {})
    assert resolve_answered(webex, [], MY_EMAIL) == set()
    assert webex.calls == []
