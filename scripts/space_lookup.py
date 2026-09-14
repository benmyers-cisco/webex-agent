#!/usr/bin/env python3
"""Space classification helpers for /webex-watch.

Resolves space names against the *live* Webex room list and audits
preferences.md for entries that no longer match a real room.

Why live and not .known_spaces.json: that cache only accumulates rooms that were
*relevant* during some past triage run (max 20 per run), so a room absent from it
is usually just a room that's been quiet — not a room that was renamed. Auditing
against the cache produces false orphans.

Usage:
    space_lookup.py resolve <search term>   # fuzzy match, JSON lines
    space_lookup.py orphans                 # prefs entries matching no live room
    space_lookup.py unclassified [keyword…] # live channels in no list
    space_lookup.py rules-gaps              # P1 entries with no space-specific rules
"""

import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import daily_summary as ds

# Webex renders group-chat titles from member names, so "Di Yin Lu, Adam Greer"
# is a group chat, not a channel. Those are always scanned regardless of any
# list, so they're never worth classifying.
GROUP_CHAT = re.compile(r"^[^,]+,\s*[^,]+")

# Titles suggesting Ben's programs — used to filter the unclassified firehose.
DEFAULT_KEYWORDS = [
    "identity", "cii", "cui", "fabric", "duo", "meraki", "cloud control",
    "pki", "cert", "scc", "c3", "canvas", "fedramp", "iam", "sso",
]


def live_rooms() -> list[dict]:
    """Fetch the full current room list. max=1000 to avoid silent truncation."""
    webex = ds.get_webex_client()
    return webex.list_spaces(max_results=1000)


def _prefs_lists():
    return ds._parse_space_lists(ds.load_preferences())


def cmd_resolve(term: str):
    """Fuzzy-match a search term or Webex space URL against live room titles."""
    if not term:
        print("usage: space_lookup.py resolve <search term>", file=sys.stderr)
        return 2

    # A pasted Webex URL carries the room UUID; space IDs are base64 of a URN
    # ending in that UUID.
    uuid_match = re.search(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})", term)
    needle = term.strip().lower()

    matches = []
    for room in live_rooms():
        title = room.get("title", "")
        sid = room.get("id", "")
        if uuid_match:
            try:
                import base64
                decoded = base64.b64decode(sid + "==").decode("utf-8", "ignore")
            except Exception:
                decoded = ""
            if uuid_match.group(1) in decoded:
                matches.append((sid, title, room.get("type", "group")))
                break
        elif needle in title.lower():
            matches.append((sid, title, room.get("type", "group")))

    if not matches:
        print(json.dumps({"matches": 0}))
        return 1

    for sid, title, rtype in matches:
        print(json.dumps({
            "id": sid,
            # repr() so trailing/leading whitespace in the title is visible —
            # the exact string is what must be written to preferences.md.
            "title": title,
            "title_repr": repr(title),
            "type": rtype,
            "is_group_chat": bool(GROUP_CHAT.match(title)),
        }, ensure_ascii=False))
    return 0


def cmd_orphans():
    """Preferences entries that match no live room — renames, typos, or descriptions."""
    always, never = _prefs_lists()
    rooms = live_rooms()
    live = {r.get("title", "").strip().lower() for r in rooms}
    titles_by_lower = {r.get("title", "").strip().lower(): r.get("title", "") for r in rooms}

    for label, entries in (("Always Scan", always), ("Never Scan", never)):
        orphans = sorted(e for e in entries if e not in live)
        if not orphans:
            continue
        print(f"{label} — {len(orphans)} entry(s) matching no live room:")
        for o in orphans:
            # Propose a correction: any live title sharing a distinctive word.
            words = [w for w in re.findall(r"[a-z0-9]{4,}", o)]
            candidates = [
                orig for low, orig in titles_by_lower.items()
                if sum(w in low for w in words) >= max(1, len(words) // 2)
            ][:3]
            hint = f"  → possible match: {candidates}" if candidates else ""
            print(f"  - {o}{hint}")
        print()

    return 0


def cmd_unclassified(keywords: list[str], limit: int = 25):
    """Live channels in no list at all — keyword-filtered, then ranked by recency.

    Keyword filtering alone leaves ~200 rooms, which is not a worklist. The rooms
    come back sorted by lastActivity, so the recent tail is the part that's
    actually costing anything by being unclassified.
    """
    always, never = _prefs_lists()
    kws = [k.lower() for k in keywords] if keywords else DEFAULT_KEYWORDS

    matched, skipped_irrelevant, skipped_groups = [], 0, 0
    for room in live_rooms():
        title = room.get("title", "")
        low = title.strip().lower()
        if low in always or low in never:
            continue
        if room.get("type") == "direct" or GROUP_CHAT.match(title):
            skipped_groups += 1
            continue
        if not any(k in low for k in kws):
            skipped_irrelevant += 1
            continue
        matched.append((title, room.get("lastActivity", "")))

    matched.sort(key=lambda x: x[1], reverse=True)
    shown = matched[:limit]

    for title, last in shown:
        print(f"  - {title}   (last activity {last[:10] or 'unknown'})")
    print(f"\nShowing the {len(shown)} most recently active of {len(matched)} unclassified "
          f"channel(s) matching {'your keywords' if keywords else 'program keywords'}.")
    print(f"Also filtered out: {skipped_irrelevant} off-topic channel(s), "
          f"{skipped_groups} DM/group chat(s) (always scanned regardless of any list).")
    if len(matched) > len(shown):
        print(f"{len(matched) - len(shown)} older match(es) not shown — pass a narrower "
              f"keyword to see them.")
    return 0


def cmd_rules_gaps():
    """Priority 1 channels with no ### block under Space-Specific Rules."""
    prefs = ds.load_preferences()

    # Collect the P1 tier specifically — _parse_space_lists flattens the tiers,
    # so re-walk the file to keep them separate.
    p1, in_always, in_p1 = [], False, False
    for line in prefs.split("\n"):
        s = line.strip()
        low = s.lower()
        if low.startswith("## always scan"):
            in_always = True
            continue
        if s.startswith("## "):
            in_always = in_p1 = False
            continue
        if in_always and s.startswith("### "):
            in_p1 = "priority 1" in low
            continue
        if in_p1 and s.startswith("- "):
            p1.append(s[2:].strip())

    # Existing rule blocks
    rules, in_rules = set(), False
    for line in prefs.split("\n"):
        s = line.strip()
        if s.lower().startswith("## space-specific rules"):
            in_rules = True
            continue
        if s.startswith("## "):
            in_rules = False
            continue
        if in_rules and s.startswith("### "):
            rules.add(s[4:].strip().lower())

    gaps = [t for t in p1 if t.strip().lower() not in rules]
    print(f"Priority 1 channels: {len(p1)}  |  with space-specific rules: {len(p1) - len(gaps)}")
    if gaps:
        print("\nNo space-specific rules yet:")
        for g in gaps:
            print(f"  - {g}")
    return 0


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    cmd, rest = sys.argv[1], sys.argv[2:]
    if cmd == "resolve":
        return cmd_resolve(" ".join(rest))
    if cmd == "orphans":
        return cmd_orphans()
    if cmd == "unclassified":
        return cmd_unclassified(rest)
    if cmd == "rules-gaps":
        return cmd_rules_gaps()
    print(f"unknown command: {cmd}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
