#!/usr/bin/env python3
"""Daily Webex summary — fetches recent messages, triages with Claude, and delivers via Webex or email."""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

import anthropic
from webex_client import WebexClient

# Add servers dir for oauth import
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "servers"))
from oauth import get_valid_token

LAST_RUN_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".last_run")
KNOWN_SPACES_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".known_spaces.json")
WATCHED_THREADS_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".watched_threads.json")
DEFAULT_LOOKBACK_H = 12
WATCHED_THREAD_STALE_DAYS = 28


def get_lookback_time() -> datetime:
    """Get the start time for this run: either last run time, SUMMARY_LOOKBACK_H, or default."""
    # Explicit override takes priority (for manual/agent calls)
    override = os.environ.get("SUMMARY_LOOKBACK_H")
    if override:
        return datetime.now(timezone.utc) - timedelta(hours=int(override))

    # Otherwise, use last run timestamp
    if os.path.exists(LAST_RUN_FILE):
        try:
            with open(LAST_RUN_FILE) as f:
                ts = f.read().strip()
            return datetime.fromisoformat(ts)
        except (ValueError, OSError):
            pass

    # Fallback
    return datetime.now(timezone.utc) - timedelta(hours=DEFAULT_LOOKBACK_H)


def save_run_timestamp():
    """Record when this run started."""
    with open(LAST_RUN_FILE, "w") as f:
        f.write(datetime.now(timezone.utc).isoformat())


def load_known_spaces() -> dict[str, str]:
    """Load previously seen space IDs → titles."""
    if os.path.exists(KNOWN_SPACES_FILE):
        try:
            with open(KNOWN_SPACES_FILE) as f:
                return json.load(f)
        except (ValueError, OSError):
            pass
    return {}


def save_known_spaces(spaces: dict[str, str]):
    """Persist current set of known space IDs → titles."""
    with open(KNOWN_SPACES_FILE, "w") as f:
        json.dump(spaces, f, indent=2)


def load_watched_threads() -> dict:
    """Load watched threads. Structure: {parent_msg_id: {space_id, space_title, last_activity, added}}"""
    if os.path.exists(WATCHED_THREADS_FILE):
        try:
            with open(WATCHED_THREADS_FILE) as f:
                return json.load(f)
        except (ValueError, OSError):
            pass
    return {}


def save_watched_threads(threads: dict):
    """Persist watched threads."""
    with open(WATCHED_THREADS_FILE, "w") as f:
        json.dump(threads, f, indent=2)


def prune_watched_threads(threads: dict) -> dict:
    """Remove threads with no activity in WATCHED_THREAD_STALE_DAYS days."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=WATCHED_THREAD_STALE_DAYS)).isoformat()
    return {
        tid: info for tid, info in threads.items()
        if info.get("last_activity", "") >= cutoff
    }


def update_watched_threads_from_messages(messages: list[dict], user_email: str, space_id: str, space_title: str, watched: dict) -> dict:
    """Scan messages for threaded replies by the user and add those threads to the watch list."""
    for msg in messages:
        parent_id = msg.get("parentId")
        sender = msg.get("personEmail", "")
        if parent_id and sender.lower() == user_email.lower():
            # User posted in this thread — watch it
            msg_time = msg.get("created", "")
            if parent_id not in watched:
                watched[parent_id] = {
                    "space_id": space_id,
                    "space_title": space_title,
                    "last_activity": msg_time,
                    "added": msg_time,
                }
            else:
                # Update last_activity if this message is newer
                if msg_time > watched[parent_id].get("last_activity", ""):
                    watched[parent_id]["last_activity"] = msg_time
    return watched


def get_watched_thread_spaces(watched: dict, lookback: datetime) -> dict[str, list[str]]:
    """Get space_id → [parent_ids] for watched threads that might have new activity."""
    space_threads: dict[str, list[str]] = {}
    for parent_id, info in watched.items():
        space_threads.setdefault(info["space_id"], []).append(parent_id)
    return space_threads


def detect_new_spaces(relevant_spaces: list[dict], known: dict[str, str]) -> list[dict]:
    """Find spaces in the relevant set that we haven't seen before."""
    return [s for s in relevant_spaces if s["id"] not in known]


