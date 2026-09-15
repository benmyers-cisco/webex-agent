"""The pulse.json contract.

Two invariants:

1. Items carry the verbatim message text, because the Hub panel shows the
   actual messages rather than a model's summary of them.
2. A failed run still writes an artifact. An empty items list and a failed run
   look identical otherwise, and the failed run would then read as a quiet hour.
3. `notified` is history, never a prediction. It is true only when a banner has
   actually fired for the item on some earlier run. build_items runs before the
   notification is attempted, so it cannot know the outcome and must not guess
   — a brand-new priority item reads False here, and the orchestrator sets it
   true afterwards only if the banner really fired.

4. The artifact is a view of the whole day, not of the last hour. Items are
   rebuilt from this run's candidates, then unresolved items from earlier runs
   today are restored beside them by carry_forward. Without that step an item
   was visible for exactly one hour and then gone, notified or not, resolved or
   not. Carried items are flagged `carried_forward` and counted in `carried`,
   because `window` still describes only what this run examined.

A further invariant lives specifically in build_items: it must never silently
drop a candidate. If the classifier returns fewer verdicts than candidates
(a truncated response, a short list from a degraded call, etc.), the missing
tail is padded with the same panel default the classifier itself uses on
omission — never escalated to priority. A message that vanishes from the
artifact with no trace is worse than one that shows up unclassified.
"""
from __future__ import annotations

import json
import os

from lib.pulse_state import fingerprint

ARTIFACT = "pulse.json"


def _missing_verdict(candidate: dict, index: int, verdict_count: int, candidate_count: int) -> dict:
    """The panel default for a candidate with no corresponding verdict.

    Mirrors pulse_classify.parse_response's own default-to-panel behavior:
    omission must never escalate to priority, and the reason must be visible
    in `why` rather than silently swallowed.
    """
    return {
        "tier": "panel",
        "trigger": candidate.get("tier_hint", "unknown"),
        "why": (
            f"No classifier verdict for candidate {index}: got {verdict_count} "
            f"verdict(s) for {candidate_count} candidate(s)."
        ),
        "draft_reply": None,
    }


def build_items(candidates, verdicts, state, now_iso) -> list[dict]:
    seen = state.get("seen", {})
    items = []
    for index, candidate in enumerate(candidates):
        if index < len(verdicts):
            verdict = verdicts[index]
        else:
            # verdicts is shorter than candidates: pad rather than truncate.
            # zip(candidates, verdicts) would silently drop this candidate.
            verdict = _missing_verdict(candidate, index, len(verdicts), len(candidates))

        fid = fingerprint(candidate)
        prior = seen.get(fid) or {}
        tier = verdict.get("tier", "panel")

        item = {
            "id": fid,
            "tier": tier,
            "source": candidate.get("source"),
            "trigger": verdict.get("trigger"),
            "channel": candidate.get("channel"),
            # Carried on the item, not just the candidate, because carry_forward
            # rebuilds a later run's panel entry from the stored item alone and
            # resolve_answered needs to know which space to ask about. Deriving
            # it back out of `link` would make the artifact's format the API for
            # a lookup, which is the kind of coupling that breaks quietly.
            "space_id": candidate.get("space_id"),
            "from": {"name": candidate.get("from_name"), "email": candidate.get("from_email")},
            "at": candidate.get("at"),
            "text": candidate.get("text"),
            "why": verdict.get("why", ""),
            "draft_reply": verdict.get("draft_reply"),
            "link": candidate.get("link"),
            "first_seen": prior.get("first_seen", now_iso),
            # Strictly historical: a banner has ACTUALLY fired for this item.
            # It must never be inferred from `tier == "priority"`, because
            # build_items runs BEFORE notify — so that inference would claim an
            # interruption that has not been attempted yet, and would still
            # claim it on a run where the banner failed. Silence reading as
            # success is the one thing this whole design exists to prevent, and
            # this field is what the Hub renders to Ben.
            "notified": bool(prior.get("notified")),
            "resolved": bool(prior.get("resolved")),
        }

        # Additive only: present (and True) only on a degraded verdict, absent
        # (never False) on a normal one. Task 11 uses its presence to refuse a
        # plain "ok" status when the classifier died, and the Hub uses it to
        # label these items rather than render a model-free panel as a
        # normal quiet hour.
        if verdict.get("classification_failed"):
            item["classification_failed"] = True

        items.append(item)
    # Verdicts beyond len(candidates) are ignored by construction: this loop
    # only ever iterates over candidates.
    return items


def partition_dropped(items) -> tuple[list[dict], list[dict]]:
    """Split `(kept, dropped)` on the classifier's relevance verdict.

    Deliberately not folded into build_items. build_items' contract is that
    every candidate becomes an item — that is what stops a truncated classifier
    response from making messages vanish — and filtering inside it would make
    "the item list is shorter than the candidate list" ambiguous between a
    deliberate drop and the bug that invariant exists to catch.

    The dropped list is returned rather than discarded so the count can reach the
    panel. A filter that silently removes things is indistinguishable from a
    quiet hour, which is the failure this whole design is built against.
    """
    kept, dropped = [], []
    for item in items:
        (dropped if item.get("tier") == "drop" else kept).append(item)
    return kept, dropped


def state_entry(item: dict) -> dict:
    """The seen-store record for an item.

    A panel or priority item stores its whole payload; a dropped one stores only
    the flags. The payload is what makes carry_forward possible without a second
    fetch or a second classification, and a dropped item never reaches the
    artifact, so a payload for it would be dead weight in a file every run reads.

    Copied, not referenced. The orchestrator mutates `notified` on the item after
    this is built, and a shared reference would make the store's contents depend
    on statement order in the caller.
    """
    entry = {
        "first_seen": item["first_seen"],
        "notified": item["notified"],
        "resolved": item["resolved"],
        "tier": item["tier"],
    }
    if item.get("tier") != "drop":
        entry["item"] = dict(item)
    return entry


