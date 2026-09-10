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
# preferences.md's `## Mentions Only`. Eligible for a fetch, but only the
# messages that name Ben or come from a watchlist sender survive it — that is
# what makes the Hub's label true rather than just making the space P2.
MENTIONS_TIER = "mentions"


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


def is_group_chat(space: dict) -> bool:
    """daily_summary's group-chat heuristic, imported rather than re-copied.

    Imported lazily, for two reasons. First, daily_summary's module body runs
    load_dotenv and imports anthropic and the oauth client, and every pulse lib
    module is deliberately importable with no env, no network and no model — a
    module-level import here would end that. Second, by the time this is called
    we are inside hourly_pulse._run, so an ImportError becomes a status="failed"
    artifact rather than an import-time crash that writes nothing at all, and
    silence reading as success is the one outcome this design exists to prevent.

    Not reimplemented: the tier vocabulary already lives in three places, and a
    fourth copy of this heuristic is how it drifts out of agreement with the
    daily triage.
    """
    from daily_summary import _is_group_chat

    return _is_group_chat(space)


def space_is_eligible(space: dict, prefs, watched_space_threads: dict) -> tuple[bool, str]:
    """Whether this space's messages are worth fetching, and at which tier.

    A DM is inherently aimed at Ben, so it gets its own tier. An unlisted space
    with a thread he's been tagged in is treated as p2 — the thread earns the
    look, the space doesn't.

    A Webex ad-hoc group chat is type "group" with a participant-name title, so
    without the second half of the first check it falls through tier_of() to
    "unlisted" and is skipped. The spec puts group chats alongside DMs at P1 for
    the same reason a DM is there: a named handful of people in a room is aimed
    at Ben rather than broadcast near him. It also means such a space needs no
    entry in preferences.md — which is why the migration was right to delete the
    one it found, and wrong about the pulse honouring it.
    """
    tier = prefs.tier_of(space.get("title", ""))

    # Never Scan is checked before anything else, including the group-chat
    # heuristic. That heuristic is a guess from the title — ad-hoc Webex rooms
    # are typed "group" and titled with participant names, so a room genuinely
    # named after people trips it. Letting it run first meant a space Ben had
    # explicitly excluded could be fetched AND escalated to P1: the loudest
    # possible way to ignore an instruction. An explicit listing wins over a
    # heuristic every time.
    if tier == "never":
        return False, "never"

    if space.get("type") == "direct" or is_group_chat(space):
        return True, "dm"

    if tier in ("p1", "p2", MENTIONS_TIER):
        # MENTIONS_TIER is returned as itself, not flattened to "p2" here:
        # `collect` is the only place that can act on it, because the filter it
        # implies is per-message rather than per-space. It translates the hint.
        return True, tier

    if space.get("id") in watched_space_threads:
        return True, "p2"

    return False, "unlisted"


def created_at(msg) -> datetime | None:
    """A message's timestamp, or None if Webex sent something unparseable."""
    try:
        return datetime.fromisoformat((msg.get("created") or "").replace("Z", "+00:00"))
    except ValueError:
        return None


def my_last_message_at(messages, my_email: str) -> datetime | None:
    """When Ben last spoke in this batch — the cutoff for "already answered".

    Webex exposes no read state for the authenticated user, so there is no way
    to ask whether Ben has *seen* a message. Having spoken after it is the one
    signal available, and it is the stronger of the two anyway: a reply is
    evidence he dealt with it, where a read receipt would only say the message
    crossed his screen.

    Returns None when he has said nothing, which leaves every candidate standing.
    """
    latest = None
    me = (my_email or "").strip().lower()
    for msg in messages:
        if (msg.get("personEmail") or "").strip().lower() != me:
            continue
        at = created_at(msg)
        if at and (latest is None or at > latest):
            latest = at
    return latest


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