def get_webex_client() -> WebexClient:
    client_id = os.environ.get("WEBEX_CLIENT_ID", "")
    client_secret = os.environ.get("WEBEX_CLIENT_SECRET", "")
    token = ""
    if client_id and client_secret:
        token = get_valid_token(client_id, client_secret)
    if not token:
        token = os.environ.get("WEBEX_ACCESS_TOKEN", "")
    if not token:
        print("No valid Webex token available.", file=sys.stderr)
        sys.exit(1)
    return WebexClient(token)


def get_claude_client() -> anthropic.Anthropic:
    """Create Claude client — supports both direct API and Bedrock."""
    if os.environ.get("CLAUDE_CODE_USE_BEDROCK") == "true":
        return anthropic.AnthropicBedrock(
            aws_profile=os.environ.get("AWS_PROFILE", "default"),
        )
    return anthropic.Anthropic()


def load_preferences() -> str:
    """Load user preferences for triage relevance filtering."""
    prefs_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "preferences.md")
    if os.path.exists(prefs_path):
        with open(prefs_path) as f:
            return f.read()
    return ""


def triage_with_claude(client, transcript: str, space_name: str, user_email: str) -> str:
    """Analyze a conversation and triage into actionable categories with draft responses."""
    model = "us.anthropic.claude-sonnet-4-20250514-v1:0" if os.environ.get("CLAUDE_CODE_USE_BEDROCK") == "true" else "claude-sonnet-4-6-20250514"
    preferences = load_preferences()
    prefs_block = f"\n\nUSER PREFERENCES (use these to judge relevance):\n{preferences}" if preferences else ""

    response = client.messages.create(
        model=model,
        max_tokens=3000,
        messages=[{
            "role": "user",
            "content": f"""You are a chief-of-staff creating an actionable briefing for {user_email}.
{prefs_block}

Analyze this Webex conversation and categorize into EXACTLY these sections. Only include sections that have content — omit empty sections entirely.

### Blocked on you
Someone ELSE has explicitly asked you a question, requested your input/approval, or is waiting for something you committed to deliver — AND you have not yet responded or delivered.

STRICT CRITERIA (all must be true):
1. Another person made a clear request or asked a direct question TO the user
2. The user has NOT yet responded to that specific request
3. The other person cannot reasonably proceed without the user's input

DO NOT include:
- Items where the user sent the last message (those go in "Waiting on others")
- Items where the user ASKED a question to someone else (that's the user waiting, not blocked)
- Vague "you might want to follow up" situations with no explicit ask
- Bot messages or automated notifications
- Items where the user offered help but the other person hasn't responded with what they need (that's waiting, not blocked)

For each item: who is waiting, what specifically they asked for, how long they've been waiting, and a **draft reply** (in a quoted block) that's concise and ready to send.

### Waiting on others
The user sent the last message OR made a request and is waiting for someone else to respond. Brief reminder of what you're waiting for and from whom.
Do NOT include suggested replies here — you've already acted.

### Decisions made without you
Concrete decisions, conclusions, or direction changes that happened without the user's involvement but affect their work. Must be an actual decision (not just a discussion or FYI).
For each: what was decided, by whom, and whether you need to weigh in or just be aware.

### Opportunities to add value
Discussions where the user's specific expertise would CHANGE THE OUTCOME — not just where they could chime in. The bar is: would this discussion go meaningfully differently with the user's input?

Only include if:
- The topic directly overlaps with the user's stated responsibilities
- There's a knowledge gap the user can uniquely fill
- A decision is being made that the user has context others don't

Do NOT include:
- General discussions the user might find interesting (those are FYI)
- Threads where awareness is sufficient
- Conversations that are proceeding fine without intervention

For each: what's being discussed, what specific knowledge/context the user has that others don't, and a **draft message** (in a quoted block).

### FYI
Important context or updates — no action needed, but useful to know. Keep each item to 1-2 lines.

RULES:
- **DIRECTIONALITY IS CRITICAL.** Messages marked "**YOU ({user_email})**" were sent BY the user.
  - If the user sent the LAST message in a thread/topic → "Waiting on others" (not "Blocked on you")
  - If the user's last message commits them to a future action (e.g., "I'll get back to you", "Let me look into that") → "Blocked on you" ONLY if there's a clear deliverable they haven't completed
  - If the user offered help and the other person hasn't responded → "Waiting on others"
  - Read message content carefully — understand obligations, not just sequence
- **NO DUPLICATES.** Each item appears in exactly ONE section. Pick the most appropriate one.
- **NO BOT MESSAGES.** Automated messages, notifications, and bot posts are never "Blocked on you." At most they're FYI.
- Be specific — include names, timestamps, and quote key phrases
- Draft replies should match the tone of the space (casual for DMs/small groups, structured for channels)
- Do NOT include the space name in your output — it will be added automatically
- Prioritize within each section (most urgent first)
- Only skip topics the user has explicitly marked as irrelevant in preferences
- If nothing requires attention, respond with exactly: "No items requiring your attention."

Space: {space_name}

Transcript:
{transcript}""",
        }],
    )
    return response.content[0].text


