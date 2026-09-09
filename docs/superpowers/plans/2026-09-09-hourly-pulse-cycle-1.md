# Hourly Pulse — Cycle 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** An hourly job that surfaces only messages that cannot wait until the next briefing, fires exactly one notification per run when such messages exist, and shows every triggering message verbatim in a Hanuman Hub panel.

**Architecture:** A standalone Python engine (`scripts/hourly_pulse.py`) built from small, separately testable library modules under `scripts/lib/`. It collects candidates from Webex, work email, and today's calendar; classifies each as `priority` or `panel` with one Bedrock call; de-dupes against a within-day fingerprint store; writes a single JSON artifact; and fires one `osascript` notification if any newly qualifying priority items exist. Hanuman Hub reads that artifact and never writes it.

**Tech Stack:** Python 3.12 (`.venv/bin/python3.12`), `anthropic` via Bedrock, `httpx`, pytest (added in Task 1). Hub: Node 20+, Express, Vue 3, vitest. macOS launchd for scheduling, `osascript` for notifications.

**Spec:** `docs/superpowers/specs/2026-09-09-hourly-pulse-design.md` — read it before starting. It carries the priority model, the notification rule, and the list of things this must not break.

## Global Constraints

- **Never write `.last_run`.** That file belongs to `daily_summary.py`. The pulse writes `.last_pulse_run` only. A pulse run that advances `.last_run` silently reduces the 08:30 briefing's lookback to one hour.
- **Never write `.watched_threads.json`.** Same hazard, different file. `daily_summary.py` owns it via `save_watched_threads()` and prunes it on a 28-day staleness rule. The pulse reads it with `load_watched_threads()` and **never** calls `save_watched_threads()` or `prune_watched_threads()`. A pulse write would let a run nine times a day churn state the briefing depends on.
- **Never modify `scripts/daily_summary.py`.** Import from it; do not edit it. Its prompt, sections, markdown output, and schedule are all out of scope.
- **`preferences.md` is gitignored and has no history.** Read before writing, targeted edits only. A `.bak` copy is written before every change.
- **Space titles match by exact lowercased, stripped comparison.** Never write a title a human typed from memory; resolve it against live Webex with `scripts/space_lookup.py resolve`.
- **Python interpreter is always `.venv/bin/python3.12`**, run from the repo root, with `.env` sourced.
- **All timestamps are ISO 8601 UTC with an explicit offset.** Never naive datetimes.
- **`StartCalendarInterval` only, never `StartInterval`** in any launchd plist.
- **Silence must never read as success.** Every failure path writes an artifact stating that it failed.
- Model id when `CLAUDE_CODE_USE_BEDROCK=true`: `us.anthropic.claude-sonnet-4-20250514-v1:0`, matching `daily_summary.triage_with_claude`.
- Commit after every task. Cross-repo tasks (Hub, `~/.claude/commands`) commit in their own repo.

---

## File Structure

**`~/projects/webex-agent/` — new**

| File | Responsibility |
|---|---|
| `scripts/lib/pulse_prefs.py` | Tier-aware parse of `preferences.md`: P1, P2, watchlist |
| `scripts/lib/pulse_state.py` | Fingerprints, within-day seen store, window computation, daily clear |
| `scripts/lib/pulse_sources_webex.py` | Webex candidates from P1, P2, DMs, watched threads |
| `scripts/lib/pulse_sources_email.py` | msgraph email candidates + sender filtering + Slack DM parsing |
| `scripts/lib/pulse_sources_calendar.py` | Today's imminent meetings for the meeting trigger |
| `scripts/lib/pulse_classify.py` | Prompt construction, Bedrock call, response parsing |
| `scripts/lib/pulse_output.py` | `pulse.json` contract, degraded sources, failure states, archive |
| `scripts/lib/pulse_notify.py` | The single `osascript` notification |
| `scripts/hourly_pulse.py` | Orchestration only — no business logic |
| `scripts/run_pulse.sh` | launchd wrapper; reuses `lib/wait_for_network.sh` and `aws_refresh.sh` |
| `tests/` | pytest suite |

**`~/projects/claude-hub/` — new**

| File | Responsibility |
|---|---|
| `server/pulse.js` | Pure functions: read artifact, compute staleness, shape the response |
| `server/pulse.test.js` | vitest for the above |
| `server/routes/pulse.js` | Express route, thin |
| `src/components/PulseView.vue` | The panel |

**Modified**

| File | Change |
|---|---|
| `preferences.md` | P1/P2 restructure, drop `### Priority 3`, add `## Watchlist` |
| `claude-hub/server/routes/webex.js` | Drop `p3`, `ADD_TO` → P2, add `/move` |
| `claude-hub/src/components/WebexWatchForm.vue` | Tier picker, move control, fix copy |
| `claude-hub/src/lib/api.js` | `addWebexSpace` takes a section; add `moveWebexSpace` |
| `claude-hub/src/stores/notifications.js` | Pulse badge count |
| `claude-hub/src/components/Sidebar.vue` | Pulse nav item, expanded and collapsed |
| `claude-hub/src/App.vue` | Route `activeTab.target === 'pulse'` to `PulseView` |
| `claude-hub/server/index.js` | Mount the pulse router |
| `~/.claude/commands/webex-watch.md` | Rewrite — its "tiers are not mechanical" claim becomes false |

### Types used across tasks

```python
# Candidate — a raw message, before classification.
{
  "source": "webex" | "email",
  "channel": str,              # space title, or "Email", or "Slack (via email)"
  "space_id": str | None,
  "from_name": str,
  "from_email": str,
  "at": str,                   # ISO 8601 UTC
  "text": str,
  "link": str,
  "is_watchlist": bool,
  "is_direct_mention": bool,
  "is_watched_thread": bool,
  "tier_hint": "p1" | "p2" | "dm" | "unlisted" | "email" | "slack_panel",
}

# Item — a classified candidate, as written to pulse.json.
{
  "id": str, "tier": "priority" | "panel", "source": str, "trigger": str,
  "channel": str, "from": {"name": str, "email": str}, "at": str, "text": str,
  "why": str, "draft_reply": str | None, "link": str,
  "first_seen": str, "notified": bool, "resolved": bool,
}
```

---

### Task 1: Test scaffolding for webex-agent

The repo has no test framework. Everything downstream depends on this.

**Files:**
- Create: `requirements-dev.txt`
- Create: `pytest.ini`
- Create: `tests/__init__.py`
- Create: `tests/test_scaffolding.py`
- Modify: `.gitignore`

**Interfaces:**
- Consumes: nothing
- Produces: a working `pytest` invocation — `.venv/bin/python3.12 -m pytest tests/ -v` — that all later tasks use verbatim

- [ ] **Step 1: Write the failing test**

Create `tests/test_scaffolding.py`:

```python
"""Proves pytest runs and can import from scripts/lib."""


def test_pytest_can_import_pulse_lib_namespace():
    import scripts.lib  # noqa: F401
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd ~/projects/webex-agent && .venv/bin/python3.12 -m pytest tests/ -v`
Expected: FAIL — `No module named pytest`.

- [ ] **Step 3: Install pytest and create the package files**

```bash
cd ~/projects/webex-agent
printf 'pytest>=8.0.0\n' > requirements-dev.txt
.venv/bin/python3.12 -m pip install -r requirements-dev.txt
mkdir -p scripts/lib tests
touch tests/__init__.py scripts/lib/__init__.py
```

Create `pytest.ini`:

```ini
[pytest]
testpaths = tests
pythonpath = .
python_files = test_*.py
addopts = -q
```

`pythonpath = .` is what makes `import scripts.lib` resolve from the repo root. `scripts/__init__.py` is deliberately NOT created — `scripts/` already contains standalone executables and making it a package would change how they resolve their own imports.

Because `scripts` is not a package, add `tests/conftest.py`:

```python
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "scripts"))
```

And change the test to import the way real code will:

```python
"""Proves pytest runs and can import the pulse lib modules."""


def test_pytest_can_import_pulse_lib_namespace():
    import lib  # noqa: F401
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd ~/projects/webex-agent && .venv/bin/python3.12 -m pytest tests/ -v`
Expected: PASS, 1 test.

- [ ] **Step 5: Keep dev artifacts out of git**

Append to `.gitignore`:

```
.pytest_cache/
__pycache__/
.last_pulse_run
.pulse_seen.json
output/pulse*.json
```

- [ ] **Step 6: Commit**

```bash
cd ~/projects/webex-agent
git add requirements-dev.txt pytest.ini tests/ scripts/lib/__init__.py .gitignore
git commit -m "test: add pytest scaffolding for the pulse engine"
```

---

### Task 2: Tier-aware preferences parser

`_parse_space_lists` in `daily_summary.py` flattens P1/P2/P3 into one set. The pulse needs the tiers kept apart, plus a new watchlist section.

**Files:**
- Create: `scripts/lib/pulse_prefs.py`
- Test: `tests/test_pulse_prefs.py`

**Interfaces:**
- Consumes: nothing
- Produces:
  - `parse_prefs(text: str) -> PulsePrefs`
  - `PulsePrefs` dataclass with `p1: set[str]`, `p2: set[str]`, `watchlist: dict[str, str]` (email → display name), `unresolved: list[str]`
  - `PulsePrefs.tier_of(title: str) -> str` returning `"p1" | "p2" | "unlisted"`
  - `PulsePrefs.is_watchlist(email: str) -> bool`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_pulse_prefs.py`:

```python
from lib.pulse_prefs import parse_prefs

PREFS = """# Webex Agent Preferences

## My Role & Focus
- Ben Myers (benmyers@cisco.com)

## Watchlist
<!-- Anything from these people is a priority notification, in any channel. -->
- Rory Scott <rorscott@cisco.com>
- Matt Caulfield <mcaulfie@cisco.com>
- Taylor

## Always Scan
<!-- comment that must not become a title -->

### Priority 1 — Interrupt Me
- C3 + CUI
- Mini EC with CUI

### Priority 2 — Tagged Only
- SCC - CII Discussion

## Mentions Only
- Duo PM Sync

## Never Scan
- Social / watercooler channels
"""


def test_p1_and_p2_are_kept_separate():
    prefs = parse_prefs(PREFS)
    assert prefs.p1 == {"c3 + cui", "mini ec with cui"}
    assert prefs.p2 == {"scc - cii discussion"}


def test_tier_of_is_case_and_whitespace_insensitive():
    prefs = parse_prefs(PREFS)
    assert prefs.tier_of("  C3 + CUI  ") == "p1"
    assert prefs.tier_of("scc - cii discussion") == "p2"
    assert prefs.tier_of("Duo PM Sync") == "unlisted"
    assert prefs.tier_of("Something Nobody Listed") == "unlisted"


def test_watchlist_maps_email_to_name():
    prefs = parse_prefs(PREFS)
    assert prefs.watchlist["rorscott@cisco.com"] == "Rory Scott"
    assert prefs.watchlist["mcaulfie@cisco.com"] == "Matt Caulfield"


def test_watchlist_lookup_is_case_insensitive():
    prefs = parse_prefs(PREFS)
    assert prefs.is_watchlist("RorScott@Cisco.COM") is True
    assert prefs.is_watchlist("stranger@cisco.com") is False


def test_watchlist_entry_without_an_email_is_reported_not_dropped():
    prefs = parse_prefs(PREFS)
    assert prefs.unresolved == ["Taylor"]


def test_html_comments_are_never_treated_as_titles():
    prefs = parse_prefs(PREFS)
    assert not any("comment" in t for t in prefs.p1 | prefs.p2)


def test_missing_sections_yield_empty_sets_not_errors():
    prefs = parse_prefs("# Empty\n")
    assert prefs.p1 == set()
    assert prefs.p2 == set()
    assert prefs.watchlist == {}
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd ~/projects/webex-agent && .venv/bin/python3.12 -m pytest tests/test_pulse_prefs.py -v`
Expected: FAIL — `No module named 'lib.pulse_prefs'`.

- [ ] **Step 3: Implement**

Create `scripts/lib/pulse_prefs.py`:

```python
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
    watchlist: dict[str, str] = field(default_factory=dict)
    unresolved: list[str] = field(default_factory=list)

    def tier_of(self, title: str) -> str:
        key = _norm(title)
        if key in self.p1:
            return "p1"
        if key in self.p2:
            return "p2"
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
```

- [ ] **Step 4: Run to verify they pass**

Run: `cd ~/projects/webex-agent && .venv/bin/python3.12 -m pytest tests/test_pulse_prefs.py -v`
Expected: PASS, 7 tests.

- [ ] **Step 5: Commit**

```bash
cd ~/projects/webex-agent
git add scripts/lib/pulse_prefs.py tests/test_pulse_prefs.py
git commit -m "feat: tier-aware preferences parser with watchlist support"
```

---

### Task 3: Migrate preferences.md

Data migration. The parser from Task 2 is the verification tool.

**Files:**
- Modify: `~/projects/webex-agent/preferences.md`

**Interfaces:**
- Consumes: `parse_prefs` from Task 2
- Produces: a `preferences.md` whose P1 holds exactly 8 titles, P2 exactly 9, and a `## Watchlist` section with resolved addresses

- [ ] **Step 1: Back it up**

```bash
cd ~/projects/webex-agent && cp preferences.md preferences.md.pre-pulse-migration
```

The file is gitignored with no history. This backup is the only undo.

- [ ] **Step 2: Resolve the watchlist emails**

```bash
for n in "Didi Dotan" "Brian Lindauer" "Rory Scott" "Dros Adamson" \
         "Shyam Srinivasan" "Einar Nilsen-Nygaard" "Aamir Yousufzai" \
         "Matt Caulfield" "Vinita Karbhari" "Tal Surasky"; do
  printf '%s -> ' "$n"
  ~/.config/claude-graph/bin/msgraph resolve-person "$n" 2>&1 | head -3
done
```

"Taylor" is a first name only and is expected to be ambiguous or to fail. **Do not guess it.** Leave it out of the file and report it to Ben — a bare name in the watchlist would parse into `unresolved` and match nothing, which is the silent-failure mode this whole design avoids.

- [ ] **Step 3: Rewrite the tier sections**

