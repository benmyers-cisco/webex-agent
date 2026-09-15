# Hourly Pulse — Design

**Status:** APPROVED — cycle 1 ready to plan
**Author:** Nigel Watters (chief of staff agent), with Ben Myers
**Date:** 2026-09-09
**Scope:** Cycle 1 only. Cycles 2 and 3 sketched at the end.
**Repos touched:** `~/projects/webex-agent`, `~/projects/claude-hub`, `~/.claude/commands`

---

## Purpose

An hourly job that surfaces the small number of messages that genuinely cannot wait until the
4pm briefing, notifies once when they exist, and stays completely silent when they don't.

**The outcome, in Ben's words:** *"be able to respond to urgent messages throughout the day and
not get caught up in things I might be tagged in or messages that aren't important to address in
the middle of the day."*

That second clause is the harder half. The existing 8:30/4pm triage is a **relevance** engine — it
is supposed to hand over four things. An hourly job built on the same bar would hand over four
things an hour, which is the problem, not the solution. The pulse is an **urgency** engine, and its
normal output is nothing.

---

## Scope

**In scope for cycle 1:**

- Webex: P1 channels, P2 channels, DMs, group chats, watched threads
- Work email via `msgraph`: direct human mail plus a named-sender watchlist
- One notification per run when new priority items exist
- A Hanuman Hub panel showing every triggering message verbatim
- Restructuring `preferences.md` from P1/P2/P3 to P1/P2/everything-else
- Adding a `## Watchlist` section to `preferences.md`
- Fixing the Hub's watch form: tier picker, move action, default to P2, drop dead `p3`
- Rewriting `~/.claude/commands/webex-watch.md`, whose current claims become false

**Not in scope for cycle 1:**

- Quiet hours or in-meeting suppression — cycle 2
- Replying from the Hub panel — cycle 2, approved 2026-09-09
- Slack via the approved app — cycle 3
- Any change to `daily_summary.py`'s behavior, prompt, or output
- Thread context in the panel beyond the triggering message and a deep link

---

## Current state

| Component | State |
|---|---|
| `scripts/daily_summary.py` | 38K. Relevance triage, 5 sections, markdown out. Runs 08:30 and 16:00 weekdays. |
| `scripts/run_summary.sh` | Hardened: DNS probe, `caffeinate` hold, timeout-bounded AWS refresh, failure files. |
| `preferences.md` | 13 P1 / 4 P2 / 1 P3 channels, 4 Mentions Only, space-specific rules, noise patterns. |
| `.last_run` | **Single shared window file.** |
| `.watched_threads.json` | Threads Ben has been tagged in, pruned at 28 days. |
| Hub `server/routes/webex.js` | Reads and writes `preferences.md`. `SECTIONS` covers p1/p2/p3/mentions/never. |
| Hub `WebexWatchForm.vue` | Lists spaces with a tier tag. Add and remove only — **no tier picker, no move.** |
| `msgraph email search` | Works on a warm SSO cookie. KQL only; no time-ordered inbox listing. |

### The tiers are currently decorative

`_parse_space_lists` flattens `### Priority 1/2/3` into one set. `/webex-watch` documents this
explicitly: *"The `### Priority 1/2/3` tiers are not mechanical... Don't tell the user P1 gets
scanned differently than P3 — it doesn't."*

**The pulse makes that statement false.** P1 now means "interrupt me." This is the single most
important migration consequence in this document, and it is why the `/webex-watch` rewrite is in
cycle 1 rather than deferred.

---

## The priority model

Three inputs, evaluated independently. Any one firing makes an item priority.

### 1. Watchlist person — sufficient on its own, within an eligible space

Anything from a watchlist person is priority. It is the primary driver, not a qualifier layered on a
channel rule: a watchlist sender needs no @mention, no thread membership, and no channel tier to
reach `priority`.

**Scope, as built in cycle 1:** this applies in every space the pulse fetches — DMs, group chats,
P1, P2, Mentions Only, and any space with a thread Ben has been tagged in. It does **not** apply in
a named space that appears in no list, because collection decides eligibility from the space before
any sender is examined, so a watchlist sender in an unlisted named space is never fetched.

Reaching them too is **deferred past cycle 1**, deliberately, for three reasons:

- It is a scoped design problem, not a fix. Delivering it means scanning ~400 unlisted spaces every
  hour to find a handful of senders.