def format_messages(messages: list[dict], user_email: str = "") -> str:
    lines = []
    for msg in reversed(messages):
        sender = msg.get("personEmail", "Unknown")
        timestamp = msg.get("created", "")[:16].replace("T", " ")
        text = msg.get("text", "[non-text content]")
        if user_email and sender.lower() == user_email.lower():
            lines.append(f"[{timestamp}] **YOU ({sender})**: {text}")
        else:
            lines.append(f"[{timestamp}] {sender}: {text}")
    return "\n".join(lines)


def deliver_webex(webex: WebexClient, summary: str, space_name: str):
    spaces = webex.list_spaces(max_results=200)
    matches = [s for s in spaces if space_name.lower() in s["title"].lower()]
    if not matches:
        print(f"Could not find delivery space '{space_name}'", file=sys.stderr)
        return
    room_id = matches[0]["id"]
    # Webex has a ~7000 char limit per message — split by section if too long
    max_len = 6000
    if len(summary) <= max_len:
        webex.send_message(room_id, summary, markdown=summary)
    else:
        sections = summary.split("\n\n---\n\n")
        for i, section in enumerate(sections):
            chunk = section if i == 0 else f"*(continued)*\n\n{section}"
            if len(chunk) > max_len:
                chunk = chunk[:max_len] + "\n\n*(truncated)*"
            webex.send_message(room_id, chunk, markdown=chunk)
    print(f"Summary posted to '{matches[0]['title']}'")


def deliver_email(summary: str, to_addr: str, subject: str):
    import smtplib
    from email.mime.text import MIMEText

    smtp_host = os.environ.get("SMTP_HOST", "localhost")
    smtp_port = int(os.environ.get("SMTP_PORT", "25"))
    smtp_user = os.environ.get("SMTP_USER", "")
    smtp_pass = os.environ.get("SMTP_PASS", "")
    from_addr = os.environ.get("SMTP_FROM", smtp_user or "webex-agent@localhost")

    msg = MIMEText(summary)
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = to_addr

    with smtplib.SMTP(smtp_host, smtp_port) as server:
        if smtp_port == 587:
            server.starttls()
        if smtp_user and smtp_pass:
            server.login(smtp_user, smtp_pass)
        server.send_message(msg)
    print(f"Summary emailed to {to_addr}")


import re

# Heuristic: Webex auto-names group chats as "Name Name, Name Name" (comma-separated full names)
_GROUP_CHAT_PATTERN = re.compile(r'^[A-Z]\w+(?: [A-Z]\w+)+(, [A-Z]\w+(?: [A-Z]\w+)+)+$')


def _is_group_chat(space: dict) -> bool:
    """Determine if a space is a small group chat vs a channel.

    Uses title pattern: group chats auto-named by Webex look like
    "Di Yin Lu, Adam Greer" (comma-separated full names with a comma).
    """
    title = space.get("title", "")
    return bool(_GROUP_CHAT_PATTERN.match(title))


