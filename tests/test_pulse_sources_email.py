import json
import subprocess

from lib.pulse_prefs import parse_prefs
from lib.pulse_sources_email import (
    AUTOMATED_SENDERS,
    classify_sender,
    parse_slack_notification,
    to_candidate,
    collect,
)

PREFS = parse_prefs("""
## Watchlist
- Rory Scott <rorscott@cisco.com>
""")
ME = "benmyers@cisco.com"


# `from` is a reserved word, so overrides come in as a dict rather than kwargs.
def _msg(over=None):
    base = {
        "subject": "Cert plan review",
        "from": {"name": "Someone", "address": "someone@cisco.com"},
        "toRecipients": [{"address": ME}],
        "ccRecipients": [],
        "received": "2026-09-09T14:02:11+00:00",
        "body_preview": "Can you confirm before the pre-read?",
        "web_link": "https://outlook.office365.com/owa/?ItemID=x",
    }
    base.update(over or {})
    return base


SLACK = {"name": "Slack", "address": "notification@slack.com"}


def test_direct_human_mail_is_kept():
    assert classify_sender(_msg(), PREFS, ME) == "keep"


def test_watchlist_sender_is_kept_even_when_ben_is_not_a_recipient():
    msg = _msg({
        "from": {"name": "Rory Scott", "address": "rorscott@cisco.com"},
        "toRecipients": [{"address": "someone-else@cisco.com"}],
    })
    assert classify_sender(msg, PREFS, ME) == "keep"


def test_mail_ben_is_only_bcc_on_from_a_stranger_is_dropped():
    msg = _msg({"toRecipients": [{"address": "list@cisco.com"}], "ccRecipients": []})
    assert classify_sender(msg, PREFS, ME) == "drop"


def test_cc_counts_as_direct():
    msg = _msg({"toRecipients": [{"address": "other@cisco.com"}],
                "ccRecipients": [{"address": ME}]})
    assert classify_sender(msg, PREFS, ME) == "keep"


def test_calendar_accept_notice_is_dropped():
    assert classify_sender(_msg({"subject": "Accepted: CII walk-through"}), PREFS, ME) == "drop"


def test_calendar_decline_and_tentative_are_dropped():
    for prefix in ("Declined:", "Tentative:", "Canceled:"):
        assert classify_sender(_msg({"subject": f"{prefix} Something"}), PREFS, ME) == "drop"


def test_mail_from_ben_himself_is_dropped():
    msg = _msg({"from": {"name": "Ben Myers", "address": ME}})
    assert classify_sender(msg, PREFS, ME) == "drop"


def test_slack_notification_routes_to_the_panel_never_to_priority():
    msg = _msg({"subject": "Yizhen Shi sent you a message", "from": SLACK})
    assert classify_sender(msg, PREFS, ME) == "slack_panel"


def test_slack_channel_noise_is_dropped_not_panelled():
    msg = _msg({
        "subject": "Outlook Calendar mentioned you in #Outlook Calendar",
        "from": SLACK,
    })
    assert classify_sender(msg, PREFS, ME) == "drop"


def test_other_automated_senders_are_dropped():
    for addr in ("noreply@github.com", "no-reply@cisco.com", "donotreply@webex.com"):
        msg = _msg({"from": {"name": "Bot", "address": addr}})
        assert classify_sender(msg, PREFS, ME) == "drop"


def test_slack_dm_parsing_extracts_sender_and_text():
    msg = _msg({
        "subject": "Yizhen Shi sent you a message",
        "from": SLACK,
        "body_preview": (
            "You have a new direct message in Cisco SBG\r\n"
            "From your conversation with Yizhen Shi\r\n"
            "Yizhen Shi   September 8th at 2:53 PM\r\n"
            "I set up a meeting for the walk-through you offered!"
        ),
    })
    parsed = parse_slack_notification(msg)
    assert parsed["from_name"] == "Yizhen Shi"
    assert "walk-through you offered" in parsed["text"]


