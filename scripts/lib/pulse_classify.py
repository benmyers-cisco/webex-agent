"""Urgency classification.

The daily triage is a relevance engine: it is supposed to return four things.
This is an urgency engine and its normal answer is "nothing". The single
question it asks is whether an item can wait for the next briefing, and the
next briefing is passed in rather than inferred, because after 16:00 it is
tomorrow morning and the bar therefore rises.

Unclassified candidates default to `panel`, never `priority`. A model that
drops an item must not be able to escalate it by omission. And a failure to
call or parse the model at all must never come back as a clean empty list —
that would read as "nothing was urgent" when the truth is "nothing was
checked". Failures degrade to an all-panel result with the failure spelled
out in `why`, printed loudly to stderr as well, so the run's own artifacts
can never be mistaken for a quiet hour.
"""
from __future__ import annotations

import json
import os
import re
import sys

MODEL_BEDROCK = "us.anthropic.claude-sonnet-4-20250514-v1:0"
MODEL_DIRECT = "claude-sonnet-4-6-20250514"
MAX_TOKENS = 4000
VALID_TIERS = ("priority", "panel")

_JSON_BLOCK = re.compile(r"\{.*\}", re.S)


def _model() -> str:
    return MODEL_BEDROCK if os.environ.get("CLAUDE_CODE_USE_BEDROCK") == "true" else MODEL_DIRECT


def _flags(candidate: dict) -> str:
    flags = []
    if candidate.get("is_watchlist"):
        flags.append("WATCHLIST")
    if candidate.get("is_direct_mention"):
        flags.append("DIRECT-MENTION")
    if candidate.get("is_watched_thread"):
        flags.append("THREAD-YOU-ARE-IN")
    flags.append(f"tier={candidate.get('tier_hint')}")
    return " ".join(flags)


def build_prompt(candidates, prefs_text, now_iso, briefing_label, briefing_horizon, meetings) -> str:
    blocks = []
    for i, candidate in enumerate(candidates):
        blocks.append(
            f"[{i}] {_flags(candidate)}\n"
            f"    channel: {candidate.get('channel')}\n"
            f"    from: {candidate.get('from_name')} <{candidate.get('from_email')}>\n"
            f"    at: {candidate.get('at')}\n"
            f"    text: {candidate.get('text')}"
        )

    meeting_block = ""
    if meetings:
        lines = "\n".join(f"- {m['subject']} at {m['start']}" for m in meetings)
        meeting_block = f"\n\nMeetings starting soon on Ben's calendar:\n{lines}"

    return f"""You triage messages for Ben Myers (benmyers@cisco.com), a Cisco PM on Identity
Intelligence / Identity Fabric. Current time: {now_iso}.

There are exactly two outcomes, and one question decides between them:

    **Can this wait until {briefing_label}?**

If yes, it is `panel`. If no, it is `priority`.

Ben already gets a thorough relevance briefing at 08:30 and 16:00. Your job is
NOT to find what is important — most of what reaches him is important AND can
wait {briefing_horizon}. Your job is to find the rare thing that cannot. When in
doubt, choose `panel`. An unnecessary interruption is a worse error than a
delayed one.

`priority` requires at least one of:
- The sender is flagged WATCHLIST. This alone is sufficient.
- An unanswered direct ask to Ben where the asker cannot proceed without him,
  in a tier=p1 or tier=dm space.
- A deadline inside roughly 24 hours, or an explicit ask for today / EOD /
  by tomorrow morning.
- An escalation: customer-impacting, an outage, a named customer blocked, or an
  exec escalation.
- A message about one of the meetings listed below as starting soon.
- tier=p2 ONLY when DIRECT-MENTION or THREAD-YOU-ARE-IN is flagged.

`panel` — never priority, no matter how interesting:
- Being tagged or mentioned with no ask aimed at Ben
- A discussion Ben could improve but that is proceeding fine without him
- A decision made without him that needs no same-day answer
- Anything already answered by someone else
- Anything where Ben sent the last message
- Status updates, FYIs, shared documents
- tier=slack_panel is ALWAYS panel — the sender is an automated relay

USER PREFERENCES (for judging relevance and noise):
{prefs_text}{meeting_block}

CANDIDATES:
{chr(10).join(blocks)}

Respond with JSON only, no prose:

{{"items": [
  {{"index": 0,
    "tier": "priority",
    "trigger": "watchlist|p1_channel|p2_mention|p2_thread|dm|meeting_imminent|email",
    "why": "one sentence, specific, naming who needs what",
    "draft_reply": "a ready-to-send reply, or null for panel items"}}
]}}

Include an entry for every candidate index.
"""


def parse_response(text: str, candidates: list[dict]) -> list[dict]:
    match = _JSON_BLOCK.search(text or "")
    if not match:
        raise ValueError("classifier returned no JSON object")
    try:
        payload = json.loads(match.group(0))
    except ValueError as exc:
        raise ValueError(f"classifier JSON did not parse: {exc}") from exc

    if not isinstance(payload, dict):
        raise ValueError("classifier JSON was not an object")

    # Default everything to panel. Omission must never escalate.
    out = [
        {"tier": "panel", "trigger": candidate.get("tier_hint", "unknown"),
         "why": "Not classified by the model; defaulted to panel.", "draft_reply": None}
        for candidate in candidates
    ]

    items = payload.get("items", [])
    if not isinstance(items, list):
        # Malformed shape — not a fatal error, just nothing to apply.
        # Omission must never escalate, so everything stays at panel.
        items = []

    for entry in items:
        if not isinstance(entry, dict):
            continue
        index = entry.get("index")
        if not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < len(out):
            continue
        tier = entry.get("tier")
        out[index] = {
            "tier": tier if tier in VALID_TIERS else "panel",
            "trigger": entry.get("trigger") or candidates[index].get("tier_hint", "unknown"),
            "why": entry.get("why") or "",
            "draft_reply": entry.get("draft_reply"),
        }

    return out


def _extract_text(response) -> str:
    content = getattr(response, "content", None)
    if not content:
        raise ValueError("classifier response had no content")
    first = content[0]
    text = getattr(first, "text", None)
    if text is None and isinstance(first, dict):
        text = first.get("text")
    if not isinstance(text, str):
        raise ValueError("classifier response content had no usable text")
    return text


def classify(client, candidates, prefs_text, now_iso, briefing_label, briefing_horizon, meetings):
    if not candidates:
        return []

    prompt = build_prompt(
        candidates, prefs_text, now_iso, briefing_label, briefing_horizon, meetings
    )

    try:
        response = client.messages.create(
            model=_model(),
            max_tokens=MAX_TOKENS,
            messages=[{"role": "user", "content": prompt}],
        )
        text = _extract_text(response)
        return parse_response(text, candidates)
    except Exception as exc:  # noqa: BLE001 — any failure degrades, none propagates
        # A silent empty return here would read as "nothing was urgent" when
        # the truth is "nothing was checked". Say so loudly, and account for
        # every candidate as panel rather than dropping them.
        print(
            f"ERROR: pulse classifier failed ({exc}); defaulting "
            f"{len(candidates)} candidate(s) to panel.",
            file=sys.stderr,
        )
        return [
            {"tier": "panel", "trigger": candidate.get("tier_hint", "unknown"),
             "why": f"Classification failed: {exc}", "draft_reply": None}
            for candidate in candidates
        ]