def _parse_space_lists(preferences: str) -> tuple[set[str], set[str]]:
    """Parse 'Always Scan' and 'Never Scan' space names from preferences.md."""
    always_scan = set()
    never_scan = set()
    current_section = None

    for line in preferences.split("\n"):
        stripped = line.strip()
        lower = stripped.lower()

        # Detect sections
        if lower.startswith("## always scan"):
            current_section = "always"
            continue
        elif lower.startswith("## never scan"):
            current_section = "never"
            continue
        elif stripped.startswith("## "):
            current_section = None
            continue

        # Skip comments, sub-headers, and blank lines
        if not stripped or stripped.startswith("<!--") or stripped.startswith("###"):
            continue

        # Parse list items
        if stripped.startswith("- ") and current_section:
            name = stripped[2:].strip()
            if name:
                if current_section == "always":
                    always_scan.add(name.lower())
                elif current_section == "never":
                    never_scan.add(name.lower())

    return always_scan, never_scan


def find_my_relevant_spaces(webex: WebexClient, lookback: datetime, max_spaces: int = 20, watched_threads: dict = None) -> list[dict]:
    """Find relevant spaces using an optimized approach:

    1. Fetch room list (sorted by lastActivity) — single API call
    2. Filter client-side by lastActivity timestamp — zero API calls for stale spaces
    3. For active spaces:
       - DMs/group chats/Always Scan: include directly (activity confirmed by timestamp)
       - Other channels: check mentionedPeople=me OR watched threads with new replies
    4. Also check for newly-created spaces (catches new DMs/groups)
    """
    preferences = load_preferences()
    always_scan, never_scan = _parse_space_lists(preferences)
    debug = os.environ.get("SUMMARY_DEBUG")
    watched_space_threads = get_watched_thread_spaces(watched_threads or {}, lookback) if watched_threads else {}
    lookback_utc = lookback.astimezone(timezone.utc) if lookback.tzinfo else lookback.replace(tzinfo=timezone.utc)

    # Single API call to get spaces sorted by last activity
    all_spaces = webex.list_spaces(max_results=200)

    # Also get recently-created spaces to catch new DMs/groups we were added to
    newly_created = webex.list_spaces_by_created(max_results=20)
    # Merge any new spaces not already in the main list
    seen_ids = {s["id"] for s in all_spaces}
    for s in newly_created:
        if s["id"] not in seen_ids:
            all_spaces.append(s)
            seen_ids.add(s["id"])

    # Client-side filter: skip spaces with no activity in lookback window
    active_spaces = []
    skipped_stale = 0
    for space in all_spaces:
        last_activity = space.get("lastActivity", "")
        if last_activity:
            activity_time = datetime.fromisoformat(last_activity.replace("Z", "+00:00"))
            if activity_time < lookback_utc:
                skipped_stale += 1
                continue
        active_spaces.append(space)

    if debug:
        print(f"    [debug] {len(all_spaces)} spaces fetched, {skipped_stale} skipped (stale), {len(active_spaces)} active")

    relevant_spaces = []
    total = len(active_spaces)
    for i, space in enumerate(active_spaces, 1):
        title = space.get("title", "")
        title_lower = title.lower()
        print(f"  Checking ({i}/{total}): {title[:40]}...", end="\r")

        # Never Scan: skip entirely
        if title_lower in never_scan:
            if debug:
                print(f"    [debug] skipping '{title[:40]}' — in Never Scan")
            continue

        space_type = space.get("type", "group")

        if space_type == "direct" or _is_group_chat(space) or title_lower in always_scan:
            # DMs, group chats, and Always Scan channels: include directly
            # (lastActivity already confirmed they have recent activity)
            relevant_spaces.append(space)
            if debug and title_lower in always_scan:
                print(f"    [debug] always-scan '{title[:40]}' — has activity")
        else:
            # Other channels: include if @mentioned OR if watched threads have new replies
            if _has_mentions_in_window(webex, space["id"], lookback_utc):
                relevant_spaces.append(space)
            elif _has_watched_thread_activity(webex, space["id"], lookback_utc, watched_space_threads, watched_threads):
                relevant_spaces.append(space)
                if debug:
                    print(f"    [debug] channel '{title[:40]}' — watched thread activity")
            elif debug:
                print(f"    [debug] channel '{title[:40]}' — no mentions or thread activity")

        if len(relevant_spaces) >= max_spaces:
            break

    print()  # Clear the progress line
    return relevant_spaces