Replace the `### Priority 1` / `### Priority 2` / `### Priority 3` block with exactly this. Leave `## My Role & Focus`, `## Mentions Only`, `## Never Scan`, `## Space-Specific Rules`, and `## Noise Patterns to Ignore` untouched.

```markdown
### Priority 1 — Interrupt Me
<!-- Activity here can fire a priority notification during the day. -->
- C3 + CUI
- Duo + Access Manager + Meraki End Users
- Identity App for C3 GA
- Identity Fabric/CUI Dependencies
- Identity in Cloud Control Working Group
- Mini EC with CUI
- PCA - Cisco Identity Fabric (Meraki Access Manager) PKI
- PureCA Identity Fabric

### Priority 2 — Tagged Only
<!-- Notifies only on a direct @mention or new activity in a thread I'm tagged
     in. Still fully scanned by the 08:30/16:00 daily triage. -->
- AI Canvas, UAIA, C3 - Product Integration Execution (PM/Eng)
- CII + Duo - Support Scope and Case Routing Project
- Cloud Control and Identity provisioning
- Duo/ CII in India SCC Needs
- IA Sprint — Fabric Folks
- Identity Fabric Requirements
- Identity Fabric Strategy & Planning
- SCC - CII Discussion
- help-identity-security-intelligence - join: https://eurl.io/#FzK_yt17m
```

Two things to get right:

`help-identity-security-intelligence - join: https://eurl.io/#FzK_yt17m` is copied **verbatim including the join URL**, because that is the literal Webex room title and matching is exact.

`Aamir Yousufzai, Ben Gaspar, Mike Wojan` is **deleted entirely**. It is a group chat, not a channel. Group chats are scanned unconditionally by `find_my_relevant_spaces` regardless of any list, and the title changes as membership changes, so the entry does nothing today and would rot.

- [ ] **Step 4: Add the watchlist section**

Insert immediately after `## My Role & Focus`, using the addresses resolved in Step 2:

```markdown
## Watchlist
<!-- Anything from these people is a priority notification, in any channel,
     including DMs and channels that appear in no list below.
     Format is strict: "Display Name <email@cisco.com>". A bare name parses as
     unresolved and matches nothing. -->
- Didi Dotan <REPLACE@cisco.com>
- Brian Lindauer <REPLACE@cisco.com>
- Rory Scott <REPLACE@cisco.com>
- Dros Adamson <REPLACE@cisco.com>
- Shyam Srinivasan <REPLACE@cisco.com>
- Einar Nilsen-Nygaard <REPLACE@cisco.com>
- Aamir Yousufzai <REPLACE@cisco.com>
- Matt Caulfield <REPLACE@cisco.com>
- Vinita Karbhari <REPLACE@cisco.com>
- Tal Surasky <REPLACE@cisco.com>
```

Every `REPLACE@cisco.com` must be substituted with a real address from Step 2. Leaving one in place would make that person's messages never notify.

- [ ] **Step 5: Verify with the parser, and verify the daily triage still sees everything**

```bash
cd ~/projects/webex-agent && .venv/bin/python3.12 -c "
import sys; sys.path.insert(0, 'scripts')
from lib.pulse_prefs import parse_prefs
from daily_summary import _parse_space_lists
text = open('preferences.md').read()
p = parse_prefs(text)
print('P1:', len(p.p1), 'expected 8')
print('P2:', len(p.p2), 'expected 9')
print('watchlist:', len(p.watchlist), 'expected 10')
assert not p.unresolved, f'UNRESOLVED: {p.unresolved}'
assert 'REPLACE@cisco.com' not in text, 'placeholder address left in the file'
always, never = _parse_space_lists(text)
print('daily triage Always Scan:', len(always), 'expected 17')
missing = (p.p1 | p.p2) - always
assert not missing, f'tier titles the daily triage cannot see: {missing}'
print('OK')
"
```

Expected: P1 8, P2 9, watchlist 10, Always Scan 17, `OK`.

That last assertion is the important one. It proves the restructure did not drop any channel out of the daily triage's coverage — the tiers moved, coverage did not.

- [ ] **Step 6: Commit**

`preferences.md` is gitignored, so there is nothing to commit. Instead, record what changed:

```bash
cd ~/projects/webex-agent
.venv/bin/python3.12 -c "
import sys; sys.path.insert(0, 'scripts')
from lib.pulse_prefs import parse_prefs
p = parse_prefs(open('preferences.md').read())
print('P1:'); [print(' -', t) for t in sorted(p.p1)]
print('P2:'); [print(' -', t) for t in sorted(p.p2)]
print('Watchlist:'); [print(' -', f'{n} <{e}>') for e, n in sorted(p.watchlist.items())]
" > docs/superpowers/plans/2026-09-09-preferences-migration-record.txt
git add docs/superpowers/plans/2026-09-09-preferences-migration-record.txt
git commit -m "docs: record the preferences.md tier migration (file itself is gitignored)"
```

---

### Task 4: Fingerprints, window, and the within-day seen store

**Files:**
- Create: `scripts/lib/pulse_state.py`
- Test: `tests/test_pulse_state.py`

**Interfaces:**
- Consumes: nothing
- Produces:
  - `fingerprint(candidate: dict) -> str`
  - `pulse_window(last_run_iso: str | None, now: datetime, tz_offset_hours: float) -> datetime`
  - `next_briefing_at(now_local: datetime) -> tuple[str, str]` → `("today 16:00", "a few hours")` or `("tomorrow 08:30", "overnight")`
  - `load_state(path: str, today: str) -> dict` → `{"day": str, "seen": {id: {...}}}`
  - `save_state(path: str, state: dict) -> None`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_pulse_state.py`:

```python
import json
from datetime import datetime, timedelta, timezone

from lib.pulse_state import (
    fingerprint,
    pulse_window,
    next_briefing_at,
    load_state,
    save_state,
)

UTC = timezone.utc


def _cand(**kw):
    base = {
        "source": "webex", "channel": "C3 + CUI", "space_id": "abc",
        "from_email": "rorscott@cisco.com", "at": "2026-09-09T14:02:11+00:00",
        "text": "Is the cross-cluster walk verified?",
    }
    base.update(kw)
    return base


def test_fingerprint_is_stable_for_the_same_message():
    assert fingerprint(_cand()) == fingerprint(_cand())


def test_fingerprint_differs_when_text_differs():
    assert fingerprint(_cand()) != fingerprint(_cand(text="different"))


def test_fingerprint_differs_across_channels_for_identical_text():
    assert fingerprint(_cand()) != fingerprint(_cand(channel="Mini EC with CUI"))


def test_fingerprint_ignores_fields_that_change_between_runs():
    # `link` and display name get rebuilt each run; they must not shift the id.
    assert fingerprint(_cand(link="x", from_name="Rob")) == fingerprint(
        _cand(link="y", from_name="Robert")
    )


def test_window_uses_last_run_when_it_is_after_the_day_floor():
    now = datetime(2026, 9, 9, 18, 15, tzinfo=UTC)  # 14:15 ET
    last = datetime(2026, 9, 9, 17, 15, tzinfo=UTC)  # 13:15 ET
    assert pulse_window(last.isoformat(), now, tz_offset_hours=-4) == last


def test_first_run_of_the_day_floors_to_0830_local():
    # 13:15 UTC = 09:15 ET. Last run was yesterday afternoon.
    now = datetime(2026, 9, 9, 13, 15, tzinfo=UTC)
    last = datetime(2026, 9, 8, 21, 15, tzinfo=UTC)
    got = pulse_window(last.isoformat(), now, tz_offset_hours=-4)
    assert got == datetime(2026, 9, 9, 12, 30, tzinfo=UTC)  # 08:30 ET


def test_missing_last_run_falls_back_to_one_hour_but_never_before_the_floor():
    now = datetime(2026, 9, 9, 18, 15, tzinfo=UTC)
    assert pulse_window(None, now, tz_offset_hours=-4) == now - timedelta(hours=1)


def test_next_briefing_is_todays_4pm_before_1600_local():
    label, horizon = next_briefing_at(datetime(2026, 9, 9, 14, 15))
    assert label == "today 16:00"
    assert horizon == "a few hours"


def test_next_briefing_is_tomorrow_morning_at_or_after_1600_local():
    label, horizon = next_briefing_at(datetime(2026, 9, 9, 16, 15))
    assert label == "tomorrow 08:30"
    assert horizon == "overnight"


def test_state_loads_empty_when_the_file_is_absent(tmp_path):
    state = load_state(str(tmp_path / "nope.json"), "2026-09-09")
    assert state == {"day": "2026-09-09", "seen": {}}


def test_state_resets_when_the_day_changed(tmp_path):
    path = tmp_path / "seen.json"
    path.write_text(json.dumps({"day": "2026-09-08", "seen": {"old": {}}}))
    state = load_state(str(path), "2026-09-09")
    assert state == {"day": "2026-09-09", "seen": {}}


def test_state_survives_within_the_same_day(tmp_path):
    path = tmp_path / "seen.json"
    path.write_text(json.dumps({"day": "2026-09-09", "seen": {"keep": {"tier": "priority"}}}))
    state = load_state(str(path), "2026-09-09")
    assert "keep" in state["seen"]


def test_state_resets_rather_than_crashing_on_corrupt_json(tmp_path):
    path = tmp_path / "seen.json"
    path.write_text("{not json")
    assert load_state(str(path), "2026-09-09") == {"day": "2026-09-09", "seen": {}}


def test_save_then_load_round_trips(tmp_path):
    path = str(tmp_path / "seen.json")
    save_state(path, {"day": "2026-09-09", "seen": {"a": {"tier": "panel"}}})
    assert load_state(path, "2026-09-09")["seen"]["a"]["tier"] == "panel"


```

- [ ] **Step 2: Run to verify they fail**

Run: `cd ~/projects/webex-agent && .venv/bin/python3.12 -m pytest tests/test_pulse_state.py -v`
Expected: FAIL — `No module named 'lib.pulse_state'`.

- [ ] **Step 3: Implement**

Create `scripts/lib/pulse_state.py`:

```python
"""Window computation and the within-day de-duplication store.

Two rules from the spec live here:

1. The pulse owns 08:30 to the last run of the day. The first run of a day
   floors its window at 08:30 local rather than reaching back to yesterday,
   because overnight traffic belongs to the 08:30 daily briefing. Without the
   floor, the 09:15 pulse re-serves everything the briefing just delivered.
2. De-duplication is within-day only. The store resets on the first run of a
   new day, and an item already surfaced never fires a second notification.
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timedelta, timezone

DAY_FLOOR_HOUR = 8
DAY_FLOOR_MINUTE = 30
AFTERNOON_BRIEFING_HOUR = 16
FALLBACK_LOOKBACK_H = 1

# Fields that identify a message. `link` and `from_name` are deliberately
# excluded: both get rebuilt each run and neither changes what was said.
_FINGERPRINT_FIELDS = ("source", "channel", "space_id", "from_email", "at", "text")


def fingerprint(candidate: dict) -> str:
    raw = "\x1f".join(str(candidate.get(f, "")) for f in _FINGERPRINT_FIELDS)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def pulse_window(last_run_iso: str | None, now: datetime, tz_offset_hours: float) -> datetime:
    """Start of this run's window, in UTC."""
    offset = timedelta(hours=tz_offset_hours)
    local_now = now + offset
    floor_local = local_now.replace(
        hour=DAY_FLOOR_HOUR, minute=DAY_FLOOR_MINUTE, second=0, microsecond=0
    )
    floor_utc = floor_local - offset

    if not last_run_iso:
        return now - timedelta(hours=FALLBACK_LOOKBACK_H)

    try:
        last = datetime.fromisoformat(last_run_iso)
    except (TypeError, ValueError):
        return now - timedelta(hours=FALLBACK_LOOKBACK_H)

    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)

    return max(last, floor_utc)


def next_briefing_at(now_local: datetime) -> tuple[str, str]:
    """Which briefing this run is measuring 'can it wait' against.

    The bar rises after 16:00: the next briefing is then tomorrow morning, so
    an item has to be unable to wait overnight rather than unable to wait a
    couple of hours.
    """
    if now_local.hour < AFTERNOON_BRIEFING_HOUR:
        return "today 16:00", "a few hours"
    return "tomorrow 08:30", "overnight"


def load_state(path: str, today: str) -> dict:
    empty = {"day": today, "seen": {}}
    if not os.path.exists(path):
        return empty
    try:
        with open(path) as fh:
            state = json.load(fh)
    except (OSError, ValueError):
        return empty
    if not isinstance(state, dict) or state.get("day") != today:
        return empty
    state.setdefault("seen", {})
    return state


def save_state(path: str, state: dict) -> None:
    tmp = f"{path}.tmp"
    with open(tmp, "w") as fh:
        json.dump(state, fh, indent=2)
    os.replace(tmp, path)


```

- [ ] **Step 4: Run to verify they pass**

Run: `cd ~/projects/webex-agent && .venv/bin/python3.12 -m pytest tests/test_pulse_state.py -v`
Expected: PASS, 14 tests.

- [ ] **Step 5: Commit**

```bash
cd ~/projects/webex-agent
git add scripts/lib/pulse_state.py tests/test_pulse_state.py
git commit -m "feat: pulse window computation and within-day dedup store"
```

---

### Task 5: Webex candidate collection

**Files:**
- Create: `scripts/lib/pulse_sources_webex.py`
- Test: `tests/test_pulse_sources_webex.py`

**Interfaces:**
- Consumes: `PulsePrefs` (Task 2)
- Produces:
  - `mentions_ben(text: str, my_email: str, my_names: list[str]) -> bool`
  - `space_is_eligible(space, prefs, watched_space_threads) -> tuple[bool, str]` → `(eligible, tier_hint)`
  - `to_candidate(msg, space, tier_hint, prefs, my_email, my_names, watched_thread_ids) -> dict`
  - `collect(webex, prefs, since, my_email, my_names, watched_space_threads) -> list[dict]`

`watched_space_threads` is exactly what `daily_summary.get_watched_thread_spaces()` returns: `dict[space_id, list[thread_id]]`. `watched_thread_ids` is one space's list as a set.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_pulse_sources_webex.py`:

```python
from lib.pulse_prefs import parse_prefs
from lib.pulse_sources_webex import mentions_ben, space_is_eligible, to_candidate

PREFS = parse_prefs("""
## Watchlist
- Rory Scott <rorscott@cisco.com>

### Priority 1 — Interrupt Me
- C3 + CUI

### Priority 2 — Tagged Only
- SCC - CII Discussion
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


def test_unlisted_space_with_a_watched_thread_is_eligible():
    ok, tier = space_is_eligible(
        {"title": "Random Chat", "type": "group", "id": "4"}, PREFS, {"4": ["thread-1"]}
    )
    assert (ok, tier) == (True, "p2")


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
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd ~/projects/webex-agent && .venv/bin/python3.12 -m pytest tests/test_pulse_sources_webex.py -v`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement**

Create `scripts/lib/pulse_sources_webex.py`:

```python
"""Webex candidate collection for the pulse.

Eligibility, not urgency. This module decides which messages the classifier
gets to look at; the classifier decides which of them can't wait.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

SPACE_LINK = "https://web.webex.com/spaces/{}"
# A single-word name is too loose — "Ben" matches "Ben Gaspar" and ordinary
# prose. Only multi-word names and the email address count as a mention.
_MIN_NAME_WORDS = 2


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
    """
    watched_space_threads = watched_space_threads or {}
    candidates: list[dict] = []

    for space in webex.list_spaces(max_results=400):
        eligible, tier_hint = space_is_eligible(space, prefs, watched_space_threads)
        if not eligible:
            continue

        watched_thread_ids = set(watched_space_threads.get(space["id"], []))

        try:
            messages = webex.list_messages(space["id"], max_results=50)
        except Exception:
            # One unreachable space must not take down the whole run.
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
```

- [ ] **Step 4: Run to verify they pass**

Run: `cd ~/projects/webex-agent && .venv/bin/python3.12 -m pytest tests/test_pulse_sources_webex.py -v`
Expected: PASS, 16 tests.

- [ ] **Step 5: Verify the two reused signatures**

```bash
cd ~/projects/webex-agent && grep -n "def list_spaces\|def list_messages" webex_client.py
grep -n "def get_watched_thread_spaces\|def load_watched_threads" scripts/daily_summary.py
```

Expected: `list_spaces(self, max_results=...)`, `list_messages(self, room_id, max_results=...)`, `get_watched_thread_spaces(watched, lookback) -> dict[str, list[str]]`, `load_watched_threads() -> dict`. If a real signature differs, fix `collect` to match — do not change `webex_client.py` or `daily_summary.py`.

- [ ] **Step 6: Commit**

```bash
cd ~/projects/webex-agent
git add scripts/lib/pulse_sources_webex.py tests/test_pulse_sources_webex.py
git commit -m "feat: Webex candidate collection with tier and mention detection"
```

---

### Task 6: Email candidate collection

**Files:**
- Create: `scripts/lib/pulse_sources_email.py`
- Test: `tests/test_pulse_sources_email.py`

**Interfaces:**
- Consumes: `PulsePrefs` (Task 2)
- Produces:
  - `AUTOMATED_SENDERS: frozenset[str]`
  - `classify_sender(msg, prefs, my_email) -> str` → `"keep" | "slack_panel" | "drop"`
  - `parse_slack_notification(msg) -> dict | None`
  - `to_candidate(msg, disposition) -> dict`
  - `collect(since_iso, prefs, my_email, runner=...) -> tuple[list[dict], str]` → `(candidates, source_status)`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_pulse_sources_email.py`:

```python
import json