def test_slack_parsing_returns_none_for_a_non_dm_subject():
    assert parse_slack_notification(_msg({"subject": "something else"})) is None


def test_candidate_carries_the_full_body_not_a_summary():
    cand = to_candidate(_msg({"body_preview": "the whole thing"}), "keep")
    assert cand["text"].endswith("the whole thing")
    assert cand["source"] == "email"
    assert cand["tier_hint"] == "email"


def test_slack_panel_candidate_is_tagged_for_the_silent_section():
    msg = _msg({
        "subject": "Yizhen Shi sent you a message",
        "from": SLACK,
        "body_preview": "From your conversation with Yizhen Shi\r\nYizhen Shi   x\r\nhi there",
    })
    cand = to_candidate(msg, "slack_panel")
    assert cand["tier_hint"] == "slack_panel"
    assert cand["channel"] == "Slack (via email)"


def test_collect_returns_degraded_status_when_msgraph_fails():
    def boom(_args):
        raise RuntimeError("token expired")

    candidates, status = collect("2026-09-09T12:30:00+00:00", PREFS, ME, runner=boom)
    assert candidates == []
    assert status.startswith("degraded:")
    assert "token expired" in status


def test_collect_returns_ok_and_filters_on_success():
    payload = json.dumps({"messages": [
        _msg(),
        _msg({"subject": "Accepted: nope"}),
    ]})
    candidates, status = collect(
        "2026-09-09T12:30:00+00:00", PREFS, ME, runner=lambda _a: payload
    )
    assert status == "ok"
    assert len(candidates) == 1


def test_collect_reports_degraded_on_unparseable_output():
    candidates, status = collect(
        "2026-09-09T12:30:00+00:00", PREFS, ME, runner=lambda _a: "not json"
    )
    assert candidates == []
    assert status.startswith("degraded:")


# --- Hardening: failures must always be loud, never indistinguishable from
# an empty inbox, and never allowed to raise out of collect(). ---


def test_collect_reports_degraded_when_runner_times_out():
    def timeout(_args):
        raise subprocess.TimeoutExpired(cmd=["msgraph"], timeout=90)

    candidates, status = collect("2026-09-09T12:30:00+00:00", PREFS, ME, runner=timeout)
    assert candidates == []
    assert status.startswith("degraded:")


def test_collect_reports_degraded_when_runner_returns_none():
    candidates, status = collect(
        "2026-09-09T12:30:00+00:00", PREFS, ME, runner=lambda _a: None
    )
    assert candidates == []
    assert status.startswith("degraded:")


def test_collect_ok_with_truly_empty_inbox_is_distinguishable_from_a_failure():
    payload = json.dumps({"messages": []})
    candidates, status = collect(
        "2026-09-09T12:30:00+00:00", PREFS, ME, runner=lambda _a: payload
    )
    assert candidates == []
    assert status == "ok"


def test_collect_reports_degraded_when_messages_is_not_a_list():
    payload = json.dumps({"messages": "oops"})
    candidates, status = collect(
        "2026-09-09T12:30:00+00:00", PREFS, ME, runner=lambda _a: payload
    )
    assert candidates == []
    assert status.startswith("degraded:")


def test_collect_reports_degraded_when_messages_are_all_wrong_shape():
    payload = json.dumps({"messages": [1, 2, 3]})
    candidates, status = collect(
        "2026-09-09T12:30:00+00:00", PREFS, ME, runner=lambda _a: payload
    )
    assert candidates == []
    assert status.startswith("degraded:")


def test_collect_reports_is_watchlist_true_for_a_watchlisted_sender():
    payload = json.dumps({"messages": [
        _msg({
            "from": {"name": "Rory Scott", "address": "rorscott@cisco.com"},
            "toRecipients": [{"address": "someone-else@cisco.com"}],
        }),
    ]})
    candidates, status = collect(
        "2026-09-09T12:30:00+00:00", PREFS, ME, runner=lambda _a: payload
    )
    assert status == "ok"
    assert len(candidates) == 1
    assert candidates[0]["is_watchlist"] is True