def carry_forward(state, fresh_ids, resolved_ids=()) -> list[dict]:
    """Unresolved items from earlier runs today, restored for this run's artifact.

    pulse.json is rebuilt from scratch every run out of that run's candidates
    alone, so an item lived for exactly one hour and then vanished whether or not
    Ben looked in that hour. On 2026-09-15 two DMs from Aanjan Ravi were
    collected at 13:15, correctly panelled, and gone by 14:15 — never notified,
    never resolved, just no longer anywhere. The seen store had tracked
    `resolved` for them the whole time and nothing read it.

    Four exclusions, each for its own reason:

    - No stored payload. Either a dropped item or a pre-carry-forward store
      entry; there is nothing to rebuild and inventing a placeholder would put a
      hollow item on a glance surface.
    - Already in `fresh_ids`. This run collected the same message, so the fresh
      copy — with this run's verdict and `why` — wins. Carrying it too would
      double it on the panel.
    - Resolved, either previously or by `resolved_ids` this run.
    - Tier is not panel or priority. Belt to the payload rule's braces: nothing
      but those two tiers ever belongs in the artifact.

    A carried item keeps its original tier and `notified` — this is restoration,
    not re-judgement. Omission must never escalate, and neither may age.
    """
    resolved_ids = set(resolved_ids)
    fresh_ids = set(fresh_ids)
    carried = []
    for fid, entry in (state.get("seen") or {}).items():
        if not isinstance(entry, dict):
            continue
        stored = entry.get("item")
        if not isinstance(stored, dict):
            continue
        if fid in fresh_ids or fid in resolved_ids or entry.get("resolved"):
            continue
        if entry.get("tier") not in ("panel", "priority"):
            continue

        item = dict(stored)
        # The store's top-level flags win over the copy embedded in the payload:
        # the orchestrator updates them in place (a banner that fired on a later
        # run, a resolution), and the embedded copy is a snapshot of the run that
        # wrote it.
        item["id"] = fid
        item["tier"] = entry.get("tier")
        item["notified"] = bool(entry.get("notified"))
        item["resolved"] = False
        # Additive and only ever True, matching classification_failed. The
        # artifact's `window` describes what this run examined, and a carried
        # item is by definition older than that — without the flag the window
        # and the items disagree with no way to tell which is wrong.
        item["carried_forward"] = True
        carried.append(item)
    return carried


def build_payload(items, sources, window_from, window_to, now_iso, notified,
                  day=None, status="ok", filtered=0, carried=0) -> dict:
    return {
        "generated_at": now_iso,
        # Ben's local day, passed in explicitly. now_iso[:10] is UTC and
        # disagrees with the locally-derived `today` that archive_if_new_day
        # compares against for part of every evening (e.g. 9pm ET is already
        # past midnight UTC). day=None falling back to now_iso[:10] is a
        # last resort, not the normal path.
        "day": day or now_iso[:10],
        "status": status,
        "window": {"from": window_from, "to": window_to},
        "sources": sources,
        "notified_this_run": bool(notified),
        # How many candidates the relevance filter removed. Reported so an empty
        # panel can say "nothing relevant came in, 14 filtered" rather than
        # letting an aggressive filter pass for a quiet hour.
        "filtered": int(filtered),
        # How many of `items` came from an earlier run today rather than from
        # this run's window. Reported for the same reason `filtered` is: the
        # window no longer bounds the item list, and a reader that assumes it
        # does would misread a carried item as brand-new activity.
        "carried": int(carried),
        "items": items,
    }


def failure_payload(reason: str, now_iso: str, day=None) -> dict:
    return {
        "generated_at": now_iso,
        "day": day or now_iso[:10],
        "status": "failed",
        "reason": reason,
        "window": None,
        "sources": {},
        "notified_this_run": False,
        # Nothing was classified, so nothing was filtered, and nothing was
        # carried — a failed run does not get to speak for earlier ones. Present
        # rather than absent so consumers never have to distinguish "zero" from
        # "no key".
        "filtered": 0,
        "carried": 0,
        "items": [],
    }


def write_payload(payload: dict, output_dir: str) -> str:
    os.makedirs(output_dir, exist_ok=True)
    path = os.path.join(output_dir, ARTIFACT)
    # Per-process, because StartCalendarInterval deliberately permits overlapping
    # runs and nothing bounds a run's duration — no timeout on the Bedrock call
    # in pulse_classify, none on the Webex HTTP calls, and the plist's TimeOut
    # key does not kill a calendar-interval job. Two concurrent writers
    # truncating one shared "pulse.json.tmp" can os.replace mixed bytes into
    # place. Ruling 79 gave the wrapper a distinct temp name for this reason;
    # this closes the python-vs-python half.
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w") as fh:
        json.dump(payload, fh, indent=2)
    os.replace(tmp, path)
    return path


def archive_if_new_day(output_dir: str, today: str) -> str | None:
    """Move a previous day's artifact aside. Returns the archive path, or None."""
    path = os.path.join(output_dir, ARTIFACT)
    if not os.path.exists(path):
        return None
    try:
        with open(path) as fh:
            existing = json.load(fh)
    except (OSError, ValueError):
        return None

    day = existing.get("day") or (existing.get("generated_at") or "")[:10]
    if not day or day == today:
        return None

    archive = os.path.join(output_dir, f"pulse-{day}.json")
    os.replace(path, archive)
    return archive