from lib.pulse_prefs import parse_prefs
from lib.pulse_sources_email import (
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
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd ~/projects/webex-agent && .venv/bin/python3.12 -m pytest tests/test_pulse_sources_email.py -v`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement**

Create `scripts/lib/pulse_sources_email.py`:

```python
"""Work-email candidates via the msgraph CLI.

msgraph auth is browser-cookie scraping with no refresh token, so this source
fails regularly and unattended. That is an expected state, not an exception:
`collect` always returns a status string and never raises. A degraded email
fetch has to be visible in the panel, because a silently missing source reads
as a quiet hour.
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
AUTOMATED_LOCALPARTS = ("noreply", "no-reply", "donotreply", "do-not-reply", "notifications")


def _addr(node) -> str:
    if isinstance(node, dict):
        return (node.get("address") or "").strip().lower()
    return ""


def _recipients(msg, key) -> set[str]:
    return {_addr(r) for r in (msg.get(key) or [])}


def _is_automated(address: str) -> bool:
    local = address.split("@", 1)[0]
    return any(local.startswith(p) for p in AUTOMATED_LOCALPARTS)


def classify_sender(msg: dict, prefs, my_email: str) -> str:
    """`keep` (priority-eligible), `slack_panel` (silent only), or `drop`."""
    sender = _addr(msg.get("from"))
    me = (my_email or "").lower()

    if sender == SLACK_SENDER:
        return "slack_panel" if parse_slack_notification(msg) else "drop"

    if CALENDAR_SUBJECT.match(msg.get("subject") or ""):
        return "drop"

    if not sender or sender == me:
        return "drop"

    if _is_automated(sender):
        return "drop"

    # A watchlist sender is priority-eligible regardless of addressing.
    if prefs.is_watchlist(sender):
        return "keep"

    if me in _recipients(msg, "toRecipients") | _recipients(msg, "ccRecipients"):
        return "keep"

    return "drop"


def parse_slack_notification(msg: dict) -> dict | None:
    """Pull the human sender and message text out of a Slack notification email.

    Panel-only by construction — the sending address is automated, so this can
    never clear the priority bar. Returns None for channel-activity digests.
    """
    subject = (msg.get("subject") or "").strip()
    match = SLACK_DM_SUBJECT.match(subject) or SLACK_MENTION_SUBJECT.match(subject)
    if not match:
        return None

    who = match.group("who").strip()
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

    return {
        "from_name": who,
        "text": " ".join(text_lines) if text_lines else body.strip(),
    }


def to_candidate(msg: dict, disposition: str) -> dict:
    web_link = msg.get("web_link") or ""
    received = msg.get("received") or ""

    if disposition == "slack_panel":
        parsed = parse_slack_notification(msg) or {"from_name": "Slack", "text": ""}
        return {
            "source": "email",
            "channel": "Slack (via email)",
            "space_id": None,
            "from_name": parsed["from_name"],
            "from_email": SLACK_SENDER,
            "at": received,
            "text": parsed["text"],
            "link": web_link,
            "is_watchlist": False,
            "is_direct_mention": True,
            "is_watched_thread": False,
            "tier_hint": "slack_panel",
        }

    sender = msg.get("from") or {}
    subject = msg.get("subject") or "(no subject)"
    return {
        "source": "email",
        "channel": "Email",
        "space_id": None,
        "from_name": (sender.get("name") or _addr(sender)),
        "from_email": _addr(sender),
        "at": received,
        "text": f"Subject: {subject}\n\n{msg.get('body_preview') or ''}",
        "link": web_link,
        "is_watchlist": False,
        "is_direct_mention": True,
        "is_watched_thread": False,
        "tier_hint": "email",
    }


def _run_msgraph(args: list[str]) -> str:
    return subprocess.run(
        [MSGRAPH, *args], capture_output=True, text=True, timeout=TIMEOUT_S, check=True
    ).stdout


def collect(since_iso: str, prefs, my_email: str, runner=_run_msgraph) -> tuple[list[dict], str]:
    """Returns (candidates, status). Never raises."""
    day = (since_iso or "")[:10]
    args = ["email", "search", f"received>={day}", "--max", MAX_RESULTS]

    try:
        raw = runner(args)
    except Exception as exc:  # noqa: BLE001 — any failure degrades, none propagates
        return [], f"degraded: {exc}"

    try:
        messages = json.loads(raw).get("messages", [])
    except (ValueError, AttributeError) as exc:
        return [], f"degraded: unparseable msgraph output ({exc})"

    candidates = []
    for msg in messages:
        if (msg.get("received") or "") < since_iso:
            continue
        disposition = classify_sender(msg, prefs, my_email)
        if disposition == "drop":
            continue
        candidates.append(to_candidate(msg, disposition))

    return candidates, "ok"
```

- [ ] **Step 4: Run to verify they pass**

Run: `cd ~/projects/webex-agent && .venv/bin/python3.12 -m pytest tests/test_pulse_sources_email.py -v`
Expected: PASS, 17 tests.

- [ ] **Step 5: Verify the real msgraph output shape matches the fixtures**

```bash
~/.config/claude-graph/bin/msgraph email search "received>=2026-09-09" --max 3
```

Confirm `toRecipients` / `ccRecipients` are present with `address` keys. The observed output on 2026-09-08 included `subject`, `from.address`, `received`, `body_preview`, and `web_link` but recipient arrays were **not** verified. If they are absent, `classify_sender` must fall back to keeping any non-automated human sender and that change needs its own test.

- [ ] **Step 6: Commit**

```bash
cd ~/projects/webex-agent
git add scripts/lib/pulse_sources_email.py tests/test_pulse_sources_email.py
git commit -m "feat: email candidate collection with automated-sender filtering"
```

---

### Task 7: Imminent meetings

**Files:**
- Create: `scripts/lib/pulse_sources_calendar.py`
- Test: `tests/test_pulse_sources_calendar.py`

**Interfaces:**
- Consumes: nothing
- Produces:
  - `LOOKAHEAD_HOURS: int`
  - `imminent_meetings(events: list[dict], now: datetime, lookahead_hours: int) -> list[dict]`
  - `collect(now, runner=...) -> tuple[list[dict], str]`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_pulse_sources_calendar.py`:

```python
import json
from datetime import datetime, timezone

from lib.pulse_sources_calendar import imminent_meetings, collect

UTC = timezone.utc
NOW = datetime(2026, 9, 9, 18, 15, tzinfo=UTC)


def _event(subject, start):
    return {"subject": subject, "start": start}


def test_meeting_inside_the_window_is_returned():
    events = [_event("Cert sync", "2026-09-09T19:00:00+00:00")]
    assert [m["subject"] for m in imminent_meetings(events, NOW, 2)] == ["Cert sync"]


def test_meeting_beyond_the_window_is_excluded():
    events = [_event("Tomorrow thing", "2026-09-09T23:00:00+00:00")]
    assert imminent_meetings(events, NOW, 2) == []


def test_meeting_already_started_is_excluded():
    events = [_event("Started", "2026-09-09T17:00:00+00:00")]
    assert imminent_meetings(events, NOW, 2) == []


def test_unparseable_start_is_skipped_not_fatal():
    events = [_event("Broken", "not a date"), _event("Good", "2026-09-09T19:00:00+00:00")]
    assert [m["subject"] for m in imminent_meetings(events, NOW, 2)] == ["Good"]


def test_collect_degrades_rather_than_raising():
    def boom(_a):
        raise RuntimeError("cookie expired")

    meetings, status = collect(NOW, runner=boom)
    assert meetings == []
    assert status.startswith("degraded:")


def test_collect_parses_a_successful_run():
    payload = json.dumps({"events": [_event("Cert sync", "2026-09-09T19:00:00+00:00")]})
    meetings, status = collect(NOW, runner=lambda _a: payload)
    assert status == "ok"
    assert meetings[0]["subject"] == "Cert sync"
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd ~/projects/webex-agent && .venv/bin/python3.12 -m pytest tests/test_pulse_sources_calendar.py -v`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement**

Create `scripts/lib/pulse_sources_calendar.py`:

```python
"""Today's imminent meetings, used only as a priority trigger.

A message about a meeting starting soon is priority at any tier. This module
supplies the meeting list; the classifier decides whether a given message is
about one of them.
"""
from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timedelta, timezone

MSGRAPH = os.path.expanduser("~/.config/claude-graph/bin/msgraph")
LOOKAHEAD_HOURS = 2
TIMEOUT_S = 60


def _parse(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat((value or "").replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def imminent_meetings(events: list[dict], now: datetime, lookahead_hours: int) -> list[dict]:
    horizon = now + timedelta(hours=lookahead_hours)
    out = []
    for event in events or []:
        start = _parse(event.get("start"))
        if start is None:
            continue
        if now < start <= horizon:
            out.append({"subject": event.get("subject") or "(untitled)", "start": start.isoformat()})
    return out


def _run_msgraph(args: list[str]) -> str:
    return subprocess.run(
        [MSGRAPH, *args], capture_output=True, text=True, timeout=TIMEOUT_S, check=True
    ).stdout


def collect(now: datetime, runner=_run_msgraph) -> tuple[list[dict], str]:
    """Returns (imminent_meetings, status). Never raises."""
    try:
        raw = runner(["calendar", "1"])
    except Exception as exc:  # noqa: BLE001
        return [], f"degraded: {exc}"

    try:
        events = json.loads(raw).get("events", [])
    except (ValueError, AttributeError) as exc:
        return [], f"degraded: unparseable calendar output ({exc})"

    return imminent_meetings(events, now, LOOKAHEAD_HOURS), "ok"
```

- [ ] **Step 4: Run to verify they pass**

Run: `cd ~/projects/webex-agent && .venv/bin/python3.12 -m pytest tests/test_pulse_sources_calendar.py -v`
Expected: PASS, 6 tests.

- [ ] **Step 5: Verify the real calendar output shape**

```bash
~/.config/claude-graph/bin/msgraph calendar 1 | head -30
```

Confirm the JSON has an `events` array with `subject` and `start`. If `start` is nested (for example `{"dateTime": ..., "timeZone": ...}`), adjust `_parse` and add a test for the nested shape.

- [ ] **Step 6: Commit**

```bash
cd ~/projects/webex-agent
git add scripts/lib/pulse_sources_calendar.py tests/test_pulse_sources_calendar.py
git commit -m "feat: imminent-meeting detection for the pulse trigger"
```

---

### Task 8: The classifier

**Files:**
- Create: `scripts/lib/pulse_classify.py`
- Test: `tests/test_pulse_classify.py`

**Interfaces:**
- Consumes: the raw `preferences.md` text as `prefs_text: str` (not a `PulsePrefs`), and `next_briefing_at` (Task 4)
- Produces:
  - `MODEL_BEDROCK: str`, `MODEL_DIRECT: str`
  - `build_prompt(candidates, prefs_text, now_iso, briefing_label, briefing_horizon, meetings) -> str`
  - `parse_response(text: str, candidates: list[dict]) -> list[dict]`
  - `classify(client, candidates, prefs_text, now_iso, briefing_label, briefing_horizon, meetings) -> list[dict]`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_pulse_classify.py`:

```python
import json

import pytest

from lib.pulse_classify import build_prompt, parse_response, classify

CANDIDATES = [
    {
        "channel": "C3 + CUI", "from_name": "Rory Scott", "from_email": "rorscott@cisco.com",
        "at": "2026-09-09T14:02:11+00:00", "text": "Can you confirm before the pre-read?",
        "is_watchlist": True, "is_direct_mention": False, "is_watched_thread": False,
        "tier_hint": "p1", "source": "webex",
    },
    {
        "channel": "SCC - CII Discussion", "from_name": "Someone", "from_email": "s@cisco.com",
        "at": "2026-09-09T14:05:00+00:00", "text": "FYI the doc moved",
        "is_watchlist": False, "is_direct_mention": False, "is_watched_thread": False,
        "tier_hint": "p2", "source": "webex",
    },
]


def test_prompt_states_the_actual_test_verbatim():
    prompt = build_prompt(CANDIDATES, "prefs", "2026-09-09T18:15:00+00:00",
                          "today 16:00", "a few hours", [])
    assert "can this wait until today 16:00" in prompt.lower()
    assert "a few hours" in prompt


def test_prompt_carries_the_watchlist_flag_per_candidate():
    prompt = build_prompt(CANDIDATES, "prefs", "n", "today 16:00", "a few hours", [])
    assert "WATCHLIST" in prompt


def test_prompt_numbers_candidates_so_responses_can_be_matched_back():
    prompt = build_prompt(CANDIDATES, "prefs", "n", "today 16:00", "a few hours", [])
    assert "[0]" in prompt and "[1]" in prompt


def test_prompt_lists_imminent_meetings_when_present():
    prompt = build_prompt(CANDIDATES, "prefs", "n", "today 16:00", "a few hours",
                          [{"subject": "Cert sync", "start": "2026-09-09T19:00:00+00:00"}])
    assert "Cert sync" in prompt


def test_prompt_omits_the_meeting_block_when_there_are_none():
    prompt = build_prompt(CANDIDATES, "prefs", "n", "today 16:00", "a few hours", [])
    assert "Meetings starting soon" not in prompt


def test_parse_response_assigns_tier_and_reason():
    raw = json.dumps({"items": [
        {"index": 0, "tier": "priority", "trigger": "watchlist",
         "why": "Watchlist sender with a deadline-bound ask.", "draft_reply": "On it."},
        {"index": 1, "tier": "panel", "trigger": "p2_channel", "why": "FYI only."},
    ]})
    out = parse_response(raw, CANDIDATES)
    assert out[0]["tier"] == "priority"
    assert out[0]["trigger"] == "watchlist"
    assert out[0]["draft_reply"] == "On it."
    assert out[1]["tier"] == "panel"


def test_parse_response_tolerates_prose_around_the_json():
    raw = 'Sure, here you go:\n```json\n{"items": [{"index": 0, "tier": "panel", "why": "x"}]}\n```\nDone.'
    out = parse_response(raw, CANDIDATES[:1])
    assert out[0]["tier"] == "panel"


def test_unmentioned_candidates_default_to_panel_never_to_priority():
    # A model that forgets a candidate must not silently escalate it.
    raw = json.dumps({"items": [{"index": 0, "tier": "priority", "why": "x"}]})
    out = parse_response(raw, CANDIDATES)
    assert out[1]["tier"] == "panel"
    assert "not classified" in out[1]["why"].lower()


def test_an_out_of_range_index_is_ignored():
    raw = json.dumps({"items": [{"index": 99, "tier": "priority", "why": "x"}]})
    out = parse_response(raw, CANDIDATES)
    assert all(item["tier"] == "panel" for item in out)


def test_an_unknown_tier_value_falls_back_to_panel():
    raw = json.dumps({"items": [{"index": 0, "tier": "URGENT!!", "why": "x"}]})
    assert parse_response(raw, CANDIDATES)[0]["tier"] == "panel"


def test_unparseable_response_raises_so_the_run_reports_failure():
    with pytest.raises(ValueError):
        parse_response("the model said nothing useful", CANDIDATES)


def test_classify_returns_an_empty_list_without_calling_the_model():
    calls = []

    class Spy:
        class messages:
            @staticmethod
            def create(**kw):
                calls.append(kw)
                raise AssertionError("must not be called")

    assert classify(Spy(), [], "prefs", "n", "today 16:00", "a few hours", []) == []
    assert calls == []
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd ~/projects/webex-agent && .venv/bin/python3.12 -m pytest tests/test_pulse_classify.py -v`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement**

Create `scripts/lib/pulse_classify.py`:

```python
"""Urgency classification.

The daily triage is a relevance engine: it is supposed to return four things.
This is an urgency engine and its normal answer is "nothing". The single
question it asks is whether an item can wait for the next briefing, and the
next briefing is passed in rather than inferred, because after 16:00 it is
tomorrow morning and the bar therefore rises.

Unclassified candidates default to `panel`, never `priority`. A model that
drops an item must not be able to escalate it by omission.
"""
from __future__ import annotations

import json
import os
import re

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

    # Default everything to panel. Omission must never escalate.
    out = [
        {"tier": "panel", "trigger": candidate.get("tier_hint", "unknown"),
         "why": "Not classified by the model; defaulted to panel.", "draft_reply": None}
        for candidate in candidates
    ]

    for entry in payload.get("items", []):
        index = entry.get("index")
        if not isinstance(index, int) or not 0 <= index < len(out):
            continue
        tier = entry.get("tier")
        out[index] = {
            "tier": tier if tier in VALID_TIERS else "panel",
            "trigger": entry.get("trigger") or candidates[index].get("tier_hint", "unknown"),
            "why": entry.get("why") or "",
            "draft_reply": entry.get("draft_reply"),
        }

    return out


def classify(client, candidates, prefs_text, now_iso, briefing_label, briefing_horizon, meetings):
    if not candidates:
        return []
    prompt = build_prompt(
        candidates, prefs_text, now_iso, briefing_label, briefing_horizon, meetings
    )
    response = client.messages.create(
        model=_model(),
        max_tokens=MAX_TOKENS,
        messages=[{"role": "user", "content": prompt}],
    )
    return parse_response(response.content[0].text, candidates)
```

- [ ] **Step 4: Run to verify they pass**

Run: `cd ~/projects/webex-agent && .venv/bin/python3.12 -m pytest tests/test_pulse_classify.py -v`
Expected: PASS, 12 tests.

- [ ] **Step 5: Commit**

```bash
cd ~/projects/webex-agent
git add scripts/lib/pulse_classify.py tests/test_pulse_classify.py
git commit -m "feat: urgency classifier that defaults to panel on omission"
```

---

### Task 9: The output artifact

**Files:**
- Create: `scripts/lib/pulse_output.py`
- Test: `tests/test_pulse_output.py`

**Interfaces:**
- Consumes: `fingerprint` (Task 4)
- Produces:
  - `build_items(candidates, verdicts, state, now_iso) -> list[dict]`
  - `build_payload(items, sources, window_from, window_to, now_iso, notified, day=None, status="ok") -> dict`
  - `failure_payload(reason, now_iso, day=None) -> dict`
  - `write_payload(payload, output_dir) -> str`
  - `archive_if_new_day(output_dir, today) -> str | None`

`day` is passed explicitly and is always Ben's **local** day. Deriving it from `now_iso[:10]` inside `build_payload` would be UTC, and `archive_if_new_day` compares it against a locally-derived `today` — the two disagree for part of every evening.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_pulse_output.py`:

```python
import json
import os

from lib.pulse_output import (
    build_items,
    build_payload,
    failure_payload,
    write_payload,
    archive_if_new_day,
)

NOW = "2026-09-09T18:15:03+00:00"

CANDIDATES = [{
    "source": "webex", "channel": "C3 + CUI", "space_id": "abc",
    "from_name": "Rory Scott", "from_email": "rorscott@cisco.com",
    "at": "2026-09-09T14:02:11+00:00", "text": "Confirm before the pre-read?",
    "link": "https://web.webex.com/spaces/abc",
    "is_watchlist": True, "is_direct_mention": False, "is_watched_thread": False,
    "tier_hint": "p1",
}]
VERDICTS = [{"tier": "priority", "trigger": "watchlist", "why": "Watchlist ask.",
             "draft_reply": "On it."}]


def test_item_carries_the_verbatim_text_not_a_summary():
    items = build_items(CANDIDATES, VERDICTS, {"day": "2026-09-09", "seen": {}}, NOW)
    assert items[0]["text"] == "Confirm before the pre-read?"


def test_item_shape_matches_the_contract():
    item = build_items(CANDIDATES, VERDICTS, {"day": "2026-09-09", "seen": {}}, NOW)[0]
    for key in ("id", "tier", "source", "trigger", "channel", "from", "at", "text",
                "why", "draft_reply", "link", "first_seen", "notified", "resolved"):
        assert key in item, key
    assert item["from"] == {"name": "Rory Scott", "email": "rorscott@cisco.com"}


def test_a_new_priority_item_is_marked_notified():
    items = build_items(CANDIDATES, VERDICTS, {"day": "2026-09-09", "seen": {}}, NOW)
    assert items[0]["notified"] is True
    assert items[0]["first_seen"] == NOW


def test_a_panel_item_is_never_marked_notified():
    verdicts = [{"tier": "panel", "trigger": "p1_channel", "why": "FYI", "draft_reply": None}]
    items = build_items(CANDIDATES, verdicts, {"day": "2026-09-09", "seen": {}}, NOW)
    assert items[0]["notified"] is False


def test_an_already_seen_item_keeps_its_original_first_seen():
    from lib.pulse_state import fingerprint
    fid = fingerprint(CANDIDATES[0])
    state = {"day": "2026-09-09", "seen": {fid: {"first_seen": "earlier", "notified": True}}}
    items = build_items(CANDIDATES, VERDICTS, state, NOW)
    assert items[0]["first_seen"] == "earlier"


def test_payload_reports_sources_and_window():
    payload = build_payload([], {"webex": "ok", "email": "degraded: x"},
                            "2026-09-09T17:15:00+00:00", NOW, NOW, notified=False)
    assert payload["sources"]["email"] == "degraded: x"
    assert payload["window"]["from"] == "2026-09-09T17:15:00+00:00"
    assert payload["status"] == "ok"
    assert payload["notified_this_run"] is False


def test_payload_uses_the_local_day_when_one_is_given():
    # 01:15 UTC on the 10th is still the evening of the 9th in Ben's timezone.
    payload = build_payload([], {}, NOW, NOW, "2026-09-10T01:15:00+00:00",
                            notified=False, day="2026-09-09")
    assert payload["day"] == "2026-09-09"


def test_payload_falls_back_to_the_utc_day_when_none_is_given():
    payload = build_payload([], {}, NOW, NOW, NOW, notified=False)
    assert payload["day"] == "2026-09-09"


def test_failure_payload_says_it_failed_rather_than_looking_empty():
    payload = failure_payload("network unreachable", NOW)
    assert payload["status"] == "failed"
    assert "network unreachable" in payload["reason"]
    assert payload["items"] == []


def test_write_then_read_round_trips(tmp_path):
    payload = build_payload([], {"webex": "ok"}, NOW, NOW, NOW, notified=False)
    path = write_payload(payload, str(tmp_path))
    assert os.path.basename(path) == "pulse.json"
    assert json.load(open(path))["status"] == "ok"


def test_archive_moves_yesterdays_artifact_aside(tmp_path):
    out = str(tmp_path)
    write_payload(
        build_payload([], {}, NOW, NOW, NOW, notified=False, day="2026-09-08"), out
    )
    archived = archive_if_new_day(out, "2026-09-09")
    assert archived and archived.endswith("pulse-2026-09-08.json")
    assert not os.path.exists(os.path.join(out, "pulse.json"))


def test_archive_is_a_noop_within_the_same_day(tmp_path):
    out = str(tmp_path)
    json.dump({"day": "2026-09-09"}, open(os.path.join(out, "pulse.json"), "w"))
    assert archive_if_new_day(out, "2026-09-09") is None
    assert os.path.exists(os.path.join(out, "pulse.json"))


def test_archive_is_a_noop_when_there_is_nothing_to_archive(tmp_path):
    assert archive_if_new_day(str(tmp_path), "2026-09-09") is None
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd ~/projects/webex-agent && .venv/bin/python3.12 -m pytest tests/test_pulse_output.py -v`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement**

Create `scripts/lib/pulse_output.py`:

```python
"""The pulse.json contract.

Two invariants:

1. Items carry the verbatim message text, because the Hub panel shows the
   actual messages rather than a model's summary of them.
2. A failed run still writes an artifact. An empty items list and a failed run
   look identical otherwise, and the failed run would then read as a quiet hour.
"""
from __future__ import annotations

import json
import os

from lib.pulse_state import fingerprint

ARTIFACT = "pulse.json"


def build_items(candidates, verdicts, state, now_iso) -> list[dict]:
    seen = state.get("seen", {})
    items = []
    for candidate, verdict in zip(candidates, verdicts):
        fid = fingerprint(candidate)
        prior = seen.get(fid) or {}
        tier = verdict.get("tier", "panel")
        items.append({
            "id": fid,
            "tier": tier,
            "source": candidate.get("source"),
            "trigger": verdict.get("trigger"),
            "channel": candidate.get("channel"),
            "from": {"name": candidate.get("from_name"), "email": candidate.get("from_email")},
            "at": candidate.get("at"),
            "text": candidate.get("text"),
            "why": verdict.get("why", ""),
            "draft_reply": verdict.get("draft_reply"),
            "link": candidate.get("link"),
            "first_seen": prior.get("first_seen", now_iso),
            "notified": bool(prior.get("notified")) or tier == "priority",
            "resolved": bool(prior.get("resolved")),
        })
    return items


def build_payload(items, sources, window_from, window_to, now_iso, notified,
                  day=None, status="ok") -> dict:
    return {
        "generated_at": now_iso,
        # Ben's local day, passed in. now_iso[:10] is UTC and disagrees with the
        # locally-derived `today` that archive_if_new_day compares against.
        "day": day or now_iso[:10],
        "status": status,
        "window": {"from": window_from, "to": window_to},
        "sources": sources,
        "notified_this_run": bool(notified),
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
        "items": [],
    }


def write_payload(payload: dict, output_dir: str) -> str:
    os.makedirs(output_dir, exist_ok=True)
    path = os.path.join(output_dir, ARTIFACT)
    tmp = f"{path}.tmp"
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
```

- [ ] **Step 4: Run to verify they pass**

Run: `cd ~/projects/webex-agent && .venv/bin/python3.12 -m pytest tests/test_pulse_output.py -v`
Expected: PASS, 13 tests.

- [ ] **Step 5: Commit**

```bash
cd ~/projects/webex-agent
git add scripts/lib/pulse_output.py tests/test_pulse_output.py
git commit -m "feat: pulse.json contract with explicit failure artifacts"
```

---

### Task 10: The notification

Fires from Python via `osascript`, not from the Hub. launchd runs whether or not the Tauri app is open; if the notification came from the Hub's web `Notification` API, closing the window would silently disable the entire system.

**Files:**
- Create: `scripts/lib/pulse_notify.py`
- Test: `tests/test_pulse_notify.py`

**Interfaces:**
- Consumes: nothing
- Produces:
  - `build_body(priority_items: list[dict]) -> str`
  - `notify(priority_items, runner=...) -> bool`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_pulse_notify.py`:

```python
from lib.pulse_notify import build_body, notify


def _item(name, channel):
    return {"from": {"name": name}, "channel": channel}


def test_body_names_a_single_sender_and_channel():
    assert build_body([_item("Rory Scott", "C3 + CUI")]) == "1 needs you — Rory Scott (C3 + CUI)"


def test_body_pluralises_and_lists_up_to_two_then_summarises():
    body = build_body([
        _item("Rory Scott", "C3 + CUI"),
        _item("Shyam Srinivasan", "Direct message"),
        _item("Einar Nilsen-Nygaard", "PureCA Identity Fabric"),
    ])
    assert body == "3 need you — Rory Scott (C3 + CUI), Shyam Srinivasan (Direct message), +1"


def test_body_with_exactly_two_has_no_overflow_marker():
    body = build_body([_item("A", "X"), _item("B", "Y")])
    assert body == "2 need you — A (X), B (Y)"


def test_notify_does_nothing_and_reports_false_when_there_is_nothing_new():
    calls = []
    assert notify([], runner=lambda cmd: calls.append(cmd)) is False
    assert calls == []


def test_notify_fires_exactly_once_regardless_of_item_count():
    calls = []
    items = [_item(f"P{i}", "C3 + CUI") for i in range(7)]
    assert notify(items, runner=lambda cmd: calls.append(cmd)) is True
    assert len(calls) == 1


def test_notify_requests_a_sound():
    calls = []
    notify([_item("A", "X")], runner=lambda cmd: calls.append(cmd))
    assert "sound name" in calls[0][-1]


def test_notify_escapes_double_quotes_in_names():
    calls = []
    notify([_item('Ro"ry', "C3 + CUI")], runner=lambda cmd: calls.append(cmd))
    assert '\\"' in calls[0][-1]


def test_notify_swallows_runner_failure_rather_than_killing_the_run():
    def boom(_cmd):
        raise RuntimeError("osascript missing")

    assert notify([_item("A", "X")], runner=boom) is False
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd ~/projects/webex-agent && .venv/bin/python3.12 -m pytest tests/test_pulse_notify.py -v`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement**

Create `scripts/lib/pulse_notify.py`:

```python
"""The single per-run notification.

One banner and one sound per run when new priority items exist, and nothing
otherwise. The item count does not change the loudness; neither does who sent
them or where.

Fired via osascript from Python rather than from the Hub, because launchd runs
regardless of whether the Tauri app is open. A Hub-side notification would go
silent the moment Ben closed the window.
"""
from __future__ import annotations

import subprocess

TITLE = "Hourly Pulse"
SOUND = "Submarine"
NAMED_IN_BODY = 2
TIMEOUT_S = 10


def _escape(value: str) -> str:
    return (value or "").replace("\\", "\\\\").replace('"', '\\"')


def build_body(priority_items: list[dict]) -> str:
    count = len(priority_items)
    verb = "needs" if count == 1 else "need"
    named = [
        f"{(item.get('from') or {}).get('name', 'someone')} ({item.get('channel', '?')})"
        for item in priority_items[:NAMED_IN_BODY]
    ]
    body = f"{count} {verb} you — {', '.join(named)}"
    overflow = count - len(named)
    if overflow > 0:
        body += f", +{overflow}"
    return body


def _run(cmd: list[str]) -> None:
    subprocess.run(cmd, check=True, timeout=TIMEOUT_S, capture_output=True)


def notify(priority_items: list[dict], runner=_run) -> bool:
    """Fire one notification. Returns whether it fired."""
    if not priority_items:
        return False

    script = (
        f'display notification "{_escape(build_body(priority_items))}" '
        f'with title "{TITLE}" sound name "{SOUND}"'
    )
    try:
        runner(["osascript", "-e", script])
    except Exception:  # noqa: BLE001 — a failed banner must not fail the run
        return False
    return True
```

- [ ] **Step 4: Run to verify they pass**

Run: `cd ~/projects/webex-agent && .venv/bin/python3.12 -m pytest tests/test_pulse_notify.py -v`
Expected: PASS, 8 tests.

- [ ] **Step 5: Verify a real notification appears**

```bash
cd ~/projects/webex-agent && .venv/bin/python3.12 -c "
import sys; sys.path.insert(0, 'scripts')
from lib.pulse_notify import notify
print('fired:', notify([{'from': {'name': 'Test Sender'}, 'channel': 'C3 + CUI'}]))
"
```

Expected: a banner reading `1 needs you — Test Sender (C3 + CUI)` with a sound. If nothing appears, check System Settings → Notifications for the script host; the return value can be `True` while macOS suppresses display.

- [ ] **Step 6: Commit**

```bash
cd ~/projects/webex-agent
git add scripts/lib/pulse_notify.py tests/test_pulse_notify.py
git commit -m "feat: single per-run osascript notification"
```

---

### Task 11: Orchestration

**Files:**
- Create: `scripts/hourly_pulse.py`
- Test: `tests/test_hourly_pulse.py`

**Interfaces:**
- Consumes: every `lib.pulse_*` module
- Produces: `main() -> int`, and `run(deps) -> dict` for testability

- [ ] **Step 1: Write the failing tests**

Create `tests/test_hourly_pulse.py`:

```python
import json
import os
from datetime import datetime, timezone

from hourly_pulse import run

UTC = timezone.utc
NOW = datetime(2026, 9, 9, 18, 15, 3, tzinfo=UTC)

PREFS_TEXT = """
## Watchlist
- Rory Scott <rorscott@cisco.com>

### Priority 1 — Interrupt Me
- C3 + CUI

### Priority 2 — Tagged Only
- SCC - CII Discussion
"""

CAND = {
    "source": "webex", "channel": "C3 + CUI", "space_id": "abc",
    "from_name": "Rory Scott", "from_email": "rorscott@cisco.com",
    "at": "2026-09-09T18:02:11+00:00", "text": "Confirm before the pre-read?",
    "link": "https://web.webex.com/spaces/abc",
    "is_watchlist": True, "is_direct_mention": False, "is_watched_thread": False,
    "tier_hint": "p1",
}


def _deps(tmp_path, **over):
    base = {
        "now": NOW,
        "tz_offset_hours": -4,
        "prefs_text": PREFS_TEXT,
        "output_dir": str(tmp_path / "output"),
        "state_path": str(tmp_path / ".pulse_seen.json"),
        "last_run_path": str(tmp_path / ".last_pulse_run"),
        "my_email": "benmyers@cisco.com",
        "my_names": ["Ben Myers"],
        "collect_webex": lambda **kw: [dict(CAND)],
        "collect_email": lambda **kw: ([], "ok"),
        "collect_calendar": lambda **kw: ([], "ok"),
        "classify": lambda **kw: [
            {"tier": "priority", "trigger": "watchlist", "why": "Ask.", "draft_reply": "On it."}
        ],
        "notify": lambda items: bool(items),
    }
    base.update(over)
    return base


def test_a_priority_item_produces_an_artifact_and_notifies(tmp_path):
    payload = run(_deps(tmp_path))
    assert payload["status"] == "ok"
    assert payload["notified_this_run"] is True
    assert payload["items"][0]["tier"] == "priority"
    assert payload["items"][0]["text"] == "Confirm before the pre-read?"


def test_the_same_item_on_a_second_run_does_not_notify_again(tmp_path):
    deps = _deps(tmp_path)
    run(deps)
    second = run(deps)
    assert second["notified_this_run"] is False
    assert second["items"][0]["tier"] == "priority"  # still shown, just silent


def test_no_candidates_means_no_notification(tmp_path):
    payload = run(_deps(tmp_path, collect_webex=lambda **kw: []))
    assert payload["notified_this_run"] is False
    assert payload["items"] == []


def test_only_panel_items_means_no_notification(tmp_path):
    deps = _deps(tmp_path, classify=lambda **kw: [
        {"tier": "panel", "trigger": "p1_channel", "why": "FYI", "draft_reply": None}
    ])
    payload = run(deps)
    assert payload["notified_this_run"] is False
    assert payload["items"][0]["tier"] == "panel"


def test_a_degraded_email_source_is_reported_and_webex_still_runs(tmp_path):
    deps = _deps(tmp_path, collect_email=lambda **kw: ([], "degraded: token expired"))
    payload = run(deps)
    assert payload["status"] == "ok"
    assert payload["sources"]["email"] == "degraded: token expired"
    assert len(payload["items"]) == 1


def test_a_webex_failure_produces_a_failed_artifact_not_an_empty_one(tmp_path):
    def boom(**kw):
        raise RuntimeError("webex unreachable")

    payload = run(_deps(tmp_path, collect_webex=boom))
    assert payload["status"] == "failed"
    assert "webex unreachable" in payload["reason"]


def test_the_artifact_lands_on_disk(tmp_path):
    run(_deps(tmp_path))
    path = tmp_path / "output" / "pulse.json"
    assert json.load(open(path))["items"][0]["from"]["name"] == "Rory Scott"


def test_last_pulse_run_is_written_and_last_run_is_never_touched(tmp_path):
    deps = _deps(tmp_path)
    shared = tmp_path / ".last_run"
    shared.write_text("2026-09-08T20:00:00+00:00")
    before = shared.read_text()
    run(deps)
    assert os.path.exists(deps["last_run_path"])
    assert shared.read_text() == before


def test_no_pulse_module_references_the_shared_state_files():
    """The real guard. A unit test can only prove this run didn't write them;
    this proves no code path can. `.last_run` and `.watched_threads.json` belong
    to daily_summary.py, and a pulse write to either corrupts the daily briefing.
    """
    from pathlib import Path

    scripts = Path(__file__).resolve().parent.parent / "scripts"
    sources = [scripts / "hourly_pulse.py", *sorted((scripts / "lib").glob("pulse_*.py"))]
    assert sources, "no pulse sources found — check the path"

    for path in sources:
        text = path.read_text().replace(".last_pulse_run", "")
        assert ".last_run" not in text, f"{path.name} references .last_run"
        assert "save_watched_threads" not in text, f"{path.name} writes watched threads"
        assert "prune_watched_threads" not in text, f"{path.name} prunes watched threads"


def test_a_new_day_archives_yesterdays_artifact(tmp_path):
    run(_deps(tmp_path))
    tomorrow = datetime(2026, 9, 10, 18, 15, tzinfo=UTC)
    run(_deps(tmp_path, now=tomorrow))
    assert (tmp_path / "output" / "pulse-2026-09-09.json").exists()


def test_a_new_day_clears_the_seen_store_and_can_notify_again(tmp_path):
    deps = _deps(tmp_path)
    run(deps)
    tomorrow = datetime(2026, 9, 10, 18, 15, tzinfo=UTC)
    payload = run(_deps(tmp_path, now=tomorrow))
    assert payload["notified_this_run"] is True
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd ~/projects/webex-agent && .venv/bin/python3.12 -m pytest tests/test_hourly_pulse.py -v`
Expected: FAIL — `No module named 'hourly_pulse'`.

- [ ] **Step 3: Implement**

Create `scripts/hourly_pulse.py`:

```python
#!/usr/bin/env python3
"""Hourly pulse — the urgency engine.

Orchestration only. Every decision lives in a lib module so it can be tested
without network, model, or clock. See
docs/superpowers/specs/2026-09-09-hourly-pulse-design.md.

NEVER writes .last_run. That file belongs to daily_summary.py, and advancing it
here would silently cut the 08:30 briefing's lookback to one hour.
"""
from __future__ import annotations

import os
import sys
import time
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


def run(deps: dict) -> dict:
    now = deps["now"]
    now_iso = now.isoformat()
    local_now = now + timedelta(hours=deps["tz_offset_hours"])
    today = local_now.strftime("%Y-%m-%d")

    pulse_output.archive_if_new_day(deps["output_dir"], today)
    prefs = parse_prefs(deps["prefs_text"])
    state = pulse_state.load_state(deps["state_path"], today)
    window_from = pulse_state.pulse_window(
        _read(deps["last_run_path"]), now, deps["tz_offset_hours"]
    )
    briefing_label, briefing_horizon = pulse_state.next_briefing_at(local_now)

    sources: dict[str, str] = {}
    try:
        candidates = deps["collect_webex"](
            prefs=prefs, since=window_from, my_email=deps["my_email"],
            my_names=deps["my_names"],
        )
        sources["webex"] = "ok"

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
    except Exception as exc:  # noqa: BLE001
        payload = pulse_output.failure_payload(str(exc), now_iso, day=today)
        pulse_output.write_payload(payload, deps["output_dir"])
        _write(deps["last_run_path"], now_iso)
        return payload

    items = pulse_output.build_items(candidates, verdicts, state, now_iso)

    # Only items not already in the seen store can fire a notification. An
    # unanswered ask from 09:15 must not re-fire every hour.
    fresh_priority = [
        item for item in items
        if item["tier"] == "priority" and item["id"] not in state["seen"]
    ]
    notified = deps["notify"](fresh_priority)

    for item in items:
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


def _local_offset_hours() -> float:
    return -time.timezone / 3600 if not time.daylight else -time.altzone / 3600


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
```

- [ ] **Step 4: Run to verify they pass**

Run: `cd ~/projects/webex-agent && .venv/bin/python3.12 -m pytest tests/test_hourly_pulse.py -v`
Expected: PASS, 11 tests.

- [ ] **Step 5: Run the whole suite**

Run: `cd ~/projects/webex-agent && .venv/bin/python3.12 -m pytest tests/ -v`
Expected: PASS, 105 tests (1 + 7 + 14 + 16 + 17 + 6 + 12 + 13 + 8 + 11).

- [ ] **Step 6: Commit**

```bash
cd ~/projects/webex-agent
chmod +x scripts/hourly_pulse.py
git add scripts/hourly_pulse.py tests/test_hourly_pulse.py
git commit -m "feat: hourly pulse orchestration with .last_run isolation"
```

---

### Task 12: The wrapper and the launchd job

**Files:**
- Create: `scripts/run_pulse.sh`
- Create: `~/Library/LaunchAgents/com.webex-agent.hourly-pulse.plist`

**Interfaces:**
- Consumes: `scripts/hourly_pulse.py`, `scripts/lib/wait_for_network.sh`, `scripts/aws_refresh.sh`
- Produces: a launchd-invocable entry point

- [ ] **Step 1: Confirm the reusable pieces exist with the names used below**

```bash
cd ~/projects/webex-agent && grep -n "^wait_for_network\|^network_failure_message\|^scrub_inherited_credentials\|^hold_system_awake" scripts/lib/wait_for_network.sh
```

Expected: all four functions. If a name differs, use the real one — do not edit `wait_for_network.sh`.

- [ ] **Step 2: Write the wrapper**

Create `scripts/run_pulse.sh`:

```bash
#!/bin/bash
# Wrapper for the hourly pulse LaunchAgent and manual runs.
#
# Mirrors run_summary.sh deliberately: prove the network before blaming a
# credential, refresh AWS through the timeout-bounded helper rather than calling
# duo-sso inline, and always leave an artifact behind. A scheduled run that
# writes nothing is indistinguishable from a quiet hour.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
OUTPUT_DIR="$PROJECT_DIR/output"
PYTHON="$PROJECT_DIR/.venv/bin/python3.12"

log() { printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >&2; }

export PATH="/opt/homebrew/bin:/opt/homebrew/opt/python@3.12/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

# The pulse's own failure artifact, written in the same shape hourly_pulse.py
# uses so the Hub needs no special case.
write_failure() {
  mkdir -p "$OUTPUT_DIR"
  "$PYTHON" - "$1" <<'PY' > "$OUTPUT_DIR/pulse.json"
import json, sys
from datetime import datetime, timezone
now = datetime.now(timezone.utc).isoformat()
json.dump({
    "generated_at": now, "day": now[:10], "status": "failed",
    "reason": sys.argv[1], "window": None, "sources": {},
    "notified_this_run": False, "items": [],
}, sys.stdout, indent=2)
PY
  log "wrote failure artifact to $OUTPUT_DIR/pulse.json"
}

if [ ! -f "$PROJECT_DIR/.env" ]; then
  log "FATAL: $PROJECT_DIR/.env not found."
  write_failure "$PROJECT_DIR/.env is missing, so the run aborted before fetching anything."
  exit 78 # EX_CONFIG
fi
set -a
# shellcheck disable=SC1091
source "$PROJECT_DIR/.env"
set +a

# shellcheck source=lib/wait_for_network.sh
. "$SCRIPT_DIR/lib/wait_for_network.sh"
scrub_inherited_credentials
hold_system_awake

NETWORK_PROBE_HOSTS="${NETWORK_PROBE_HOSTS:-webexapis.com bedrock-runtime.us-east-1.amazonaws.com}"
# shellcheck disable=SC2086
if ! wait_for_network $NETWORK_PROBE_HOSTS; then
  write_failure "$(network_failure_message)"
  exit 75 # EX_TEMPFAIL
fi

AWS_REFRESH_LOG=/dev/stderr AWS_REFRESH_THRESHOLD=7200 \
  bash "$SCRIPT_DIR/aws_refresh.sh" >> /tmp/webex-pulse.log 2>&1 || true

cd "$PROJECT_DIR"
log "starting hourly pulse"

"$PYTHON" scripts/hourly_pulse.py >> /tmp/webex-pulse.log 2>&1
CODE=$?
log "hourly_pulse.py exited $CODE"

if [ "$CODE" -eq 0 ]; then
  exit 0
fi

# hourly_pulse.py writes its own failure artifact on a handled error. Only
# overwrite if it died before getting that far.
if ! grep -q '"status"' "$OUTPUT_DIR/pulse.json" 2>/dev/null; then
  write_failure "hourly_pulse.py exited $CODE before writing an artifact. The network
passed its reachability check first, so this is not connectivity — the usual cause is
expired AWS credentials. Run: duo-sso -profile claudecode"
fi

exit $CODE
```

- [ ] **Step 3: Make it executable and dry-run it**

```bash
cd ~/projects/webex-agent && chmod +x scripts/run_pulse.sh && bash -n scripts/run_pulse.sh && echo "syntax OK"
bash scripts/run_pulse.sh; echo "exit=$?"
cat output/pulse.json | head -20
```

Expected: `syntax OK`, then an artifact with `"status": "ok"` (or `"failed"` with a readable reason). Either is a pass — a silent absence of `pulse.json` is the only failure.

- [ ] **Step 4: Exercise the failure path**

```bash
cd ~/projects/webex-agent && NETWORK_PROBE_HOSTS="this-host-does-not-exist.invalid" bash scripts/run_pulse.sh; echo "exit=$?"
.venv/bin/python3.12 -c "import json; d=json.load(open('output/pulse.json')); print(d['status']); print(d['reason'][:120])"
```

Expected: exit 75, `failed`, and a network-flavoured reason. This proves silence never reads as a quiet hour.

- [ ] **Step 5: Create the launchd plist**

Create `~/Library/LaunchAgents/com.webex-agent.hourly-pulse.plist`. Nine `StartCalendarInterval` entries per weekday, hours 9 through 17 at minute 15. **Never `StartInterval`** — it never overlaps, so one hung run kills the job silently and permanently.

```bash
cd ~/projects/webex-agent && .venv/bin/python3.12 - <<'PY' > ~/Library/LaunchAgents/com.webex-agent.hourly-pulse.plist
import plistlib, sys
intervals = [
    {"Hour": h, "Minute": 15, "Weekday": d}
    for d in range(1, 6)
    for h in range(9, 18)
]
plist = {
    "Label": "com.webex-agent.hourly-pulse",
    # Lowercase `projects`, matching the newer plists (aws-refresh, github-triage).
    # The older triage plists say `Projects` and work only because macOS's FS is
    # case-insensitive; don't add another one.
    "ProgramArguments": ["/Users/benmyers/projects/webex-agent/scripts/run_pulse.sh"],
    "StandardOutPath": "/tmp/webex-pulse.log",
    "StandardErrorPath": "/tmp/webex-pulse.log",
    "StartCalendarInterval": intervals,
    "TimeOut": 900,
}
sys.stdout.buffer.write(plistlib.dumps(plist))
PY
plutil -lint ~/Library/LaunchAgents/com.webex-agent.hourly-pulse.plist
```

Expected: `OK`. Verify the count is 45 entries (9 hours × 5 weekdays):

```bash
.venv/bin/python3.12 -c "
import plistlib
p = plistlib.load(open('/Users/benmyers/Library/LaunchAgents/com.webex-agent.hourly-pulse.plist','rb'))
print(len(p['StartCalendarInterval']), 'intervals')
assert 'StartInterval' not in p, 'StartInterval is forbidden'
print('OK')
"
```

- [ ] **Step 6: Load it and confirm**

```bash
launchctl unload ~/Library/LaunchAgents/com.webex-agent.hourly-pulse.plist 2>/dev/null
launchctl load ~/Library/LaunchAgents/com.webex-agent.hourly-pulse.plist
launchctl list | grep hourly-pulse
```

Expected: a line with the label. A PID column of `-` is correct between runs.

- [ ] **Step 7: Commit**

```bash
cd ~/projects/webex-agent
cp ~/Library/LaunchAgents/com.webex-agent.hourly-pulse.plist docs/launchagents/
git add scripts/run_pulse.sh docs/launchagents/com.webex-agent.hourly-pulse.plist
git commit -m "feat: run_pulse.sh wrapper and hourly launchd job"
```

(Create `docs/launchagents/` first if absent — the plist lives outside the repo, so a copy is the only version history it gets.)

---

### Task 13: Hub API for the pulse

**Files:**
- Create: `~/projects/claude-hub/server/pulse.js`
- Create: `~/projects/claude-hub/server/pulse.test.js`
- Create: `~/projects/claude-hub/server/routes/pulse.js`
- Modify: `~/projects/claude-hub/server/index.js`

**Interfaces:**
- Consumes: `output/pulse.json` from Task 9
- Produces:
  - `STALE_AFTER_MS: number`
  - `isStale(generatedAt, now) -> boolean`
  - `shapeResponse(payload, now) -> object` with `{ status, stale, generatedAt, sources, priority[], silent[], counts }`
  - `readPulse(dir, now) -> object`

- [ ] **Step 1: Write the failing tests**

Create `server/pulse.test.js`:

```javascript
import { describe, it, expect } from 'vitest';
import { isStale, shapeResponse, STALE_AFTER_MS } from './pulse.js';

const NOW = new Date('2026-09-09T18:15:00Z');

const payload = {
  generated_at: '2026-09-09T18:15:00+00:00',
  status: 'ok',
  sources: { webex: 'ok', email: 'degraded: token expired' },
  notified_this_run: true,
  items: [
    { id: 'a', tier: 'priority', channel: 'C3 + CUI', from: { name: 'Rory Scott' },
      at: '2026-09-09T18:02:11+00:00', text: 'Confirm?', why: 'ask', trigger: 'watchlist' },
    { id: 'b', tier: 'panel', channel: 'SCC - CII Discussion', from: { name: 'Someone' },
      at: '2026-09-09T18:05:00+00:00', text: 'FYI', why: 'fyi', trigger: 'p2_channel' },
    { id: 'c', tier: 'priority', channel: 'Direct message', from: { name: 'Shyam' },
      at: '2026-09-09T18:10:00+00:00', text: 'quick q', why: 'dm', trigger: 'dm' },
  ],
};

describe('isStale', () => {
  it('is fresh when the artifact is minutes old', () => {
    expect(isStale('2026-09-09T18:10:00+00:00', NOW)).toBe(false);
  });

  it('is stale past the threshold', () => {
    expect(isStale('2026-09-09T16:00:00+00:00', NOW)).toBe(true);
  });

  it('treats a missing timestamp as stale rather than fresh', () => {
    expect(isStale(null, NOW)).toBe(true);
  });

  it('treats an unparseable timestamp as stale', () => {
    expect(isStale('not a date', NOW)).toBe(true);
  });

  it('uses a 90 minute threshold', () => {
    expect(STALE_AFTER_MS).toBe(90 * 60 * 1000);
  });
});

describe('shapeResponse', () => {
  it('splits priority from silent without merging anything', () => {
    const out = shapeResponse(payload, NOW);
    expect(out.priority.map(i => i.id)).toEqual(['c', 'a']);
    expect(out.silent.map(i => i.id)).toEqual(['b']);
  });

  it('sorts newest first within each section', () => {
    const out = shapeResponse(payload, NOW);
    expect(out.priority[0].from.name).toBe('Shyam');
  });

  it('counts each section', () => {
    const out = shapeResponse(payload, NOW);
    expect(out.counts).toEqual({ priority: 2, silent: 1 });
  });

  it('surfaces degraded sources so the panel can warn', () => {
    const out = shapeResponse(payload, NOW);
    expect(out.degraded).toEqual([{ source: 'email', detail: 'degraded: token expired' }]);
  });

  it('reports no degraded sources when all are ok', () => {
    const out = shapeResponse({ ...payload, sources: { webex: 'ok' } }, NOW);
    expect(out.degraded).toEqual([]);
  });

  it('passes a failed run through as failed with its reason', () => {
    const out = shapeResponse(
      { generated_at: '2026-09-09T18:15:00+00:00', status: 'failed',
        reason: 'network unreachable', items: [], sources: {} },
      NOW,
    );
    expect(out.status).toBe('failed');
    expect(out.reason).toBe('network unreachable');
  });

  it('reports missing as its own status, not as a quiet hour', () => {
    const out = shapeResponse(null, NOW);
    expect(out.status).toBe('missing');
    expect(out.counts).toEqual({ priority: 0, silent: 0 });
  });
});
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd ~/projects/claude-hub && npm test -- server/pulse.test.js`
Expected: FAIL — cannot resolve `./pulse.js`.

- [ ] **Step 3: Implement the pure module**

Create `server/pulse.js`:

```javascript
import fs from 'fs';
import path from 'path';

// A hung launchd job still shows a PID in `launchctl list`. The only honest
// freshness signal is the timestamp on the artifact itself.
export const STALE_AFTER_MS = 90 * 60 * 1000;

export function isStale(generatedAt, now = new Date()) {
  if (!generatedAt) return true;
  const then = new Date(generatedAt);
  if (Number.isNaN(then.getTime())) return true;
  return now.getTime() - then.getTime() > STALE_AFTER_MS;
}

const newestFirst = (a, b) => String(b.at || '').localeCompare(String(a.at || ''));

export function shapeResponse(payload, now = new Date()) {
  if (!payload) {
    return {
      status: 'missing', stale: true, generatedAt: null, sources: {},
      degraded: [], priority: [], silent: [], counts: { priority: 0, silent: 0 },
      notifiedThisRun: false,
    };
  }

  const items = Array.isArray(payload.items) ? payload.items : [];
  const priority = items.filter((i) => i.tier === 'priority').sort(newestFirst);
  const silent = items.filter((i) => i.tier !== 'priority').sort(newestFirst);

  const sources = payload.sources || {};
  const degraded = Object.entries(sources)
    .filter(([, v]) => typeof v === 'string' && v !== 'ok')
    .map(([source, detail]) => ({ source, detail }));

  return {
    status: payload.status || 'ok',
    reason: payload.reason,
    stale: isStale(payload.generated_at, now),
    generatedAt: payload.generated_at || null,
    sources,
    degraded,
    priority,
    silent,
    counts: { priority: priority.length, silent: silent.length },
    notifiedThisRun: Boolean(payload.notified_this_run),
  };
}

export function readPulse(dir, now = new Date()) {
  const file = path.join(dir, 'pulse.json');
  let payload = null;
  try {
    payload = JSON.parse(fs.readFileSync(file, 'utf-8'));
  } catch {
    // Absent or unreadable is reported as `missing`, never as an empty ok run.
    payload = null;
  }
  return shapeResponse(payload, now);
}
```

- [ ] **Step 4: Run to verify they pass**

Run: `cd ~/projects/claude-hub && npm test -- server/pulse.test.js`
Expected: PASS, 12 tests (5 `isStale`, 7 `shapeResponse`).

- [ ] **Step 5: Add the route**

Create `server/routes/pulse.js`:

```javascript
import { Router } from 'express';
import path from 'path';
import { readPulse } from '../pulse.js';

const router = Router();
const OUTPUT_DIR = path.join(process.env.HOME, 'projects/webex-agent/output');

router.get('/', (req, res) => {
  try {
    res.json(readPulse(OUTPUT_DIR));
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

// Marks items read so the dock badge can zero. NOT a reply, NOT a dismissal —
// items self-clear and the whole panel clears daily.
const readIds = new Set();

router.post('/ack', (req, res) => {
  const ids = Array.isArray(req.body?.ids) ? req.body.ids : [];
  ids.forEach((id) => readIds.add(id));
  res.json({ ok: true, read: readIds.size });
});

export default router;
```

- [ ] **Step 6: Mount it**

In `server/index.js`, add the import beside the other route imports and the mount beside `app.use('/api/webex', webexRouter);`:

```javascript
import pulseRouter from './routes/pulse.js';
```
```javascript
app.use('/api/pulse', pulseRouter);
```

- [ ] **Step 7: Verify the endpoint responds**

```bash
cd ~/projects/claude-hub && (node server/index.js &) && sleep 3 && \
  curl -s http://localhost:3200/api/pulse | head -c 400; echo
```

Expected: JSON with `status`, `counts`, `priority`, `silent`. Kill the server afterwards with `pkill -f "node server/index.js"`.

- [ ] **Step 8: Commit**

```bash
cd ~/projects/claude-hub
git add server/pulse.js server/pulse.test.js server/routes/pulse.js server/index.js
git commit -m "feat: pulse API with staleness detection and degraded-source reporting"
```

---

### Task 14: The Hub panel

**Files:**
- Create: `~/projects/claude-hub/src/components/PulseView.vue`
- Modify: `~/projects/claude-hub/src/stores/notifications.js`
- Modify: `~/projects/claude-hub/src/components/Sidebar.vue`
- Modify: `~/projects/claude-hub/src/App.vue`

**Interfaces:**
- Consumes: `GET /api/pulse`, `POST /api/pulse/ack` (Task 13)
- Produces: `setPulseBadge(count)` exported from `stores/notifications.js`

- [ ] **Step 1: Extend the notifications store**

The existing store badges on unread conversation completions. Pulse count is a second contributor, so the badge has to combine them rather than overwrite.

In `src/stores/notifications.js`, replace `updateDockBadge` and add the pulse setter:

```javascript
// Pulse priority count is a second badge contributor alongside unread
// conversation completions. It must add to the badge, not overwrite it.
let pulseCount = 0;

export function setPulseBadge(count) {
  pulseCount = Number.isFinite(count) ? Math.max(0, count) : 0;
  updateDockBadge();
}

function updateDockBadge() {
  const total = unreadCompletions.size + pulseCount;
  if (navigator.setAppBadge) {
    if (total > 0) {
      navigator.setAppBadge(total);
    } else {
      navigator.clearAppBadge();
    }
  }
}
```

Note the behaviour change: the existing code called `setAppBadge()` with no argument, which shows a dot. Passing `total` shows a count, which the spec requires.

- [ ] **Step 2: Create the panel**

Create `src/components/PulseView.vue`:

```vue
<template>
  <div class="pulse">
    <header class="head">
      <h2>Hourly Pulse</h2>
      <span v-if="data.generatedAt" class="ts">
        updated {{ shortTime(data.generatedAt) }}
      </span>
      <button class="refresh" @click="load">Refresh</button>
    </header>

    <div v-if="data.status === 'missing'" class="warn">
      No pulse artifact yet. The job may not have run — this is not a quiet hour.
    </div>
    <div v-else-if="data.status === 'failed'" class="warn">
      <strong>The last run failed.</strong> {{ data.reason }}
      Nothing was fetched, so do not read this as a quiet hour.
    </div>
    <div v-else-if="data.stale" class="warn">
      Last run was {{ shortTime(data.generatedAt) }} — more than 90 minutes ago.
      The hourly job may be hung.
    </div>

    <div v-for="d in data.degraded" :key="d.source" class="degraded">
      <strong>{{ d.source }}</strong> is degraded: {{ d.detail }}.
      Items from that source are missing.
    </div>

    <section>
      <h3>⚡ Triggered notifications <span class="count">{{ data.counts.priority }}</span></h3>
      <p v-if="!data.priority.length" class="empty">Nothing has needed you today.</p>
      <article v-for="item in data.priority" :key="item.id" class="item priority">
        <div class="meta">
          <strong>{{ item.from?.name }}</strong>
          <span class="chan">{{ item.channel }}</span>
          <span class="when">{{ shortTime(item.at) }}</span>
          <span class="trigger">{{ item.trigger }}</span>
        </div>
        <p class="text">{{ item.text }}</p>
        <p class="why">{{ item.why }}</p>
        <blockquote v-if="item.draft_reply" class="draft">{{ item.draft_reply }}</blockquote>
        <a v-if="item.link" :href="item.link" target="_blank" rel="noopener">Open</a>
      </article>
    </section>

    <section>
      <h3>◻ Silent — came in, didn't interrupt you
        <span class="count">{{ data.counts.silent }}</span></h3>
      <article v-for="item in data.silent" :key="item.id" class="item">
        <div class="meta">
          <strong>{{ item.from?.name }}</strong>
          <span class="chan">{{ item.channel }}</span>
          <span class="when">{{ shortTime(item.at) }}</span>
        </div>
        <p class="text">{{ item.text }}</p>
        <a v-if="item.link" :href="item.link" target="_blank" rel="noopener">Open</a>
      </article>
    </section>
  </div>
</template>

<script setup>
import { ref, onMounted, onUnmounted } from 'vue';
import { setPulseBadge } from '../stores/notifications.js';

const EMPTY = {
  status: 'missing', stale: true, generatedAt: null, degraded: [],
  priority: [], silent: [], counts: { priority: 0, silent: 0 },
};

const data = ref({ ...EMPTY });
let timer = null;

function shortTime(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  return Number.isNaN(d.getTime())
    ? '' : d.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
}

async function load() {
  try {
    const res = await fetch('/api/pulse');
    data.value = { ...EMPTY, ...(await res.json()) };
    setPulseBadge(data.value.counts.priority);
  } catch {
    data.value = { ...EMPTY };
    setPulseBadge(0);
  }
}

async function ack() {
  const ids = data.value.priority.map((i) => i.id);
  if (!ids.length) return;
  await fetch('/api/pulse/ack', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ ids }),
  });
  setPulseBadge(0);
}

onMounted(async () => {
  await load();
  // Viewing the panel is the acknowledgement — that is what zeroes the badge.
  await ack();
  timer = setInterval(load, 5 * 60 * 1000);
});

onUnmounted(() => clearInterval(timer));
</script>

<style scoped>
.pulse { padding: 16px; overflow-y: auto; }
.head { display: flex; align-items: baseline; gap: 12px; }
.ts { font-size: 12px; opacity: 0.6; }
.refresh { margin-left: auto; }
.warn, .degraded {
  padding: 10px 12px; border-radius: 6px; margin: 10px 0;
  background: rgba(255, 179, 0, 0.12); border: 1px solid rgba(255, 179, 0, 0.4);
  font-size: 13px;
}
section { margin-top: 20px; }
h3 { font-size: 14px; text-transform: uppercase; letter-spacing: 0.04em; opacity: 0.75; }
.count { opacity: 0.5; font-weight: 400; }
.empty { font-size: 13px; opacity: 0.6; }
.item {
  border: 1px solid rgba(128, 128, 128, 0.25); border-radius: 8px;
  padding: 10px 12px; margin: 8px 0;
}
.item.priority { border-left: 3px solid #e5533d; }
.meta { display: flex; gap: 10px; align-items: baseline; font-size: 12px; flex-wrap: wrap; }
.chan, .when, .trigger { opacity: 0.6; }
.trigger { font-family: ui-monospace, monospace; font-size: 11px; }
.text { white-space: pre-wrap; margin: 6px 0; }
.why { font-size: 12px; opacity: 0.7; margin: 4px 0; }
.draft {
  border-left: 2px solid rgba(128, 128, 128, 0.4); margin: 6px 0;
  padding-left: 10px; font-size: 13px; opacity: 0.85;
}
</style>
```

- [ ] **Step 3: Register the view**

Two files. Views are tabs, not routes: `Sidebar.vue` opens a tab with `openView(target, title)`, and `App.vue` picks the component off `activeTab.target`. Miss the second half and the nav item opens a blank tab.

**3a.** In `src/components/Sidebar.vue`, add the nav item inside `<div class="nav-group group-menu">`, immediately before the existing `isActiveView('aha')` anchor:

```html
        <a
          class="nav-item"
          :class="{ active: isActiveView('pulse') }"
          @click="openView('pulse', 'Pulse')"
        >
          <svg class="nav-icon icon-pulse" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M22 12h-4l-3 9L9 3l-3 9H2"/></svg>
          Pulse
        </a>
```

**3b.** In the same file's `<style>` block, beside `.icon-aha { color: #fb923c; }`, add:

```css
.icon-pulse { color: #e5533d; }
```

The same red as `.item.priority` in `PulseView.vue`, so the nav icon and the priority items read as one thing.

**3c.** Also add it to the collapsed icon strip. That strip is a curated subset — it carries `home`, `daily`, `people`, `projects`, `board`, `glossary` and deliberately omits `okrs`/`alphas`/`aha`. Pulse belongs in it: it is where Ben goes when the badge fires, and the badge fires whether or not the sidebar is open. Add inside `<nav class="collapsed-nav">`, after the `glossary` anchor:

```html
        <a
          class="collapsed-nav-icon"
          :class="{ active: isActiveView('pulse') }"
          @click="openView('pulse', 'Pulse')"
          title="Hourly Pulse"
        >
          <svg class="nav-icon icon-pulse" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M22 12h-4l-3 9L9 3l-3 9H2"/></svg>
        </a>
```

**3d.** In `src/App.vue`, add the import beside the existing `import AhaView from './components/AhaView.vue';`:

```javascript
import PulseView from './components/PulseView.vue';
```

and the render branch beside the existing `AhaView` one:

```html
      <PulseView
        v-else-if="activeTab.type === 'view' && activeTab.target === 'pulse'"
        :key="activeTab.id"
      />
```

The `:key="activeTab.id"` matters — it is what forces a fresh mount per tab, so a reopened Pulse tab re-fetches rather than showing a stale artifact.

- [ ] **Step 4: Verify in the browser**

```bash
cd ~/projects/claude-hub && npm run dev
```

Open `http://localhost:5173/`, go to Pulse, and confirm: both sections render, the counts match `curl -s localhost:3200/api/pulse | grep counts`, and a degraded email source shows an amber strip. If the page is blank, check the browser console — a bare `Load failed` in the Tauri app means a dead backend, not a broken feature.

- [ ] **Step 5: Run the Hub suite**

Run: `cd ~/projects/claude-hub && npm test`
Expected: PASS — existing `okrs.test.js` plus the 12 new pulse tests.

- [ ] **Step 6: Commit**

```bash
cd ~/projects/claude-hub
git add src/components/PulseView.vue src/stores/notifications.js src/components/Sidebar.vue src/App.vue
git commit -m "feat: pulse panel showing every triggering message"
```

---

### Task 15: Fix the Hub watch form for the new tier semantics

`ADD_TO = SECTIONS[0]` sends every added channel to Priority 1. Under the new model that is a silent opt-in to being interrupted, with no way to change it in the UI.

**Files:**
- Modify: `~/projects/claude-hub/server/routes/webex.js`
- Modify: `~/projects/claude-hub/src/components/WebexWatchForm.vue`
- Modify: `~/projects/claude-hub/src/lib/api.js`
- Create: `~/projects/claude-hub/server/webex-sections.test.js`

**Interfaces:**
- Consumes: `preferences.md` as migrated in Task 3
- Produces: `POST /api/webex/add` accepting `{ title, section }`; `POST /api/webex/move` accepting `{ title, section }`

- [ ] **Step 1: Write the failing test**

Create `server/webex-sections.test.js`:

```javascript
import { describe, it, expect } from 'vitest';
import { SECTIONS, DEFAULT_SECTION } from './routes/webex.js';

describe('webex preference sections', () => {
  it('no longer carries a Priority 3 tier', () => {
    expect(SECTIONS.map(s => s.id)).not.toContain('p3');
  });

  it('still covers p1, p2, mentions and never', () => {
    expect(SECTIONS.map(s => s.id)).toEqual(['p1', 'p2', 'mentions', 'never']);
  });

  it('defaults new channels to p2, not p1', () => {
    expect(DEFAULT_SECTION.id).toBe('p2');
  });

  it('matches the migrated Priority 1 header text', () => {
    const p1 = SECTIONS.find(s => s.id === 'p1');
    expect(p1.match('### Priority 1 — Interrupt Me')).toBe(true);
  });

  it('matches the migrated Priority 2 header text', () => {
    const p2 = SECTIONS.find(s => s.id === 'p2');
    expect(p2.match('### Priority 2 — Tagged Only')).toBe(true);
  });
});
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd ~/projects/claude-hub && npm test -- server/webex-sections.test.js`
Expected: FAIL — `SECTIONS` is not exported, and `p3` is present.

- [ ] **Step 3: Update the route**

In `server/routes/webex.js`, replace the `SECTIONS` / `ADD_TO` block with:

```javascript
// Priority 3 was dropped in the 2026-09-09 tier migration. The tiers are now
// mechanical: p1 fires a priority notification in the hourly pulse, p2 only
// does so on a direct mention or a tagged thread. That is why the default for a
// newly added channel is p2 — defaulting to p1 would silently opt Ben into
// being interrupted by it.
export const SECTIONS = [
  { id: 'p1', label: 'Priority 1', match: (l) => l.startsWith('### Priority 1') },
  { id: 'p2', label: 'Priority 2', match: (l) => l.startsWith('### Priority 2') },
  { id: 'mentions', label: 'Mentions only', match: (l) => l.startsWith('## Mentions Only') },
  { id: 'never', label: 'Never scan', match: (l) => l.startsWith('## Never Scan') },
];
export const DEFAULT_SECTION = SECTIONS[1];
```

Then delete the old `const ADD_TO = SECTIONS[0];` line and update `/add` to honour a requested section:

```javascript
router.post('/add', (req, res) => {
  const title = (req.body?.title || '').trim();
  if (!title) return res.status(400).json({ error: 'No space title' });
  if (title.includes('\n') || /^https?:\/\//i.test(title)) {
    return res.status(400).json({ error: 'Title must be a resolved space name, not a URL' });
  }

  const requested = req.body?.section;
  const target = SECTIONS.find((s) => s.id === requested) || DEFAULT_SECTION;

  try {
    const { lines, spaces } = load();
    const existing = spaces.find((s) => s.title.toLowerCase() === title.toLowerCase());
    if (existing) {
      return res.status(409).json({ error: `"${title}" is already under ${existing.label}` });
    }
    const after = insertUnder(lines, target, title);
    if (!after.spaces.some((s) => s.title === title)) {
      return res.status(500).json({ error: `Write did not land. Previous file saved at ${BACKUP}` });
    }
    res.json({ ok: true, title, label: target.label, spaces: publicSpaces(after.spaces) });
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});
```

Add the shared insert helper above the routes, factored out of the old `/add` body:

```javascript
// Insert after the section's last bullet; if it has none, after the header and
// any HTML comments that follow it.
function insertUnder(lines, section, title) {
  const headerIdx = lines.findIndex(section.match);
  if (headerIdx === -1) throw new Error(`Header for ${section.label} not found in preferences.md`);
  const bullets = sectionBullets(lines, headerIdx);
  let insertAt = headerIdx + 1;
  if (bullets.length) {
    insertAt = bullets[bullets.length - 1].line + 1;
  } else {
    while (insertAt < lines.length && lines[insertAt].trim().startsWith('<!--')) insertAt++;
  }
  lines.splice(insertAt, 0, `- ${title}`);
  save(lines);
  return load();
}
```

And add the move endpoint:

```javascript
// Demoting a noisy P1 previously required dropping to the CLI.
router.post('/move', (req, res) => {
  const title = (req.body?.title || '').trim();
  const requested = req.body?.section;
  if (!title) return res.status(400).json({ error: 'No space title' });

  const target = SECTIONS.find((s) => s.id === requested);
  if (!target) return res.status(400).json({ error: `Unknown section "${requested}"` });

  try {
    const { lines, spaces } = load();
    const entry = spaces.find((s) => s.title === title);
    if (!entry) return res.status(404).json({ error: `"${title}" isn't listed` });
    if (entry.section === target.id) {
      return res.status(409).json({ error: `"${title}" is already under ${target.label}` });
    }

    lines.splice(entry.line, 1);
    const after = insertUnder(lines, target, title);
    const moved = after.spaces.find((s) => s.title === title);
    if (!moved || moved.section !== target.id) {
      return res.status(500).json({ error: `Move did not land. Previous file saved at ${BACKUP}` });
    }
    res.json({ ok: true, title, label: target.label, spaces: publicSpaces(after.spaces) });
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});
```

- [ ] **Step 4: Run to verify it passes**

Run: `cd ~/projects/claude-hub && npm test -- server/webex-sections.test.js`
Expected: PASS, 5 tests.

- [ ] **Step 5: Extend the API client**

`api.addWebexSpace(title)` sends only `{ title }`, so a picker in the form would be ignored by the route you just wrote. In `src/lib/api.js`, replace the `addWebexSpace` line and add `moveWebexSpace` beside it:

```javascript
  addWebexSpace: (title, section) => request('/api/webex/add', { method: 'POST', body: { title, section } }),
  moveWebexSpace: (title, section) => request('/api/webex/move', { method: 'POST', body: { title, section } }),
```

- [ ] **Step 6: Update the form**

In `src/components/WebexWatchForm.vue`, four edits.

**6a.** Put the tier picker in the add row, between the input and the button:

```html
      <select v-model="section" class="input tier" :disabled="busy">
        <option value="p1">P1 — interrupt me</option>
        <option value="p2">P2 — tagged only</option>
        <option value="mentions">Mentions only</option>
        <option value="never">Never scan</option>
      </select>
```

**6b.** Replace the watched-spaces row so the tag becomes a live control. `s.section` already comes back from `publicSpaces()`, so the select can bind straight to it:

```html
    <div v-for="s in spaces" :key="s.title" class="entry">
      <span class="title">{{ s.title }}</span>
      <select
        class="input tier"
        :disabled="busy"
        :value="s.section"
        @change="move(s.title, $event.target.value)"
      >
        <option value="p1">P1</option>
        <option value="p2">P2</option>
        <option value="mentions">Mentions</option>
        <option value="never">Never</option>
      </select>
      <button class="btn-remove" :disabled="busy" title="Remove" @click="remove(s.title)">✕</button>
    </div>
```

**6c.** Replace the footnote — it currently promises Priority 1, which is now both wrong and the opposite of what the default does:

```html
    <p class="footnote">
      <code>{{ prefsPath }}</code> — new spaces default to <strong>P2</strong>, which the daily
      triage covers and which only interrupts you when you're tagged directly. <strong>P1</strong>
      fires a notification during the day, so put a space there only if it's worth stopping for.
      Titles come from the live Webex room list, never from what you typed. Previous version kept at
      <code>preferences.md.bak</code>.
    </p>
```

**6d.** In `<script setup>`, add the `section` ref, thread it through `write`, and add `move`. Note `write` still takes only `title` — `addTitle` and the single-match path in `add` both call it, and both should use whatever the picker says.

```javascript
const section = ref('p2');
```

```javascript
async function write(title) {
  const data = await api.addWebexSpace(title, section.value);
  spaces.value = data.spaces;
  notice.value = `Added "${data.title}" to ${data.label}.`;
  term.value = '';
  matches.value = [];
}

async function move(title, target) {
  if (busy.value) return;
  busy.value = true;
  error.value = '';
  notice.value = '';
  try {
    const data = await api.moveWebexSpace(title, target);
    spaces.value = data.spaces;
    notice.value = `Moved "${data.title}" to ${data.label}.`;
  } catch (e) {
    // The select is now showing a tier the file doesn't have. Re-read rather
    // than leaving the UI asserting something untrue about preferences.md.
    error.value = e.message;
    await load();
  } finally {
    busy.value = false;
  }
}
```

**6e.** At the **end** of `<style scoped>`, add the picker sizing. It must come after `.input` — `.input` sets `flex: 1`, and equal-specificity rules resolve by source order, so placing this earlier would let the picker eat the row.

```css
.tier { flex: 0 0 auto; min-width: 0; padding: 6px 8px; cursor: pointer; }
```

- [ ] **Step 7: Verify end to end**

```bash
cd ~/projects/claude-hub && cp ~/projects/webex-agent/preferences.md /tmp/prefs-backup-$(date +%s).md && npm run dev
```

In the UI: add a resolved space with the picker set to Priority 2, confirm it lands under `### Priority 2 — Tagged Only` in `preferences.md`, move it to Priority 1, confirm it moved, then remove it. Re-run the Task 3 Step 5 verification to confirm the counts are back to 8 / 9 / 10.

- [ ] **Step 8: Commit**

```bash
cd ~/projects/claude-hub
git add server/routes/webex.js server/webex-sections.test.js src/components/WebexWatchForm.vue src/lib/api.js
git commit -m "feat: tier picker and move for watched spaces; default to P2"
```

---

### Task 16: Rewrite /webex-watch

The command currently states in writing that the tiers are not mechanical. That was true; it is now false, and a doc that misleads about which channels interrupt Ben is worse than no doc.

**Files:**
- Modify: `~/.claude/commands/webex-watch.md`

**Interfaces:**
- Consumes: the migrated `preferences.md` (Task 3), the new Hub endpoints (Task 15)
- Produces: documentation matching actual behaviour

- [ ] **Step 1: Fix the "how preferences.md is read" table**

Replace the rows that describe the tiers as decorative. The corrected table:

| Section | Parsed by code? | Real effect |
|---|---|---|
| `## Watchlist` | **Yes** — `lib/pulse_prefs.parse_prefs` | Anything from these people is a priority notification, in any channel |
| `### Priority 1` | **Yes** — both parsers | Full daily scan **and** priority notifications in the hourly pulse |
| `### Priority 2` | **Yes** — both parsers | Full daily scan; hourly notification only on a direct @mention or a tagged thread |
| `## Mentions Only` | **No** | Documentary only — unlisted channels already behave this way |
| `## Never Scan` | **Yes** | Hard skip before any API call |
| `## Space-Specific Rules` | No — LLM text | Per-space flagging instructions |
| `## Noise Patterns to Ignore` | No — LLM text | Global suppression |
| `## My Role & Focus` | No — LLM text | Relevance judgment |

- [ ] **Step 2: Replace consequence 2**

Delete the paragraph beginning *"The `### Priority 1/2/3` tiers are not mechanical."* Replace with:

```markdown
2. **The tiers ARE mechanical, as of 2026-09-09.** `daily_summary._parse_space_lists`
   still flattens them, so the 08:30/16:00 briefing treats P1 and P2 identically.
   But `lib/pulse_prefs.parse_prefs` keeps them apart, and the hourly pulse uses
   the difference: **P1 interrupts Ben during the day; P2 only interrupts on a
   direct @mention or new activity in a thread he's tagged in.** Moving a channel
   between tiers changes nothing about the daily briefing and everything about
   whether it can interrupt him. Priority 3 no longer exists.
```

- [ ] **Step 3: Update every `p3` reference**

Dispatch, Add mode, and Review mode all offer `p1|p2|p3|never|mentions`. Change each to `p1|p2|never|mentions`. In Review and Add mode, state what the choice means now — "p1 = this can interrupt me mid-day; p2 = only when I'm tagged" — rather than naming a tier without its consequence.

- [ ] **Step 4: Add a Watchlist mode**

```markdown
## Watchlist mode

`watchlist` — show or edit who can interrupt Ben regardless of channel.

Read `## Watchlist` in `preferences.md`. Entries are strictly
`- Display Name <email@cisco.com>`; a bare name parses into `unresolved` and
matches **nothing, forever**. Resolve every address before writing it:

```bash
~/.config/claude-graph/bin/msgraph resolve-person '<NAME>'
```

To add someone, confirm the resolved address, then insert under `## Watchlist`.
To check the file's health:

```bash
cd ~/projects/webex-agent && .venv/bin/python3.12 -c "
import sys; sys.path.insert(0, 'scripts')
from lib.pulse_prefs import parse_prefs
p = parse_prefs(open('preferences.md').read())
print(len(p.watchlist), 'resolved')
print('UNRESOLVED:', p.unresolved or 'none')
"
```

Any name in `unresolved` is silently inert. Report it and offer to fix it.
```

- [ ] **Step 5: Note the Hub surface**

Add to the Guardrails section:

```markdown
- **The Hub can do this too.** `WebexWatchForm.vue` (under the `webex-watch`
  skill view in Hanuman) lists the tiers, adds with a tier picker, and moves
  channels between tiers. It defaults new channels to **P2** deliberately —
  defaulting to P1 would silently opt Ben into being interrupted.
```

- [ ] **Step 6: Verify no stale references survive**

```bash
grep -n "p3\|Priority 3\|not mechanical" ~/.claude/commands/webex-watch.md || echo "clean"
```

Expected: `clean`, or only the deliberate "Priority 3 no longer exists" sentence.

- [ ] **Step 7: Commit**

```bash
cd ~/.claude && git add commands/webex-watch.md && \
  git commit -m "docs: rewrite /webex-watch for mechanical tiers and the watchlist"
```

If `~/.claude` is not a git repo, skip the commit and say so.

---

## Final verification

- [ ] **Full suite, both repos**

```bash
cd ~/projects/webex-agent && .venv/bin/python3.12 -m pytest tests/ -v
cd ~/projects/claude-hub && npm test
```

- [ ] **One real end-to-end run**

```bash
cd ~/projects/webex-agent && bash scripts/run_pulse.sh; echo "exit=$?"
.venv/bin/python3.12 -c "
import json
d = json.load(open('output/pulse.json'))
print('status  ', d['status'])
print('sources ', d['sources'])
print('priority', sum(1 for i in d['items'] if i['tier']=='priority'))
print('silent  ', sum(1 for i in d['items'] if i['tier']!='priority'))
print('notified', d['notified_this_run'])
"
```

- [ ] **Confirm `.last_run` is untouched**

```bash
cd ~/projects/webex-agent && cat .last_run && ls -l .last_run .last_pulse_run
```

`.last_run` must still hold the timestamp of the last `daily_summary.py` run, not the pulse's.

- [ ] **Confirm the daily triage still works**

```bash
cd ~/projects/webex-agent && SUMMARY_LOOKBACK_H=2 bash scripts/run_summary.sh; echo "exit=$?"
```

This is the single most important regression check in the plan. If the 08:30 briefing broke, everything else is moot.