def test_collect_reports_is_watchlist_false_for_a_non_watchlisted_sender():
    payload = json.dumps({"messages": [_msg()]})
    candidates, status = collect(
        "2026-09-09T12:30:00+00:00", PREFS, ME, runner=lambda _a: payload
    )
    assert status == "ok"
    assert len(candidates) == 1
    assert candidates[0]["is_watchlist"] is False


# --- Hardening: classify_sender must not let automated mail through, even
# when it would otherwise qualify (direct recipient, or on the watchlist). ---


def test_automated_sender_prefix_variants_are_dropped_even_as_direct_recipient():
    for addr in ("notifications@atlassian.net", "no-reply@duo.com", "do-not-reply@cisco.com"):
        msg = _msg({"from": {"name": "Bot", "address": addr},
                     "toRecipients": [{"address": ME}]})
        assert classify_sender(msg, PREFS, ME) == "drop"


def test_sender_with_noreply_only_as_a_substring_not_a_prefix_is_kept():
    # "annoreply" contains "noreply" but does not start with any automated
    # prefix — this must not be caught by the automated-sender filter.
    msg = _msg({"from": {"name": "Ann Oreply", "address": "annoreply@cisco.com"}})
    assert classify_sender(msg, PREFS, ME) == "keep"


def test_automated_senders_constant_is_exactly_the_briefs_entries():
    assert AUTOMATED_SENDERS == frozenset({
        "notification@slack.com",
        "noreply@github.com",
        "no-reply@cisco.com",
        "donotreply@webex.com",
    })


# --- Hardening: sender matching is exact email-address matching, never a
# substring or display-name match. ---


def test_watchlist_match_requires_the_exact_address_not_a_substring():
    # Case 1: the candidate address CONTAINS the watchlisted address as a
    # substring ("rorscott@cisco.com" inside "xrorscott@cisco.com") but is
    # not equal to it. Must not be treated as a watchlist hit.
    msg_superset = _msg({
        "from": {"name": "Someone Else", "address": "xrorscott@cisco.com"},
        "toRecipients": [{"address": "someone-else@cisco.com"}],
    })
    assert classify_sender(msg_superset, PREFS, ME) == "drop"

    # Case 2: the WATCHLISTED address contains the candidate address as a
    # substring ("orscott@cisco.com" inside "rorscott@cisco.com") but the
    # candidate itself is not equal to the watchlisted entry.
    msg_subset = _msg({
        "from": {"name": "Someone Else", "address": "orscott@cisco.com"},
        "toRecipients": [{"address": "someone-else@cisco.com"}],
    })
    assert classify_sender(msg_subset, PREFS, ME) == "drop"


def test_watchlist_match_is_case_insensitive_on_the_address():
    msg = _msg({
        "from": {"name": "Rory Scott", "address": "RorScott@CISCO.com"},
        "toRecipients": [{"address": "someone-else@cisco.com"}],
    })
    assert classify_sender(msg, PREFS, ME) == "keep"


def test_display_name_matching_a_watchlist_address_is_not_a_real_match():
    # The display name lies; the address is what counts.
    msg = _msg({
        "from": {"name": "rorscott@cisco.com", "address": "attacker@evil.example"},
        "toRecipients": [{"address": "someone-else@cisco.com"}],
    })
    assert classify_sender(msg, PREFS, ME) == "drop"


# --- Hardening: parse_slack_notification fails closed. A candidate with an
# empty sender or empty text is worse than no candidate at all. ---


def test_slack_notification_with_missing_body_returns_none():
    msg = _msg({"subject": "Yizhen Shi sent you a message", "from": SLACK})
    del msg["body_preview"]
    assert parse_slack_notification(msg) is None


def test_slack_notification_with_empty_body_returns_none():
    msg = _msg({
        "subject": "Yizhen Shi sent you a message",
        "from": SLACK,
        "body_preview": "",
    })
    assert parse_slack_notification(msg) is None