def _has_mentions_in_window(webex: WebexClient, room_id: str, after_utc: datetime) -> bool:
    """Check if user was mentioned in a space within the lookback window. Single API call."""
    try:
        response = webex.client.get("/messages", params={
            "roomId": room_id, "mentionedPeople": "me", "max": 1
        })
        response.raise_for_status()
    except Exception:
        return False
    mentions = response.json().get("items", [])
    if not mentions:
        return False
    msg_time = datetime.fromisoformat(mentions[0]["created"].replace("Z", "+00:00"))
    return msg_time >= after_utc


def _has_watched_thread_activity(webex: WebexClient, space_id: str, after_utc: datetime, watched_space_threads: dict, watched_threads: dict = None) -> bool:
    """Check if any watched threads in this space have new messages since the lookback.
    Also updates last_activity on watched_threads if new replies found."""
    thread_ids = watched_space_threads.get(space_id, [])
    if not thread_ids:
        return False
    for parent_id in thread_ids:
        try:
            replies = webex.get_thread_messages(space_id, parent_id, after=after_utc, max_results=1)
            if replies:
                # Update last_activity so the stale timer resets
                if watched_threads and parent_id in watched_threads:
                    reply_time = replies[0].get("created", "")
                    if reply_time > watched_threads[parent_id].get("last_activity", ""):
                        watched_threads[parent_id]["last_activity"] = reply_time
                return True
        except Exception:
            continue
    return False


