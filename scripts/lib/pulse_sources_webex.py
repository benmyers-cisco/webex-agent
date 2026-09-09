"""Webex candidate collection for the pulse.

Eligibility, not urgency. This module decides which messages the classifier
gets to look at; the classifier decides which of them can't wait.
"""
from __future__ import annotations

import re
import sys
from datetime import datetime, timezone

SPACE_LINK = "https://web.webex.com/spaces/{}"
# A single-word name is too loose — "Ben" matches "Ben Gaspar" and ordinary
# prose. Only multi-word names and the email address count as a mention.
_MIN_NAME_WORDS = 2
# One hourly pass per space; generous enough to catch a busy hour without
# paging deep into history the "since" filter would discard anyway.
_MESSAGES_PER_SPACE = 100


def mentions_ben(text: str, my_email: str, my_names: list[str]) -> bool:
    haystack = (text or "").lower()
    if my_email and my_email.lower() in haystack:
        return True
    for name in my_names:
        if len(name.split()) < _MIN_NAME_WORDS:
            continue
        if re.search(rf"\b{re.escape(name.lower())}\b", haystack):
            return True
    return False


def space_is_eligible(space: dict, prefs, watched_space_threads: dict) -> tuple[bool, str]:
    """Whether this space's messages are worth fetching, and at which tier.

    A DM is inherently aimed at Ben, so it gets its own tier. An unlisted space
    with a thread he's been tagged in is treated as p2 — the thread earns the
    look, the space doesn't.
    """
    if space.get("type") == "direct":
        return True, "dm"

    tier = prefs.tier_of(space.get("title", ""))
    if tier in ("p1", "p2"):
        return True, tier

    if space.get("id") in watched_space_threads:
        return True, "p2"

    return False, "unlisted"


def to_candidate(msg, space, tier_hint, prefs, my_email, my_names, watched_thread_ids) -> dict:
    created = msg.get("created", "")
    try:
        at = datetime.fromisoformat(created.replace("Z", "+00:00")).astimezone(timezone.utc)
        at_iso = at.isoformat()
    except ValueError:
        at_iso = created

    sender = (msg.get("personEmail") or "").strip()
    text = msg.get("text") or "[non-text content]"

    return {
        "source": "webex",
        "channel": space.get("title", ""),
        "space_id": space.get("id"),
        "from_name": msg.get("personDisplayName") or sender,
        "from_email": sender,
        "at": at_iso,
        "text": text,
        "link": SPACE_LINK.format(space.get("id", "")),
        "is_watchlist": prefs.is_watchlist(sender),
        "is_direct_mention": mentions_ben(text, my_email, my_names),
        # Membership, not merely "is a reply". Flagging every threaded reply
        # would escalate every P2 conversation Ben has never touched.
        "is_watched_thread": bool(msg.get("parentId")) and msg["parentId"] in watched_thread_ids,
        "tier_hint": tier_hint,
    }


def collect(webex, prefs, since, my_email, my_names, watched_space_threads) -> list[dict]:
    """Fetch candidates from every eligible space.

    `webex` is a WebexClient. `since` is a timezone-aware UTC datetime.
    `watched_space_threads` is daily_summary.get_watched_thread_spaces()'s
    output: dict[space_id, list[thread_id]]. Read-only — the pulse never writes
    .watched_threads.json.

    Ben's own messages are dropped — the pulse is about what needs him.

    Note: webex_client.py has no `list_messages` method. The real fetch call
    is `get_messages(room_id, before=None, after=None, max_results=...)`,
    which already filters client-side by `after`. `collect` calls that real
    signature (per brief Step 5: "If a real signature differs, fix collect to
    match — do not change webex_client.py"). We still re-check the cutoff
    below rather than trust the client's filtering alone, since
    `get_messages` treats `after` as inclusive-at-boundary while the pulse's
    contract is strictly-after `since`.
    """
    watched_space_threads = watched_space_threads or {}
    candidates: list[dict] = []

    for space in webex.list_spaces(max_results=400):
        eligible, tier_hint = space_is_eligible(space, prefs, watched_space_threads)
        if not eligible:
            continue

        watched_thread_ids = set(watched_space_threads.get(space["id"], []))

        try:
            messages = webex.get_messages(
                space["id"], after=since, max_results=_MESSAGES_PER_SPACE
            )
        except Exception as exc:
            # One unreachable space must not take down the whole run, but the
            # gap must be observable — silence here would read as "nothing
            # happened in that space", which is a lie.
            print(
                f"WARNING: pulse could not fetch messages for Webex space "
                f"'{space.get('title', '?')}' ({space.get('id', '?')}): {exc}",
                file=sys.stderr,
            )
            continue

        for msg in messages:
            created = msg.get("created", "")
            try:
                at = datetime.fromisoformat(created.replace("Z", "+00:00"))
            except ValueError:
                continue
            if at <= since:
                continue
            if (msg.get("personEmail") or "").lower() == my_email.lower():
                continue
            candidates.append(
                to_candidate(
                    msg, space, tier_hint, prefs, my_email, my_names, watched_thread_ids
                )
            )

    return candidates
