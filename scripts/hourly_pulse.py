#!/usr/bin/env python3
"""Hourly pulse — the urgency engine.

Orchestration only. Every decision lives in a lib module so it can be tested
without network, model, or clock. See
docs/superpowers/specs/2026-09-09-hourly-pulse-design.md.

NEVER writes .last_run. That file belongs to daily_summary.py, and advancing it
here would silently cut the 08:30 briefing's lookback to one hour. This module
writes only .last_pulse_run, and reads .watched_threads.json without ever
writing it.

Three failure rules shape the code below:

1. Silence must never read as success. Any escaping exception still writes a
   status="failed" artifact, because a run that crashes and writes nothing is
   indistinguishable on disk from a quiet hour.
2. A failed run does NOT advance the watermark. Advancing it would permanently
   discard the failed run's own window — no later run would ever scan those
   messages, and an urgent one would be lost in silence. Leaving it alone means
   the next successful run re-scans that window, and the seen store dedupes
   anything already notified. The 08:30 day floor in pulse_state.pulse_window
   bounds how far the window can regrow, so there is no runaway.
3. Omission must never escalate. Every default, pad, and fallback lands on the
   non-interrupting `panel` tier.
4. `notified` records what actually happened, never what was expected to. It
   becomes true only when a banner really fired, which is why a priority item
   stays eligible for a retry until one does — an urgent message whose banner
   hit a permission dialog or a timeout would otherwise never interrupt Ben at
   all. The local day boundary resets the store and is the retry bound.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from lib import pulse_classify, pulse_notify, pulse_output, pulse_state  # noqa: E402
from lib import pulse_sources_calendar, pulse_sources_email, pulse_sources_webex  # noqa: E402
from lib.pulse_prefs import parse_prefs  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PREFS_PATH = os.path.join(REPO, "preferences.md")
OUTPUT_DIR = os.path.join(REPO, "output")
STATE_PATH = os.path.join(REPO, ".pulse_seen.json")
LAST_RUN_PATH = os.path.join(REPO, ".last_pulse_run")
MY_EMAIL = "benmyers@cisco.com"
MY_NAMES = ["Ben Myers", "Myers, Ben"]

# No real UTC offset exceeds this (Pacific/Kiritimati is +14).
MAX_OFFSET_HOURS = 14

# The classifier catches every exception internally and comes back with
# all-panel verdicts flagged classification_failed, so a model outage arrives
# here as data, not as a raised exception. A run where the model was down but
# the messages arrived is DEGRADED, not failed: every message is on the panel,
# none of it was ranked. Surfaced through `sources` because the Hub already
# renders any non-"ok" source value as degraded — no new status value needed.
CLASSIFIER_DEGRADED = (
    "degraded: the classifier failed; every message is shown but none was ranked"
)


def run(deps: dict) -> dict:
    now = deps["now"]
    now_iso = now.isoformat()
    # Coerced, not trusted. This runs BEFORE the guard below is in scope — that
    # ordering is required, since the guard needs now_iso and today to write
    # anything at all — so an unusable offset here would raise with no artifact
    # written, which is the one outcome this whole design exists to prevent.
    offset_hours = _coerce_offset_hours(deps.get("tz_offset_hours"))
    local_now = now + timedelta(hours=offset_hours)
    today = local_now.strftime("%Y-%m-%d")

    # Broad on purpose. The source and output modules degrade rather than raise,
    # but the output builders deliberately raise on hostile input (verdicts=None,
    # a non-dict verdict, a corrupted seen store that loads as a string), and
    # every one of those must still leave an artifact saying the run failed.
    try:
        return _run(deps, now, now_iso, local_now, today, offset_hours)
    except Exception as exc:  # noqa: BLE001 — see rule 1 in the module docstring
        reason = f"{type(exc).__name__}: {exc}"
        print(f"ERROR: pulse run failed ({reason})", file=sys.stderr)
        payload = pulse_output.failure_payload(reason, now_iso, day=today)
        pulse_output.write_payload(payload, deps["output_dir"])
        # Deliberately no watermark write here — see rule 2.
        return payload


def _run(deps: dict, now, now_iso: str, local_now, today: str, offset_hours: float) -> dict:
    pulse_output.archive_if_new_day(deps["output_dir"], today)
    prefs = parse_prefs(deps["prefs_text"])
    state = pulse_state.load_state(deps["state_path"], today)
    # The coerced offset, not deps["tz_offset_hours"]: the window's 08:30 floor
    # and the day boundary above must agree, or the first run of a day scans a
    # window the seen store was not reset for.
    window_from = pulse_state.pulse_window(
        _read(deps["last_run_path"]), now, offset_hours
    )
    briefing_label, briefing_horizon = pulse_state.next_briefing_at(local_now)

    sources: dict[str, str] = {}

    # (candidates, status), not a bare list. The status is not decoration: a run
    # where every per-space get_messages failed returns zero candidates, and
    # hardcoding "ok" beside that made a total Webex blackout indistinguishable
    # from a quiet hour on the panel. All three sources now report their own
    # health the same way.
    candidates, sources["webex"] = deps["collect_webex"](
        prefs=prefs, since=window_from, my_email=deps["my_email"],
        my_names=deps["my_names"],
    )
    candidates = list(candidates)

    email_candidates, sources["email"] = deps["collect_email"](
        since_iso=window_from.isoformat(), prefs=prefs, my_email=deps["my_email"]
    )
    candidates += email_candidates

    meetings, sources["calendar"] = deps["collect_calendar"](now=now)

    verdicts = deps["classify"](
        candidates=candidates, prefs_text=deps["prefs_text"], now_iso=now_iso,
        briefing_label=briefing_label, briefing_horizon=briefing_horizon,
        meetings=meetings,
    )

    items = pulse_output.build_items(candidates, verdicts, state, now_iso)

    if any(item.get("classification_failed") for item in items):
        sources["classifier"] = CLASSIFIER_DEGRADED

    # A priority item is eligible for a banner until one has actually fired for
    # it. `notified` is history, not "have we seen this before", so an
    # unanswered ask from 09:15 still does not re-fire every hour — it was
    # notified, so it is not eligible. What DOES re-fire is an item whose banner
    # failed: a TCC permission dialog, an osascript timeout. That is transient
    # failure, which is exactly what a retry is for, and the alternative is an
    # urgent message that never interrupts Ben at all.
    #
    # No retry cap, deliberately: load_state resets the seen store at the local
    # day boundary, so the worst case is bounded at one workday of attempts,
    # each already bounded by pulse_notify's own subprocess timeout.
    fresh_priority = [
        item for item in items
        if item["tier"] == "priority" and not item["notified"]
    ]
    # Captured BEFORE the loop below mutates state["seen"]: testing against a
    # set you are still building answers the wrong question.
    fresh_ids = {item["id"] for item in fresh_priority}

    # Exactly one call, whatever the item count. pulse_notify.notify fires one
    # banner per call, so "one notification per run" is this line's obligation.
    notified = deps["notify"](fresh_priority)

    for item in items:
        # Read it plainly: it stays notified if it already was, and becomes
        # notified if the banner fired this run and this item was in that
        # banner. Both halves are load-bearing — drop the first and a quiet run
        # forgets the interruption already delivered; drop the second and the
        # banner's success is never recorded, so Ben gets interrupted again next
        # hour about the same message.
        #
        # Folded back onto the item, not just into the store, because
        # build_items necessarily ran before the banner was attempted and the
        # artifact is written after it. The Hub reads the artifact: an item Ben
        # was just interrupted about must not read as un-notified there, and the
        # artifact must not disagree with the store the next run loads.
        item["notified"] = item["notified"] or (notified and item["id"] in fresh_ids)
        state["seen"][item["id"]] = {
            "first_seen": item["first_seen"],
            "notified": item["notified"],
            "resolved": item["resolved"],
            "tier": item["tier"],
        }
    pulse_state.save_state(deps["state_path"], state)

    payload = pulse_output.build_payload(
        items, sources, window_from.isoformat(), now_iso, now_iso, notified,
        day=today,
    )
    pulse_output.write_payload(payload, deps["output_dir"])
    _write(deps["last_run_path"], now_iso)
    return payload


def _read(path: str) -> str | None:
    try:
        with open(path) as fh:
            return fh.read().strip() or None
    except OSError:
        return None


def _write(path: str, value: str) -> None:
    with open(path, "w") as fh:
        fh.write(value)


def _coerce_offset_hours(value) -> float:
    """A usable UTC offset in hours, or 0.0 (UTC) if `value` is not one.

    Degrade, never raise. This is called before ruling 35's guard can cover
    anything, because the guard needs `today` in order to write a failure
    artifact at all — so raising here produces NO artifact, and silence reads as
    a quiet hour. UTC is the right fallback under "omission must never
    escalate": a day boundary off by hours shifts when the artifact archives and
    when the seen store resets, which is visible and harmless, and it cannot
    promote anything to `priority`. Writing nothing is neither.

    Unreachable from main(), which builds this from _local_offset_hours(). It
    exists because the constraint is unconditional and because deps is a
    public-ish seam with more than one caller.
    """
    try:
        offset = float(value)
    except (TypeError, ValueError):
        print(
            f"WARNING: unusable tz_offset_hours {value!r}; "
            "falling back to UTC for the local day boundary",
            file=sys.stderr,
        )
        return 0.0

    # NaN and infinity are floats that make timedelta() raise, and a real UTC
    # offset never exceeds 14 hours (Pacific/Kiritimati).
    if offset != offset or abs(offset) > MAX_OFFSET_HOURS:
        print(
            f"WARNING: tz_offset_hours {value!r} is not a real UTC offset; "
            "falling back to UTC for the local day boundary",
            file=sys.stderr,
        )
        return 0.0
    return offset


def _local_offset_hours(now: datetime | None = None) -> float:
    """The UTC offset in effect at `now` (default: this moment), per the OS.

    time.daylight is a static build-time flag ("does this zone ever observe
    DST"), not "is DST in effect at this moment", so keying off it returns the
    wrong offset for part of every year. Ben is US Eastern: that is the
    difference between a correct local day boundary and one an hour off for
    eight months — and the local day boundary decides when the artifact
    archives and when the seen store resets.

    `now` exists only so a test can ask for a date in the other half of the
    year; without it a September test agrees with the buggy expression and
    proves nothing.
    """
    return (now or datetime.now()).astimezone().utcoffset().total_seconds() / 3600


def main() -> int:
    from daily_summary import (
        get_claude_client,
        get_watched_thread_spaces,
        get_webex_client,
        load_watched_threads,
    )

    webex = get_webex_client()
    client = get_claude_client()

    # Read-only. daily_summary.py owns this file; see Global Constraints. Without
    # it the spec's P2 trigger "new activity in a thread he's been tagged in"
    # could never fire, because nothing else knows which threads those are.
    watched = load_watched_threads()

    with open(PREFS_PATH) as fh:
        prefs_text = fh.read()

    prefs = parse_prefs(prefs_text)
    if prefs.unresolved:
        # A watchlist name with no address matches nothing, forever. Loud, not silent.
        print(f"WARNING: watchlist entries with no email: {prefs.unresolved}", file=sys.stderr)

    payload = run({
        "now": datetime.now(timezone.utc),
        "tz_offset_hours": _local_offset_hours(),
        "prefs_text": prefs_text,
        "output_dir": OUTPUT_DIR,
        "state_path": STATE_PATH,
        "last_run_path": LAST_RUN_PATH,
        "my_email": MY_EMAIL,
        "my_names": MY_NAMES,
        # get_watched_thread_spaces needs the lookback, which run() computes, so
        # resolve it here inside the lambda rather than hoisting it above.
        "collect_webex": lambda **kw: pulse_sources_webex.collect(
            webex, kw["prefs"], kw["since"], kw["my_email"], kw["my_names"],
            get_watched_thread_spaces(watched, kw["since"]),
        ),
        "collect_email": lambda **kw: pulse_sources_email.collect(
            kw["since_iso"], kw["prefs"], kw["my_email"]
        ),
        "collect_calendar": lambda **kw: pulse_sources_calendar.collect(kw["now"]),
        "classify": lambda **kw: pulse_classify.classify(
            client, kw["candidates"], kw["prefs_text"], kw["now_iso"],
            kw["briefing_label"], kw["briefing_horizon"], kw["meetings"],
        ),
        "notify": pulse_notify.notify,
    })

    priority = sum(1 for i in payload["items"] if i["tier"] == "priority")
    print(f"status={payload['status']} items={len(payload['items'])} "
          f"priority={priority} notified={payload['notified_this_run']}")
    return 0 if payload["status"] == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