def main():
    after = get_lookback_time()
    lookback_desc = datetime.now(timezone.utc) - after
    lookback_hours = round(lookback_desc.total_seconds() / 3600, 1)

    delivery = os.environ.get("SUMMARY_DELIVERY", "webex")
    delivery_space = os.environ.get("SUMMARY_WEBEX_SPACE", "")
    delivery_email = os.environ.get("SUMMARY_EMAIL_TO", "")
    user_email = os.environ.get("SUMMARY_USER_EMAIL", "")

    webex = get_webex_client()
    claude = get_claude_client()

    # Auto-detect user email from Webex API if not configured
    if not user_email:
        try:
            me = webex.get_me()
            user_email = me.get("emails", [""])[0]
            if user_email:
                print(f"  Auto-detected email: {user_email}")
        except Exception:
            pass
    if not user_email:
        print("Warning: SUMMARY_USER_EMAIL not set and auto-detect failed. Triage directionality will be impaired.", file=sys.stderr)

    # Load and prune watched threads
    watched_threads = load_watched_threads()
    watched_threads = prune_watched_threads(watched_threads)

    print(f"Looking back {lookback_hours}h (since {after.strftime('%Y-%m-%d %H:%M UTC')})...")
    if watched_threads:
        print(f"  Watching {len(watched_threads)} active thread(s).")
    target_spaces = find_my_relevant_spaces(webex, lookback=after, max_spaces=20, watched_threads=watched_threads)

    if not target_spaces:
        print("No spaces with your recent activity found.")
        save_run_timestamp()
        return

    print(f"Found {len(target_spaces)} relevant spaces. Triaging...")

    # Detect newly-appeared spaces (not in our known set from previous runs)
    known_spaces = load_known_spaces()
    new_spaces = detect_new_spaces(target_spaces, known_spaces)
    if new_spaces:
        print(f"  {len(new_spaces)} new space(s) detected since last run.")

    # Update known spaces with everything we see now
    current_spaces = {s["id"]: s.get("title", "") for s in target_spaces}
    # Merge (keep old ones too — a space disappearing from one run doesn't mean it's gone)
    known_spaces.update(current_spaces)
    save_known_spaces(known_spaces)

    # Build set of new channel IDs (DMs don't need classification — always scanned)
    new_channel_ids = {s["id"] for s in new_spaces if s.get("type") != "direct" and not _is_group_chat(s)}

    # Triage each space
    blocked_parts = []
    waiting_parts = []
    decisions_parts = []
    opportunities_parts = []
    fyi_parts = []
    new_channel_triage_results = {}  # space_id → (title, triage_text, had_actionable_content)

    for space in target_spaces:
        messages = webex.get_messages(space["id"], after=after, max_results=200)
        if not messages:
            continue

        # Update watched threads: any threaded reply by the user gets tracked
        if user_email:
            watched_threads = update_watched_threads_from_messages(
                messages, user_email, space["id"], space.get("title", ""), watched_threads
            )

        print(f"  Analyzing '{space['title']}' ({len(messages)} messages)...")
        transcript = format_messages(messages, user_email)
        triage = triage_with_claude(claude, transcript, space["title"], user_email)

        # Track triage results for new channels (before skipping)
        if space["id"] in new_channel_ids:
            had_content = "no items requiring your attention" not in triage.lower()
            new_channel_triage_results[space["id"]] = (space["title"], triage, had_content)

        if "no items requiring your attention" in triage.lower():
            print(f"    -> Nothing relevant, skipping.")
            continue

        # Parse sections from the triage output and group across spaces
        current_section = None
        current_lines = []
        for line in triage.split("\n"):
            lower = line.lower().strip().replace("*", "").replace("#", "").strip()
            if "blocked on you" in lower:
                if current_section and current_lines:
                    _append_section(current_section, current_lines, space["title"],
                                    blocked_parts, waiting_parts, decisions_parts, opportunities_parts, fyi_parts)
                current_section = "blocked"
                current_lines = []
            elif "waiting on others" in lower:
                if current_section and current_lines:
                    _append_section(current_section, current_lines, space["title"],
                                    blocked_parts, waiting_parts, decisions_parts, opportunities_parts, fyi_parts)
                current_section = "waiting"
                current_lines = []
            elif "decisions made without you" in lower:
                if current_section and current_lines:
                    _append_section(current_section, current_lines, space["title"],
                                    blocked_parts, waiting_parts, decisions_parts, opportunities_parts, fyi_parts)
                current_section = "decisions"
                current_lines = []
            elif "opportunities to add value" in lower:
                if current_section and current_lines:
                    _append_section(current_section, current_lines, space["title"],
                                    blocked_parts, waiting_parts, decisions_parts, opportunities_parts, fyi_parts)
                current_section = "opportunities"
                current_lines = []
            elif lower.startswith("fyi"):
                if current_section and current_lines:
                    _append_section(current_section, current_lines, space["title"],
                                    blocked_parts, waiting_parts, decisions_parts, opportunities_parts, fyi_parts)
                current_section = "fyi"
                current_lines = []
            else:
                current_lines.append(line)
        if current_section and current_lines:
            _append_section(current_section, current_lines, space["title"],
                            blocked_parts, waiting_parts, decisions_parts, opportunities_parts, fyi_parts)

    if not any([blocked_parts, waiting_parts, decisions_parts, opportunities_parts, fyi_parts]) and not new_spaces:
        print("No actionable items found.")
        save_watched_threads(watched_threads)
        save_run_timestamp()
        return

    # Build the final digest grouped by priority
    now_str = datetime.now().strftime("%Y-%m-%d %I:%M %p")
    parts = [f"# Webex Briefing — {now_str}"]

    if blocked_parts:
        parts.append("## 🔴 Blocked on You\n" + "\n".join(blocked_parts))
    if waiting_parts:
        parts.append("## ⏳ Waiting on Others\n" + "\n".join(waiting_parts))
    if decisions_parts:
        parts.append("## 🟡 Decisions Made Without You\n" + "\n".join(decisions_parts))
    if opportunities_parts:
        parts.append("## 🟢 Opportunities to Add Value\n" + "\n".join(opportunities_parts))
    if fyi_parts:
        parts.append("## ℹ️ FYI\n" + "\n".join(fyi_parts))

    # New spaces section — with AI-suggested classifications for channels
    if new_spaces:
        new_space_lines = []

        # Separate DMs/group chats (no classification needed) from channels
        new_dms = [s for s in new_spaces if s.get("type") == "direct" or _is_group_chat(s)]
        new_channels = [s for s in new_spaces if s.get("type") != "direct" and not _is_group_chat(s)]

        if new_channels:
            new_space_lines.append("**New channels — consider adding to preferences:**\n")
            for s in new_channels:
                title = s.get("title", "Unknown")
                result = new_channel_triage_results.get(s["id"])
                if result:
                    _, triage_text, had_content = result
                    suggestion = _suggest_classification(title, triage_text, had_content)
                else:
                    suggestion = "Mentions Only (no messages in lookback window)"
                new_space_lines.append(f"- **{title}** → *{suggestion}*")

        if new_dms:
            new_space_lines.append("\n**New DMs/group chats (always scanned, no action needed):**\n")
            for s in new_dms:
                title = s.get("title", "Unknown")
                type_label = "DM" if s.get("type") == "direct" else "Group Chat"
                new_space_lines.append(f"- {title} ({type_label})")

        parts.append("## 🆕 New Spaces\n" + "\n".join(new_space_lines))

    full_summary = "\n\n---\n\n".join(parts)

    # Always save locally for other tools to read (morning-coffee, hub, etc.)
    local_output_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "output")
    os.makedirs(local_output_dir, exist_ok=True)
    today_str = datetime.now().strftime("%Y-%m-%d")
    local_path = os.path.join(local_output_dir, f"{today_str}-triage.md")
    with open(local_path, "w") as f:
        f.write(full_summary)

    # Deliver
    if delivery in ("webex", "both") and delivery_space:
        deliver_webex(webex, full_summary, delivery_space)
    if delivery in ("email", "both") and delivery_email:
        deliver_email(full_summary, delivery_email, f"Webex Briefing — {now_str}")
    if not delivery_space and not delivery_email:
        print("No delivery target configured — printing to stdout:\n")
        print(full_summary)

    # Persist watched threads (updated during message scanning)
    save_watched_threads(watched_threads)
    save_run_timestamp()


