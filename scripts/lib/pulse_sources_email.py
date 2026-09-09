"""Work-email candidates via the msgraph CLI.

msgraph auth is browser-cookie scraping with no refresh token, so this source
fails regularly and unattended. That is an expected state, not an exception:
`collect` always returns a status string and never raises. A degraded email
fetch has to be visible in the panel, because a silently missing source reads
as a quiet hour — and a truly empty inbox has to report "ok" so the two are
never confused with each other.

Eligibility, not urgency. This module decides which messages the classifier
gets to look at; the classifier decides which of them can't wait.
"""
from __future__ import annotations

import json
import os
import re
import subprocess

MSGRAPH = os.path.expanduser("~/.config/claude-graph/bin/msgraph")
MAX_RESULTS = "60"
TIMEOUT_S = 90

SLACK_SENDER = "notification@slack.com"
# Only DMs and direct mentions of Ben. Channel-activity digests are noise.
SLACK_DM_SUBJECT = re.compile(r"^(?P<who>.+?) sent you a message\s*$", re.I)
SLACK_MENTION_SUBJECT = re.compile(r"^(?P<who>.+?) mentioned you in a (?:direct message|thread)\s*$", re.I)

CALENDAR_SUBJECT = re.compile(r"^(Accepted|Declined|Tentative|Canceled|Cancelled):", re.I)

# Exact automated-sender addresses. Deliberately a short, literal list — new
# automated senders are caught by the AUTOMATED_LOCALPARTS prefix check below,
# not by growing this constant with guesses.
AUTOMATED_SENDERS: frozenset[str] = frozenset({
    "notification@slack.com",
    "noreply@github.com",
    "no-reply@cisco.com",
    "donotreply@webex.com",
})
AUTOMATED_LOCALPARTS = ("noreply", "no-reply", "donotreply", "do-not-reply", "notifications")


def _addr(node) -> str:
    if isinstance(node, dict):
        return (node.get("address") or "").strip().lower()
    return ""


def _recipients(msg, key) -> set[str]:
    return {_addr(r) for r in (msg.get(key) or [])}


def _is_automated(address: str) -> bool:
    if not address:
        return False
    if address in AUTOMATED_SENDERS:
        return True
    # Prefix match on the local part only — never a substring match anywhere
    # in the address. "annoreply@x" must not be caught by "noreply".
    local = address.split("@", 1)[0]
    return any(local.startswith(p) for p in AUTOMATED_LOCALPARTS)


def classify_sender(msg: dict, prefs, my_email: str) -> str:
    """`keep` (priority-eligible), `slack_panel` (silent only), or `drop`."""
    sender = _addr(msg.get("from"))
    me = (my_email or "").strip().lower()

    if sender == SLACK_SENDER:
        return "slack_panel" if parse_slack_notification(msg) else "drop"

    if CALENDAR_SUBJECT.match(msg.get("subject") or ""):
        return "drop"

    if not sender or sender == me:
        return "drop"

    # Automated mail can never clear the bar, even if it is on the watchlist
    # or addressed straight at Ben. This check must come before both.
    if _is_automated(sender):
        return "drop"

    # A watchlist sender is priority-eligible regardless of addressing.
    # Matching is exact-address (case-insensitive), never substring — and
    # never against the display name, which the sender fully controls.
    if prefs.is_watchlist(sender):
        return "keep"

    if me in _recipients(msg, "toRecipients") | _recipients(msg, "ccRecipients"):
        return "keep"

    return "drop"


def parse_slack_notification(msg: dict) -> dict | None:
    """Pull the human sender and message text out of a Slack notification email.

    Panel-only by construction — the sending address is automated, so this can
    never clear the priority bar. Returns None for channel-activity digests,
    and — just as importantly — for anything that fails to parse into a real
    sender name and non-empty text. A half-built dict with an empty sender or
    empty text would reach the classifier looking like a real candidate; that
    is worse than surfacing nothing.
    """
    subject = (msg.get("subject") or "").strip()
    match = SLACK_DM_SUBJECT.match(subject) or SLACK_MENTION_SUBJECT.match(subject)
    if not match:
        return None

    who = match.group("who").strip()
    if not who:
        return None

    body = (msg.get("body_preview") or "").replace("\r\n", "\n")
    lines = [ln.strip() for ln in body.split("\n") if ln.strip()]

    # Slack's layout is: context lines, then "<Name>   <timestamp>", then the
    # message. Take everything after the last line that starts with the name.
    text_lines, seen_byline = [], False
    for line in lines:
        if not seen_byline and line.startswith(who):
            seen_byline = True
            continue
        if seen_byline:
            text_lines.append(line)

    text = " ".join(text_lines) if text_lines else body.strip()
    if not text:
        return None

    return {"from_name": who, "text": text}


