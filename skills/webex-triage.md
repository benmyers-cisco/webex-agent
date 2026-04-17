---
name: webex-triage
description: Triage your Webex spaces — find what needs your attention, what was decided without you, and where you can add value. Works with any Webex MCP server that provides list_spaces and get_messages tools.
user-invocable: true
---

# Webex Triage

Scan your Webex spaces and produce an actionable briefing organized by priority.
This skill works with any Webex MCP server that exposes `list_spaces` and
`get_messages` tools.

Arguments passed: `$ARGUMENTS`

---

## Dispatch

### If `$ARGUMENTS` is empty — default triage (last 8 hours)

Run the full triage with a lookback of 8 hours.

### If `$ARGUMENTS` is a timeframe — custom lookback

The user may pass a timeframe like `24h`, `2d`, `1w`, or a date. Use that as
the lookback period.

### If `$ARGUMENTS` is `teach` or `preferences` — show/edit relevance rules

Help the user define what's relevant to them. See the Trainable Preferences
section below.

---

## Step 1: Identify yourself

Before triaging, you need to know who the user is so you can distinguish their
messages from others'. Ask the Webex API or ask the user directly:

> "What's your Webex email? (I need this to know which messages are yours.)"

If you've already learned it in this session or from preferences, skip the ask.

---

## Step 2: Select spaces to analyze

Not every space deserves attention. Use this logic to decide what to pull:

### Always include (if there's new activity in the lookback period):
- **Direct messages (DMs)** — someone wrote to you specifically
- **Small group chats (roughly 10 or fewer people)** — these are working groups
  where conversation is likely relevant to you

### Conditionally include:
- **Large channels (more than ~10 people)** — only if:
  - You were **@mentioned** and haven't responded yet, OR
  - You were **newly added** to the channel in the last 24 hours (catch-up)
- If you can't determine member count, err on the side of including the space

### Skip:
- Spaces the user has explicitly marked as irrelevant (see preferences)
- Spaces with no activity in the lookback period

### How to do this:
1. Call `list_spaces` to get the full list
2. For each space with recent activity, call `get_messages` with the lookback
   timeframe
3. Apply the selection logic above
4. Aim for no more than ~20 spaces per triage — if there are more, prioritize
   DMs and small groups first

---

## Step 3: Triage each space

For every selected space, read the messages and categorize what you find into
exactly these four buckets. Only include buckets that have content.

### 🔴 Blocked on You
Someone is waiting for your input, approval, decision, or response. They
cannot move forward without you.

For each item include:
- **Who** is waiting and **what** they need
- **How long** they've been waiting (if you can tell from timestamps)
- A **draft response** in a quoted block — concise, professional, ready to
  send with minimal editing

This is the highest priority bucket. If someone asked you a direct question and
you haven't answered, it goes here.

### 🟡 Decisions Made Without You
Things that were decided, concluded, or changed direction — and they affect
your work — but you weren't part of the discussion.

For each item include:
- **What** was decided and **by whom**
- Whether you need to **weigh in** or just be **aware**

### 🟢 Opportunities to Add Value
Discussions where your expertise or perspective could meaningfully help, but
nobody asked you directly. These are chances to be proactive.

For each item include:
- **What's** being discussed and **why** your input matters
- A **draft message** in a quoted block you could send to contribute

### ℹ️ FYI
Important context or updates. No action needed, but useful to know.
Keep these brief — one or two lines each.

---

## Step 4: Present the briefing

Group results **by priority across all spaces** (not per-space). This way the
user sees all urgent items together, not buried inside individual space
summaries.

Format:

```
# Webex Briefing — <date and time>
Looking back <timeframe> across <N> spaces.

## 🔴 Blocked on You

**<Space Name>**
<item with draft response>

**<Space Name>**
<item with draft response>

---

## 🟡 Decisions Made Without You

**<Space Name>**
<item>

---

## 🟢 Opportunities to Add Value

**<Space Name>**
<item with draft message>

---

## ℹ️ FYI

**<Space Name>**
<item>
```

If nothing needs attention, say so clearly — don't manufacture urgency:

> "Nothing requiring your attention in the last <timeframe>. Your spaces are quiet."

---

## Step 5: Learn from feedback

After presenting the briefing, ask:

> "Anything here that shouldn't have been flagged? Or anything I should always
> flag in the future?"

Use feedback to update preferences (see below).

---

## Triage Rules

These rules govern how you decide what's relevant:

1. **Err on the side of over-informing.** When in doubt, include it. It's
   better to flag something the user can quickly skip than to miss something
   important.

2. **Don't include items the user has already responded to.** If they already
   answered a question or acknowledged something, it's not "blocked."

3. **Be specific.** Include names, timestamps, and quote key phrases. Don't
   summarize away the details that help the user decide what to do.

4. **Draft responses should be ready to send.** Match the tone of the space
   (casual for team chats, more formal for cross-org). Be concise and direct.

5. **Prioritize within each section.** Most urgent or important items first.

6. **Only suppress items explicitly marked irrelevant in preferences.** Your
   default should be to include, not exclude.

---

## Trainable Preferences

The user can teach you what matters to them. Maintain a mental model of their
relevance rules across these dimensions:

### Role & Focus
What the user is responsible for. This helps you judge whether a discussion is
relevant even if they aren't mentioned by name.

Examples:
- "I lead the auth engineering team"
- "I'm responsible for the CII integration project"
- "I'm a PM for Duo security products"

### Always Relevant
Topics, projects, or keywords that should always be flagged regardless of
which space they appear in.

Examples:
- "Always flag anything about the migration project"
- "Flag any discussion of API breaking changes"

### Never Relevant
Topics or patterns to suppress. Only skip these — everything else gets
through.

Examples:
- "Ignore general help desk chatter unless I'm mentioned by name"
- "Skip social/watercooler channels"

### Space-Specific Rules
Override the default logic for specific spaces.

Examples:
- "In the All-Hands space, only flag me if I'm directly mentioned"
- "In the Security Alerts space, always include everything"

If your MCP server has `get_preferences` and `update_preferences` tools, use
those to persist rules. Otherwise, tell the user to save preferences in a
`preferences.md` file that you'll reference in future sessions.

---

## Adaptation Notes

This skill is designed to work with any Webex MCP server. The minimum tools
needed are:

- `list_spaces` — enumerate the user's spaces
- `get_messages` — fetch messages from a space with a time filter

Optional tools that improve the experience:
- `get_space_details` — for member counts (helps with space selection logic)
- `search_messages` — for keyword-based lookups
- `send_message` — so the user can send draft responses directly
- `get_preferences` / `update_preferences` — for persisting relevance rules

If your MCP server uses different tool names, adapt accordingly — the logic
is the same regardless of the tool interface.