def collect(
    webex, prefs, since, my_email, my_names, watched_space_threads
) -> tuple[list[dict], str]:
    """Fetch candidates from every eligible space.

    `webex` is a WebexClient. `since` is a timezone-aware UTC datetime.
    `watched_space_threads` is daily_summary.get_watched_thread_spaces()'s
    output: dict[space_id, list[thread_id]]. Read-only — the pulse never writes
    .watched_threads.json.

    Ben's own messages are dropped — the pulse is about what needs him — and so
    is anything he has already replied to, which `my_last_message_at` decides.

    Note: webex_client.py has no `list_messages` method. The real fetch call
    is `get_messages(room_id, before=None, after=None, max_results=...)`,
    which already filters client-side by `after`. `collect` calls that real
    signature (per brief Step 5: "If a real signature differs, fix collect to
    match — do not change webex_client.py"). We still re-check the cutoff
    below rather than trust the client's filtering alone, since
    `get_messages` treats `after` as inclusive-at-boundary while the pulse's
    contract is strictly-after `since`.

    Returns `(candidates, status)` like its email and calendar siblings. The
    status is the only way a per-space fetch failure can reach the artifact: the
    stderr warning below goes to /tmp/webex-pulse.log, which nothing surfaces,
    so without the status a run where `get_messages` failed for EVERY space
    would write `sources: {"webex": "ok"}` with zero items and the panel would
    render a total blackout as a quiet hour. `list_spaces` raising is a
    different case and still escapes to the run guard, which writes
    status="failed" — that is louder, and correct, because without the space
    list there is no denominator to be degraded against.

    The denominator counts *attempted* spaces, not every space Webex returned:
    "3 of 400" would read as a rounding error when it is really 3 of the 17
    spaces Ben actually watches.
    """
    watched_space_threads = watched_space_threads or {}
    candidates: list[dict] = []
    attempted = 0
    failed = 0
    answered = 0

    for space in webex.list_spaces(max_results=400):
        eligible, space_tier = space_is_eligible(space, prefs, watched_space_threads)
        if not eligible:
            continue

        # A mentions-tier space is fetched in full and then filtered to the
        # messages that actually name Ben or come from a watchlist sender. The
        # hint the classifier sees is "p2", because that is the urgency rule the
        # surviving messages get judged under; "mentions" describes eligibility,
        # and eligibility is not part of the classifier's vocabulary.
        mentions_only = space_tier == MENTIONS_TIER
        tier_hint = "p2" if mentions_only else space_tier

        attempted += 1
        watched_thread_ids = set(watched_space_threads.get(space["id"], []))

        try:
            messages = webex.get_messages(
                space["id"], after=since, max_results=_MESSAGES_PER_SPACE
            )
        except Exception as exc:
            # One unreachable space must not take down the whole run, but the
            # gap must be observable — silence here would read as "nothing
            # happened in that space", which is a lie.
            failed += 1
            print(
                f"WARNING: pulse could not fetch messages for Webex space "
                f"'{space.get('title', '?')}' ({space.get('id', '?')}): {exc}",
                file=sys.stderr,
            )
            continue

        # Computed before the candidate loop, over the whole batch: a message
        # Ben has already replied to must not be surfaced, and his reply may sit
        # anywhere in the batch relative to it.
        answered_through = my_last_message_at(messages, my_email)

        for msg in messages:
            at = created_at(msg)
            if at is None:
                continue
            if at <= since:
                continue
            if (msg.get("personEmail") or "").lower() == my_email.lower():
                continue
            # Ben spoke after this, so he has dealt with it. Dropped rather than
            # demoted to the panel: the panel is for things that came in and did
            # not interrupt him, not for a record of his own replies.
            # Strictly earlier, not at-or-earlier. Equal timestamps are no
            # evidence that Ben's reply came second, and the safe reading of an
            # ambiguous order is the one that keeps the message visible.
            if answered_through is not None and at < answered_through:
                answered += 1
                continue
            candidate = to_candidate(
                msg, space, tier_hint, prefs, my_email, my_names, watched_thread_ids
            )
            # Filtered on the built candidate rather than on the raw message, so
            # the @mention rule is `mentions_ben` itself (word-boundary matched
            # and already tested) and not a second, looser copy of it.
            #
            # `is_watched_thread` belongs in this disjunction because the tier
            # hint these survivors carry is "p2", and a tagged thread is one of
            # the two ways a p2 space notifies. Without it, moving a space into
            # `## Mentions Only` was a demotion that *lost* coverage Ben already
            # had while the space sat unlisted, where a watched thread made it
            # eligible on its own.
            if mentions_only and not (
                candidate["is_direct_mention"]
                or candidate["is_watchlist"]
                or candidate["is_watched_thread"]
            ):
                continue
            candidates.append(candidate)

    # Logged, not returned in the status: anything other than "ok" there is
    # rendered as a degraded source in the Hub, and suppressing messages Ben has
    # already answered is the feature working, not a fault.
    if answered:
        print(
            f"INFO: pulse suppressed {answered} Webex message(s) Ben had "
            f"already replied to",
            file=sys.stderr,
        )

    if failed:
        return candidates, (
            f"degraded: {failed} of {attempted} spaces could not be fetched"
        )
    return candidates, "ok"