def to_candidate(msg: dict, disposition: str, prefs=None) -> dict:
    web_link = msg.get("web_link") or ""
    received = msg.get("received") or ""
    subject = msg.get("subject") or "(no subject)"

    if disposition == "slack_panel":
        # Defensive only: classify_sender never returns "slack_panel" unless
        # parse_slack_notification already succeeded. If some other caller
        # reaches this branch with an unparseable message, fall back to the
        # subject line rather than an empty string, per the fail-closed rule.
        parsed = parse_slack_notification(msg) or {"from_name": "Slack", "text": subject}
        return {
            "source": "email",
            "channel": "Slack (via email)",
            "space_id": None,
            "from_name": parsed["from_name"],
            "from_email": SLACK_SENDER,
            "at": received,
            "text": parsed["text"],
            "link": web_link,
            "is_watchlist": prefs.is_watchlist(SLACK_SENDER) if prefs else False,
            "is_direct_mention": True,
            "is_watched_thread": False,
            "tier_hint": "slack_panel",
        }

    sender = msg.get("from") or {}
    from_email = _addr(sender)
    return {
        "source": "email",
        "channel": "Email",
        "space_id": None,
        "from_name": (sender.get("name") or from_email),
        "from_email": from_email,
        "at": received,
        "text": f"Subject: {subject}\n\n{msg.get('body_preview') or ''}",
        "link": web_link,
        "is_watchlist": prefs.is_watchlist(from_email) if prefs else False,
        "is_direct_mention": True,
        "is_watched_thread": False,
        "tier_hint": "email",
    }


def _run_msgraph(args: list[str]) -> str:
    try:
        return subprocess.run(
            [MSGRAPH, *args], capture_output=True, text=True, timeout=TIMEOUT_S, check=True
        ).stdout
    except subprocess.CalledProcessError as exc:
        try:
            reason = json.loads(exc.stdout or "").get("error")
        except (ValueError, AttributeError):
            reason = None
        reason = reason or (exc.stdout or "").strip() or (exc.stderr or "").strip() or str(exc)
        raise RuntimeError(reason) from exc


def collect(since_iso: str, prefs, my_email: str, runner=_run_msgraph) -> tuple[list[dict], str]:
    """Returns (candidates, status). Never raises.

    `status` is "ok" or "degraded: <reason>". A degraded fetch always returns
    an empty candidate list — degraded status must never be paired with
    partial results a caller might mistake for the whole picture. An "ok"
    status with zero candidates means the inbox really was empty (or nothing
    in it cleared the bar), and that must stay distinguishable from a failure.
    """
    day = (since_iso or "")[:10]
    args = ["email", "search", f"received>={day}", "--max", MAX_RESULTS]

    try:
        raw = runner(args)
    except Exception as exc:  # noqa: BLE001 — any failure degrades, none propagates
        return [], f"degraded: {exc}"

    if not raw:
        return [], "degraded: empty output from msgraph"

    try:
        payload = json.loads(raw)
        messages = payload.get("messages", [])
    except (ValueError, TypeError, AttributeError) as exc:
        return [], f"degraded: unparseable msgraph output ({exc})"

    if not isinstance(messages, list):
        return [], "degraded: unparseable msgraph output (messages is not a list)"

    candidates = []
    skipped = 0
    for msg in messages:
        if not isinstance(msg, dict):
            skipped += 1
            continue
        if (msg.get("received") or "") < since_iso:
            continue
        disposition = classify_sender(msg, prefs, my_email)
        if disposition == "drop":
            continue
        candidates.append(to_candidate(msg, disposition, prefs))

    if messages and skipped == len(messages):
        return [], "degraded: no usable messages in msgraph output"

    return candidates, "ok"