def _suggest_classification(title: str, triage_text: str, had_actionable_content: bool) -> str:
    """Generate a classification suggestion based on triage results.

    Returns a human-readable suggestion like 'Always Scan P2 (active project discussion)'
    """
    title_lower = title.lower()

    # If the triage found actionable items (blocked, decisions, opportunities), suggest Always Scan
    if had_actionable_content:
        triage_lower = triage_text.lower()
        has_blocked = "blocked on you" in triage_lower and "no items" not in triage_lower.split("blocked on you")[1][:100]
        has_decisions = "decisions made without you" in triage_lower

        if has_blocked or has_decisions:
            # High relevance — suggest P1 or P2
            if "proj-" in title_lower or "squad" in title_lower:
                return "Suggest: Always Scan P1 (project space with items requiring your attention)"
            return "Suggest: Always Scan P2 (had actionable items for you)"
        else:
            # Some content but lower urgency
            return "Suggest: Always Scan P3 (relevant discussion, awareness-level)"

    # No actionable content found
    if "help-" in title_lower or "ask " in title_lower.lower():
        return "Suggest: Mentions Only (support/help channel — only flag if you're @mentioned)"
    if "proj-" in title_lower:
        return "Suggest: Always Scan P3 (project space, quiet this run but likely relevant)"

    return "Suggest: Mentions Only (no actionable content this run)"


_EMPTY_SECTION_PHRASES = [
    "nothing in this conversation",
    "no items",
    "no action needed",
    "nothing is currently blocking",
    "nothing requiring your attention",
    "none identified",
    "n/a",
]


def _append_section(section: str, lines: list[str], space_title: str,
                    blocked: list, waiting: list, decisions: list, opportunities: list, fyi: list):
    """Append parsed section content to the appropriate list."""
    content = "\n".join(lines).strip()
    if not content:
        return
    # Skip sections where the model said "nothing here"
    content_lower = content.lower()
    if any(phrase in content_lower for phrase in _EMPTY_SECTION_PHRASES):
        return
    entry = f"**{space_title}**\n{content}\n"
    if section == "blocked":
        blocked.append(entry)
    elif section == "waiting":
        waiting.append(entry)
    elif section == "decisions":
        decisions.append(entry)
    elif section == "opportunities":
        opportunities.append(entry)
    elif section == "fyi":
        fyi.append(entry)


if __name__ == "__main__":
    main()