- The common case is already covered. A watchlist person reaching Ben outside a listed space almost
  always does it in a DM or a group chat, and both are fetched unconditionally.
- This is being built in cycles, and an unbuilt capability that no document promises is a backlog
  item rather than a hole.

Until it is built, no document may state the unconditional form. The earlier wording ("in any
channel, including channels that appear in no list") described behaviour the code did not have, in
three places at once — including `preferences.md`, which is passed to the classifier as prompt text,
so the model was being told a rule collection could not deliver.

Stored as `- Name <email>` because Webex messages carry `personEmail`, not display names. Emails
resolved with `msgraph resolve-person`.

Cycle 1 watchlist: Taylor · Didi Dotan · Brian Lindauer · Rory Scott · Dros Adamson ·
Shyam Srinivasan · Einar Nilsen-Nygaard · Aamir Yousufzai · Matt Caulfield · Vinita Karbhari ·
Tal Surasky.

**Mentees are deliberately excluded** (Adam Greer, Sam Betlej, Abdul, Devon, Ethan) — decided
2026-09-09. They are lower-urgency and belong in the 4pm briefing.

### 2. Channel tier

| Tier | Hourly pulse | Daily triage (08:30 / 16:00) |
|---|---|---|
| **P1** | Qualifying activity is priority | Full scan — unchanged |
| **P2** | Priority **only** if Ben is directly @mentioned, or there is new activity in a thread he has been tagged in | Full scan — unchanged |
| **Mentions Only** | Fetched, but **only** messages that @mention Ben or come from a watchlist sender are collected at all; those are then judged as P2 | Full scan — unchanged |
| **Everything else** | Never fetched — so nothing in it can reach the panel, including a watchlist sender (see rule 1's scope) | @mention-only, as today |

"Mentions Only" is a real tier, not documentation of what unlisted spaces already do. An unlisted
space does not surface on @mention in the pulse; it does not surface at all. Demoting a channel to
Mentions Only therefore keeps @mentions reaching Ben while dropping the channel's ordinary traffic —
which is what the label says and what the daily triage does.

DMs and group chats are treated as P1, unconditionally and with no entry in any list. A DM is
inherently aimed at Ben, so it clears the can-this-wait test far more often than a channel post
does, and an ad-hoc group chat is a named handful of people rather than a broadcast. A group chat is
recognised by `daily_summary._is_group_chat` — imported, not re-implemented, so the pulse and the
daily triage cannot drift on which spaces this covers.

### 3. Meeting-imminent

A message about a meeting on today's calendar starting within roughly two hours is priority at any
tier.

### The test the model applies

Not *"is this important."* The literal question is **"can this wait until the next briefing?"** If
yes, it is not priority. Most of what reaches Ben is important *and* can wait, and only this framing
separates the two.

"The next briefing" is time-dependent, and getting this wrong would quietly break the afternoon:

| Run time | Next briefing | Effective bar |
|---|---|---|
| Before 16:00 | Today's 16:00 triage | Can this wait a few hours? |
| 16:00 or later | Tomorrow's 08:30 triage | Can this wait until tomorrow morning? |

The bar therefore **rises** after 16:00, which is correct — something that cannot wait overnight is
more urgent than something that cannot wait two hours. The prompt is given the current time and the
next briefing time explicitly rather than inferring either.

### Explicitly not priority

These sit in the panel, silent, and are the cases Ben named directly:

- @mentioned or tagged with no ask directed at him
- A discussion he could improve — the existing "Opportunities to add value." Not a mid-day
  interrupt by design.
- Decisions made without him that need no same-day answer
- Anything already answered by someone else in the thread
- Anything where Ben sent the last message
- Status updates, FYIs, shared documents

### Channel split — approved 2026-09-09

Cut from 13 P1 channels to 8. Thirteen channels that all interrupt is not a priority tier. The four
demoted channels keep full daily-triage coverage; they simply no longer interrupt mid-day, and any of
them can be promoted in cycle 2 once a week of real data exists.

**P1 — notify (8):** C3 + CUI · Identity in Cloud Control Working Group · Identity Fabric/CUI
Dependencies · Mini EC with CUI · Identity App for C3 GA · PCA - Cisco Identity Fabric (Meraki
Access Manager) PKI · PureCA Identity Fabric · Duo + Access Manager + Meraki End Users

**P2 — tagged-only (9):** Identity Fabric Strategy & Planning · Identity Fabric Requirements ·
IA Sprint — Fabric Folks · CII + Duo - Support Scope and Case Routing Project · AI Canvas, UAIA,
C3 - Product Integration Execution (PM/Eng) · SCC - CII Discussion · Duo/ CII in India SCC Needs ·
Cloud Control and Identity provisioning · help-identity-security-intelligence

**Unchanged, no hourly (4):** Duo PM Sync · Cisco Security PM Community · Identity Intelligence
All-Hands · 🗣️Ask/Tell Duo Support (No SLA)

**One deletion:** `Aamir Yousufzai, Ben Gaspar, Mike Wojan` is a group chat, not a channel. Group
chats are scanned unconditionally regardless of any list, and the title rots as membership changes
— so the entry does nothing today and will break silently. Covered by the DM/group-chat rule.

Because the daily triage flattens the tiers, **moving a channel from P1 to P2 changes nothing about
the 08:30/16:00 briefing.** Only removing a channel from Always Scan would, and no channel is
proposed for removal.

---

## The notification model

**Each run: if the count of newly qualifying items is ≥ 1, fire exactly one notification — one
banner, one sound. Otherwise nothing.**

The number of items does not change the loudness. Neither does who sent them or where.

**"Newly" is load-bearing.** An unanswered ask from 9:15 must not re-fire at 10:15, 11:15, and noon.
Fingerprint de-dupe governs the notification itself, not just the display.

| Surface | Behavior |
|---|---|
| Notification banner | Once per run. Body carries the count and a hint: *"3 need you — Rob Scott (C3 + CUI), Shyam (DM), +1."* |
| Dock badge | Outstanding count via `navigator.setAppBadge(n)`. A persistent state indicator, not an interrupt — so it does not violate once-per-run. |
| Sound | Once per run, with the banner. |
| Panel | Updated every run regardless, silently. |

---

## Daily clear, and the boundary with the daily triage

- Panel and badge clear at the first run of each day. Yesterday archives to
  `output/pulse-YYYY-MM-DD.json`; today starts empty.
- The fingerprint set clears with it. De-dupe is within-day only.
- **The first run of the day starts its window at 08:30 local, not at yesterday's last run.**

That last rule matters: without it, the 09:15 pulse re-serves everything the 08:30 briefing just
delivered. It gives a clean division of labor:

> **The pulse owns 08:30–17:15 today. The daily triage owns overnight and anything that carries
> across days.**

So an unanswered ask from yesterday does not reappear in the pulse. It surfaces in the next 08:30
briefing under "Blocked on you," which already has the lookback and the carry-forward logic.

### Within-day carry-forward — added 2026-09-15

Across days, the division above holds. *Within* a day it originally did not, and that was a bug
rather than a design: `pulse.json` is rebuilt every run from that run's candidates alone, so an item
was visible for exactly one hour and then gone — whether or not Ben looked in that hour, whether or
not a banner fired, whether or not anyone had dealt with it. On 2026-09-15 two DMs from Aanjan Ravi
were collected at 13:15, correctly panelled, and absent from the artifact by 14:15. The seen store
had been tracking a `resolved` flag for them the whole time and nothing ever read it.

So: **the panel shows everything from 08:30 today that is still outstanding**, not just the last
hour. `pulse_output.carry_forward` restores unresolved items from earlier runs beside the new ones,
and the only thing that takes one off the panel is a real signal —
`pulse_sources_webex.resolve_answered` asking whether Ben has spoken in that space since. Carried
items are flagged `carried_forward` and counted in `carried`, because `window` still describes only
what the current run examined.

Three properties this must keep:

- A carried item keeps its original tier and `notified`. Restoration, not re-judgement: an item that
  could wait at 13:15 has not become urgent by 14:15, and carrying must not launder away an
  interruption already delivered.
- A carried priority item whose banner never fired is still eligible for its retry. Before this, the
  retry promise expired with the window.
- Resolution degrades toward visible. A failed reply-check leaves the item on the panel; a stale
  panel row costs a glance, and one hidden by accident is invisible.

---

## Architecture

### New — `~/projects/webex-agent/`

| File | Purpose |
|---|---|
| `scripts/hourly_pulse.py` | The pulse engine |
| `scripts/run_pulse.sh` | Wrapper. Sources `lib/wait_for_network.sh` and `aws_refresh.sh` unchanged. |
| `.last_pulse_run` | Own window state. **Never writes `.last_run`.** |
| `.pulse_seen.json` | Within-day item fingerprints. Powers de-dupe and self-clearing. |
| `output/pulse.json` | Today's state. The only artifact the Hub reads. |
| `output/pulse-YYYY-MM-DD.json` | Yesterday's archive. |

### New — `~/Library/LaunchAgents/`

`com.webex-agent.hourly-pulse.plist` — `StartCalendarInterval` with one explicit entry per hour at
:15 past, **09:15 through 17:15 inclusive, weekdays — nine runs a day.** The :15 slot is chosen
because the existing meeting-check crons hold :25 and :55.

So the pulse covers 08:30 (its first window's start) through 17:15. Anything arriving after the last
run belongs to the next morning's briefing.

> **WARNING — never `StartInterval`.** It never overlaps, so one hung run kills a periodic job
> silently and permanently. `launchctl list` showing a PID *is* the bug in that failure mode.

### New — `~/projects/claude-hub/`

| File | Purpose |
|---|---|
| `server/routes/pulse.js` | `GET /api/pulse` reads today's artifact. `POST /api/pulse/ack` marks items read, which zeroes the dock badge. Ack is **not** a reply and **not** a dismissal — items self-clear and the panel clears daily. |
| `src/components/PulseView.vue` | The panel |

### Changed

| File | Change |
|---|---|
| `preferences.md` | P1/P2 restructure, drop `### Priority 3`, add `## Watchlist` |
| Hub `server/routes/webex.js` | Drop `p3` from `SECTIONS`; `ADD_TO` → P2; add a move endpoint |
| Hub `WebexWatchForm.vue` | Tier picker on add, move control, fix the "new spaces go under Priority 1" copy |
| Hub `src/stores/notifications.js` | Extend for pulse badge count and sound |
| `~/.claude/commands/webex-triage.md` | Its "tiers are not mechanical" claim is now false |

### Reuse, by import rather than copy

From `daily_summary.py`: `get_webex_client`, `get_claude_client`, `load_preferences`,
`format_messages`, and the watched-threads helpers.

`_parse_space_lists` **cannot** be reused — it flattens the tiers, and the tiers now matter. The
pulse needs a tier-aware parser. This is the only piece of parsing logic that gets duplicated, and
the reason is worth a comment in the code.

---

## Data flow

1. Window = `max(.last_pulse_run, today 08:30 local)`, fallback one hour.
2. **Webex** — fetch P1 channels, P2 channels, DMs, group chats, and watched threads.
3. **Email** — `msgraph email search "received>=<window>"`. Keep mail where Ben is in To or Cc from
   a human sender, plus anything from a watchlist sender. Hard-exclude calendar accept/decline
   notices, distribution-list blasts, and automated senders.

   **One deliberate exception:** Slack DM and direct-mention notification emails from
   `notification@slack.com` are parsed and shown in the panel's silent section — **never priority**.
   They represent real human messages (Yizhen Shi's 2026-09-08 walk-through DM arrived this way) and
   they are the only Slack visibility available until the app is approved in cycle 3. The sender is
   automated, so they can never clear the priority bar; the exception is display-only.
4. **Calendar** — today's events for the meeting-imminent trigger.
5. One Bedrock call per source bundle, classifying each candidate `priority | panel | drop` with a
   reason and a draft reply.
6. Fingerprint every item. Suppress already-seen items from the *notify* set; keep them in the panel.
7. Self-clear: mark a seen item resolved if Ben replied, or if someone else answered it.
8. Write `pulse.json`. Fire one notification if any newly qualifying priority items exist.

---

## Output contract — `pulse.json`

```json
{
  "generated_at": "2026-09-09T14:15:03Z",
  "status": "ok",
  "window": { "from": "2026-09-09T13:15:00Z", "to": "2026-09-09T14:15:03Z" },
  "sources": {
    "webex": "ok",
    "email": "degraded: msgraph auth expired",
    "calendar": "ok"
  },
  "notified_this_run": true,
  "filtered": 0,
  "carried": 1,
  "items": [
    {
      "id": "sha1-fingerprint",
      "tier": "priority",
      "source": "webex",
      "trigger": "watchlist",
      "channel": "C3 + CUI",
      "space_id": "Y2lzY29zcGFyazovL3VzL1JPT00v…",
      "from": { "name": "Rob Scott", "email": "rorscott@cisco.com" },
      "at": "2026-09-09T14:02:11Z",
      "text": "full verbatim message body",
      "why": "Watchlist sender. Asks whether the cross-cluster walk is verified before the EC pre-read.",
      "draft_reply": "…",
      "link": "https://web.webex.com/spaces/…",
      "first_seen": "2026-09-09T14:15:03Z",
      "notified": true,
      "resolved": false
    }
  ]
}
```

Items carry the **full verbatim `text`**, not a model-written summary, because the panel shows the
actual messages. `sources` is not decoration — a degraded email fetch has to be visible.

Three fields exist so that nothing the engine removed or held over can pass for a quiet hour:
`filtered` counts what the relevance filter dropped, `carried` counts how many of `items` predate
this run's `window`, and `carried_forward: true` appears on those items (additive and only ever true,
like `classification_failed`). `space_id` is what lets a later run ask whether Ben has since replied.

---

## The Hub panel

Two sections. Both show every message; nothing is merged or collapsed.

**⚡ Triggered notifications** — every message that fired a notification, newest first, grouped by
channel but never merged. Each row: who, when, where, the verbatim message, the trigger
(`watchlist` / `p1_channel` / `p2_mention` / `p2_thread` / `dm` / `meeting_imminent`), and the draft
reply. Deep link into the space.

**◻ Silent — came in, didn't interrupt you** — the panel-tier items at the same fidelity.

Collapse governs the interrupt only. The panel is complete.

**Cycle 1's panel is read-only** — click through to Webex or Outlook to reply. Replying from the Hub
moves to cycle 2 (approved 2026-09-09).

---

## Failure handling

- Network probe before blaming credentials. A DNS failure reports as a DNS failure, not as expired
  AWS creds.
- `aws_refresh.sh` with its timeout. Never inline `duo-sso` — it waits on a dead browser forever.
- **Email failure degrades, never disappears.** `sources.email` carries the reason and the panel
  shows an amber strip. `msgraph` auth is browser-cookie scraping with no refresh token, so this
  path *will* fail regularly and unattended. Treating it as an exception rather than an expected
  state would be a design error.
- Total failure writes `pulse.json` with `status: "failed"` and a reason. The panel says *did not
  run* rather than showing an empty list — silence must never read as a quiet hour.
- **Staleness check:** the Hub flags the pulse stale if `generated_at` is more than 90 minutes old.
  A hung launchd job still shows a PID; the only honest signal is the timestamp on the artifact.

---

## What this must not break

> **WARNING — `.last_run` is shared.** If the pulse advances it, the 08:30 briefing looks back one
> hour instead of since 16:00 yesterday. The symptom would read as "quiet week," not "broken job."
> The pulse writes `.last_pulse_run` and nothing else.

- No change to `daily_summary.py`'s prompt, sections, markdown output, or schedule.
- No channel leaves Always Scan, so no channel loses daily-triage coverage.
- `preferences.md` is gitignored with no history. Read before writing; targeted edits only; the
  Hub's `save()` already keeps `preferences.md.bak`.

---

## Testing

| What | How |
|---|---|
| Tier-aware parser | Unit test against the real `preferences.md` structure, including the em-dash headers and HTML comments |
| Priority classification | Fixture transcripts for each trigger, plus negative fixtures for each "explicitly not priority" case |
| De-dupe | Same item across two consecutive runs notifies once |
| Daily clear | A run crossing midnight archives, empties, and resets the window to 08:30 |
| `.last_run` isolation | Assert the file's mtime and contents are untouched by a pulse run |
| Degraded email | Force an `msgraph` failure; assert `status: "ok"`, `sources.email` degraded, Webex items still present |
| Total failure | Assert `status: "failed"` and a human-readable reason in `pulse.json` |
| Staleness | Backdate `generated_at`; assert the Hub renders the stale state |

---

## Cycles 2 and 3

**Cycle 2 — tune from a week of real data, then add suppression.**

The obvious way to tune is to ask *"what badged me that could have waited?"* That measures
**precision only**. It never measures the misses.

This is the exact trap in the advisor-corpus audit, where `classify()` only adjudicated downward
and every published figure turned out to be precision with recall never measured at all. So cycle 1
logs the **panel** items — the ones that deliberately stayed silent — at the same fidelity as the
notified ones. That data is cheap now and impossible to reconstruct later.

Cycle 2 then asks both questions, with counts beside rates:

1. What interrupted Ben that could have waited until 4pm?
2. What sat silent in the panel that he later wished had interrupted him?

Then: quiet hours, in-meeting suppression using the Hub's existing calendar route, and the richer
prioritization Ben flagged on 2026-09-09.

**Cycle 3 — Slack via the approved app.** Slack becomes a first-class source with the same tiering
and the same bar. The interim `notification@slack.com` parsing added in cycle 1 retires at that point.

---

## Open questions

All cycle-1 blocking questions were resolved on 2026-09-09. What remains:

1. **Watchlist emails are unresolved.** Eleven names, no addresses yet. Webex matches on
   `personEmail`, so every name must go through `msgraph resolve-person` before the watchlist does
   anything. A name that fails to resolve must **fail loudly at startup**, not silently match
   nothing — that is the same failure mode as an unresolved space title in `preferences.md`.
   "Taylor" in particular is a first name only and may be ambiguous.
2. **Working-hours envelope.** 09:15–17:15 weekdays, nine runs, derived from the existing triage
   schedule rather than stated by Ben. Worth confirming once he sees it running — particularly
   whether the last run should be later than 17:15.
3. **Meeting-imminent lookahead** is set at two hours by assumption. A tuning knob for cycle 2.

---

## Decision log

| Date | Decision | Source |
|---|---|---|
| 2026-09-09 | Surface in Hanuman Hub rather than macOS-only or Webex-space delivery | Ben |
| 2026-09-09 | Badge and sound gated on priority; non-priority gets no indicator at all | Ben |
| 2026-09-09 | Email scope: direct human mail plus a named-sender watchlist | Ben |
| 2026-09-09 | Watchlist person is priority unconditionally, in any channel | Ben |
| 2026-09-09 | **Narrowed:** watchlist is sufficient on its own *within an eligible space* (listed, DM, group chat, Mentions Only, tagged thread). Unlisted named spaces are deferred past cycle 1 — delivering it means scanning ~400 spaces hourly, and DMs plus group chats already cover the common case. Rule 1, the tier table and `preferences.md` were reconciled to the code rather than the code to them. | Ben, on final-review F15 |
| 2026-09-09 | Mentions Only is a real tier the pulse implements: fetched, filtered to @mentions and watchlist senders, judged as P2. It was previously indistinguishable from Never Scan. | final-review F4 |
| 2026-09-09 | Channel tiers restructured to P1 / P2 / everything-else | Ben |
| 2026-09-09 | P2 is priority only on a direct @mention or a tagged thread | Ben |
| 2026-09-09 | DMs and group chats treated as P1 | Nigel proposed, Ben accepted |
| 2026-09-09 | Mentees excluded from the watchlist | Ben |
| 2026-09-09 | Panel shows every triggering message verbatim, uncollapsed | Ben |
| 2026-09-09 | One notification per run when new priority items exist — not per person or channel | Ben, superseding an earlier per-person-per-channel answer |
| 2026-09-09 | Items clear daily | Ben |
| 2026-09-09 | First run of the day windows from 08:30, ceding overnight to the daily triage | Nigel |
| 2026-09-09 | Approach A — standalone `hourly_pulse.py`, not a mode on `daily_summary.py` | Nigel proposed, Ben accepted |
| 2026-09-09 | Log panel-tier items in cycle 1 so cycle 2 can measure recall, not just precision | Nigel |
| 2026-09-09 | The bar is "can this wait until the *next* briefing," so it rises after 16:00 | Nigel, from spec self-review |
| 2026-09-09 | P1 cut from 13 channels to 8; the other 4 keep daily-triage coverage | Nigel proposed, Ben approved |
| 2026-09-09 | Matt Caulfield on the watchlist. Matt Miller not. | Ben |
| 2026-09-09 | Panel is read-only in cycle 1; replying moves to cycle 2 | Nigel recommended, Ben approved |
| 2026-09-09 | Slack notification emails: panel-only, never priority | Nigel, Ben deferred the call |
| 2026-09-15 | **Within-day carry-forward.** The artifact is a view of the whole day, not of the last hour: unresolved items from earlier runs are restored beside the new ones, and only Ben having replied takes one off the panel. "Items clear daily" is unchanged — the day boundary is still the reset. | Ben, on the Aanjan Ravi miss |
