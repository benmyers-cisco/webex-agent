"""Tier-aware parsing of preferences.md.

daily_summary._parse_space_lists deliberately flattens Priority 1/2/3 into one
set, because the daily triage scans every listed space the same way. The pulse
cannot reuse it: P1 means "interrupt me" and P2 means "only if I'm tagged", so
the tiers have to stay apart. This is the one piece of preferences parsing that
is duplicated rather than imported, and that is the reason.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

P1_HEADER = re.compile(r"^###\s+Priority\s+1\b")
P2_HEADER = re.compile(r"^###\s+Priority\s+2\b")
# The Hub's watch form writes four destinations; this is the third. Without it
# "Mentions only" resolved to "unlisted" and was therefore identical to "Never
# scan" — a demoted channel went silent instead of surfacing @mentions.
MENTIONS_HEADER = re.compile(r"^##\s+Mentions\s+Only\b")
NEVER_HEADER = re.compile(r"^##\s+Never\s+Scan\b")
WATCHLIST_HEADER = re.compile(r"^##\s+Watchlist\b")
ANY_HEADER = re.compile(r"^#{1,6}\s")
# "Rory Scott <rorscott@cisco.com>" — angle brackets are required so a bare
# name is reported as unresolved rather than silently matching nothing.
WATCHLIST_ENTRY = re.compile(r"^(?P<name>.+?)\s*<(?P<email>[^>]+)>\s*$")


def _norm(title: str) -> str:
    return title.strip().lower()


@dataclass
class PulsePrefs:
    p1: set[str] = field(default_factory=set)
    p2: set[str] = field(default_factory=set)
    mentions: set[str] = field(default_factory=set)
    never: set[str] = field(default_factory=set)
    watchlist: dict[str, str] = field(default_factory=dict)
    unresolved: list[str] = field(default_factory=list)

    def tier_of(self, title: str) -> str:
        """The loudest tier this title appears in.

        Precedence is explicit and descending because a Hub "move" that leaves a
        stale duplicate behind would otherwise silently demote a P1 channel.

        `## Never Scan` used to be left unparsed on the grounds that "unlisted"
        already means "do not fetch", so naming it twice bought nothing. Making
        group chats unconditionally eligible ended that: an unlisted space whose
        title trips the group-chat heuristic is now fetched and escalated to P1,
        so Never Scan and unlisted have genuinely different behaviour and the
        explicit exclusion has to be readable. It is checked FIRST, ahead of even
        p1 — a title in both lists is a contradiction, and the safe reading of a
        contradiction is the quieter one.
        """
        key = _norm(title)
        if key in self.never:
            return "never"
        if key in self.p1:
            return "p1"
        if key in self.p2:
            return "p2"
        if key in self.mentions:
            return "mentions"
        return "unlisted"

    def is_watchlist(self, email: str) -> bool:
        return (email or "").strip().lower() in self.watchlist


def _bullets_under(lines: list[str], start: int) -> list[str]:
    """Bullet text under a header, stopping at the next header of any level."""
    out = []
    for line in lines[start + 1:]:
        stripped = line.strip()
        if ANY_HEADER.match(stripped):
            break
        if stripped.startswith("- "):
            out.append(stripped[2:].strip())
    return out


def _find(lines: list[str], pattern: re.Pattern) -> int:
    for i, line in enumerate(lines):
        if pattern.match(line.strip()):
            return i
    return -1


def parse_prefs(text: str) -> PulsePrefs:
    lines = text.split("\n")
    prefs = PulsePrefs()

    idx = _find(lines, P1_HEADER)
    if idx != -1:
        prefs.p1 = {_norm(t) for t in _bullets_under(lines, idx)}

    idx = _find(lines, P2_HEADER)
    if idx != -1:
        prefs.p2 = {_norm(t) for t in _bullets_under(lines, idx)}

    idx = _find(lines, MENTIONS_HEADER)
    if idx != -1:
        prefs.mentions = {_norm(t) for t in _bullets_under(lines, idx)}

    idx = _find(lines, NEVER_HEADER)
    if idx != -1:
        prefs.never = {_norm(t) for t in _bullets_under(lines, idx)}

    idx = _find(lines, WATCHLIST_HEADER)
    if idx != -1:
        for entry in _bullets_under(lines, idx):
            match = WATCHLIST_ENTRY.match(entry)
            if match:
                prefs.watchlist[match.group("email").strip().lower()] = match.group("name").strip()
            else:
                # A name with no address matches no Webex message, forever. Report it.
                prefs.unresolved.append(entry)

    return prefs
