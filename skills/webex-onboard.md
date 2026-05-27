---
name: webex-onboard
description: First-time setup for Webex Agent — teaches the triage system who you are, classifies your spaces, and runs your first briefing. Run this after installing the plugin.
user-invocable: true
---

# Webex Onboard

Interactive onboarding for the Webex Agent. This skill configures your triage
preferences by learning about your role, classifying your spaces, and running
a calibration triage.

**Prerequisites:** The Webex MCP server must be connected and authenticated
(you should be able to call `list_spaces` successfully).

Arguments passed: `$ARGUMENTS`

---

## Dispatch

### If `$ARGUMENTS` is empty — full onboarding flow

Run all steps below in sequence.

### If `$ARGUMENTS` is `spaces` — just the space classification step

Skip to Step 2 (useful for re-classification after a role change).

### If `$ARGUMENTS` is `calibrate` — just the calibration triage

Skip to Step 3 (useful for tuning after initial setup).

---

## Step 1: Learn About You

Goal: Populate the "My Role & Focus" section of `preferences.md`.

Ask the user (one message, not one at a time):

> I need to understand your role so I can judge what's relevant to you.
>
> 1. **What's your name and email?** (I need your Webex email to identify your messages)
> 2. **What's your role?** (title, team, org)
> 3. **What are your 3-5 key focus areas?** (projects, products, programs you own or care about)
> 4. **What do you NOT need to see?** (types of noise — deploy alerts, social chat, etc.)

Once they answer, use `update_preferences` to write the "My Role & Focus"
section. If the tool isn't available, write directly to `preferences.md` using
the Edit tool.

Also set their email via the same preferences update or note it for Step 3.

---

## Step 2: Classify Spaces

Goal: Populate "Always Scan", "Mentions Only", and "Never Scan" sections.

### 2a. Fetch and analyze their spaces

Call `list_spaces` to get the full list. Group them:

- **DMs** (type=direct) — skip these, they're always scanned automatically
- **Group chats** (small, auto-named like "Name, Name") — skip, always scanned
- **Channels** — these need classification

Sort channels by `lastActivity` (most recent first). Take the top 40 most
active channels (ignore anything dormant for 30+ days).

### 2b. Auto-suggest classifications

For each channel, generate a suggestion based on name patterns:

| Pattern | Suggestion |
|---------|-----------|
| `proj-*` or `squad*` | Always Scan P1 or P2 |
| User's focus area keywords in title | Always Scan P2 |
| `help-*`, `ask-*`, `ask *` | Mentions Only |
| Social keywords (fitness, parents, classifieds, finance, home) | Never Scan |
| `team-*` matching user's team | Always Scan P3 |
| `team-*` not matching | Mentions Only |
| Everything else | Mentions Only |

### 2c. Present for rapid-fire confirmation

Show the suggestions in batches of 10, grouped by suggested category:

```
Here's my suggested classification for your most active channels.
Reply with changes only — anything you don't mention, I'll apply as suggested.

**Suggested: Always Scan P1** (your direct projects)
- proj-my-feature → P1
- Squad: My Team Auth → P1

**Suggested: Always Scan P2** (cross-functional, stakeholder)
- Identity Product Team → P2
- SCC/Duo Product Sync → P2

**Suggested: Always Scan P3** (engineering awareness)
- team-my-eng → P3
- metrics-dashboard → P3

**Suggested: Mentions Only** (only flag if you're @mentioned)
- help-sre → Mentions Only
- Apps Team → Mentions Only

**Suggested: Never Scan** (skip entirely)
- classifieds → Never
- Home Owners → Never

Changes? (e.g., "move Apps Team to P3", "add team-security to P2",
"never scan metrics-dashboard")
```

Apply their corrections, then write all classifications to `preferences.md`
using `update_preferences` or direct file edit.

### 2d. Space-specific rules (optional)

After classification, ask:

> Any spaces that need special rules? For example:
> - "In help-sre, only flag if I'm directly mentioned"
> - "In proj-alpha, flag everything — this is my primary project"
> - "In Identity Product Team, skip social chat, only flag decisions"
>
> (Skip if none come to mind — you can add these anytime later.)

Write any rules to the "Space-Specific Rules" section.

---

## Step 3: Calibration Triage

Goal: Run a real triage and use feedback to fine-tune.

### 3a. Run a 24-hour triage

Use the `triage` MCP tool with a 24-hour lookback (or `get_messages` + local
triage logic if the tool isn't available). Present the full briefing.

If it's a weekend or holiday and spaces are quiet, extend to 48h or 72h to get
meaningful content.

### 3b. Collect feedback

After showing results, ask:

> How did that look? A few questions:
>
> 1. **Anything flagged that shouldn't have been?** (I'll add it to noise filters or Never Scan)
> 2. **Any spaces missing?** (Active spaces that should be in Always Scan but weren't?)
> 3. **Were the priority levels right?** (Anything in "Blocked" that was really just FYI? Anything in FYI that should have been higher?)

Apply corrections to preferences immediately.

### 3c. Noise patterns

If they identified false positives, ask about patterns:

> Should I always ignore messages from specific bots or about specific topics?
> Examples:
> - "Ignore bot messages from deploy-bot and observ"
> - "Skip PR merge notifications unless they mention me"
> - "Filter out good morning greetings"

Write patterns to the "Noise Patterns to Ignore" section.

---

## Step 4: Wrap Up

Summarize what was configured:

```
Setup complete! Here's your configuration:

- **Role**: [their role summary]
- **Always Scan**: X spaces (P1: N, P2: N, P3: N)
- **Mentions Only**: X spaces
- **Never Scan**: X spaces
- **Space rules**: N custom rules
- **Noise filters**: N patterns

Your triage is ready to use:
- Run `/webex-triage` anytime for an on-demand briefing
- Set up cron jobs for automated 8:30am + 4pm briefings (see README)
- Say "add [space] to Always Scan P2" anytime to update preferences
- Say "/webex-onboard calibrate" to re-run the calibration step

The system learns from feedback — the more you tell it what matters,
the better it gets.
```

---

## Rules

- Keep the pace fast — don't over-explain each step. Users who installed
  a Claude Code plugin know what they're doing.
- If a step fails (e.g., can't list spaces), diagnose and suggest fixes
  rather than silently skipping.
- Never overwrite existing preferences without asking — if `preferences.md`
  already has content, confirm before replacing.
- The whole onboarding should take 5-10 minutes. Don't make it feel like
  a chore.
