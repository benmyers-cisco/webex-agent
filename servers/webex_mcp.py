#!/usr/bin/env python3
"""MCP server exposing Webex API tools for Claude Code."""

import os
import sys
import logging
import re

# Add parent directory so we can import webex_client
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime, timezone, timedelta
from mcp.server.fastmcp import FastMCP
from webex_client import WebexClient

mcp = FastMCP("webex")
logger = logging.getLogger(__name__)

_client = None
_token_refreshed_at = None


def _get_fresh_token() -> str:
    """Get a valid token, refreshing via OAuth if possible."""
    from oauth import get_valid_token
    client_id = os.environ.get("WEBEX_CLIENT_ID", "")
    client_secret = os.environ.get("WEBEX_CLIENT_SECRET", "")
    token = ""
    if client_id and client_secret:
        token = get_valid_token(client_id, client_secret)
    if not token:
        token = os.environ.get("WEBEX_ACCESS_TOKEN", "")
    if not token:
        raise RuntimeError(
            "No valid Webex token. Set WEBEX_CLIENT_ID + WEBEX_CLIENT_SECRET "
            "(OAuth) or WEBEX_ACCESS_TOKEN in your environment."
        )
    return token


def get_client(force_refresh: bool = False) -> WebexClient:
    """Get a WebexClient, refreshing the token if needed.

    On 401 errors, callers should call get_client(force_refresh=True) to retry.
    """
    global _client, _token_refreshed_at
    if _client is None or force_refresh:
        token = _get_fresh_token()
        _client = WebexClient(token)
        _token_refreshed_at = datetime.now(timezone.utc)
        if force_refresh:
            logger.info("Token refreshed successfully")
    return _client


def with_token_retry(fn):
    """Decorator that retries a function once with a fresh token on auth failure."""
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            error_str = str(e).lower()
            # Retry on 401 Unauthorized or token-related errors
            if "401" in error_str or "unauthorized" in error_str or "token" in error_str:
                logger.warning(f"Auth error in {fn.__name__}, refreshing token and retrying...")
                get_client(force_refresh=True)
                return fn(*args, **kwargs)
            raise
    wrapper.__name__ = fn.__name__
    wrapper.__doc__ = fn.__doc__
    return wrapper


def parse_timeframe(timeframe: str) -> datetime:
    """Parse a human-friendly timeframe like '7d', '2w', '24h', '3m' or ISO date."""
    now = datetime.now(timezone.utc)
    timeframe = timeframe.lower().strip()
    units = {"h": "hours", "d": "days", "w": "weeks", "m": "months"}
    for suffix, unit in units.items():
        if timeframe.endswith(suffix):
            try:
                value = int(timeframe[:-1])
            except ValueError:
                break
            if unit == "months":
                return now - timedelta(days=value * 30)
            return now - timedelta(**{unit: value})
    dt = datetime.fromisoformat(timeframe)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def format_messages(messages: list[dict]) -> str:
    """Format messages into a readable transcript."""
    lines = []
    for msg in reversed(messages):
        sender = msg.get("personEmail", "Unknown")
        timestamp = msg.get("created", "")[:16].replace("T", " ")
        text = msg.get("text", "[non-text content]")
        lines.append(f"[{timestamp}] {sender}: {text}")
    return "\n".join(lines)


@mcp.tool()
@with_token_retry
def list_spaces(
    max_results: int = 50,
    space_type: str = "",
) -> str:
    """List the user's Webex spaces and direct chats.

    Args:
        max_results: Maximum number of spaces to return (default 50)
        space_type: Filter by type: "direct" for DMs, "group" for group spaces, or empty for all
    """
    client = get_client()
    st = space_type if space_type in ("direct", "group") else None
    spaces = client.list_spaces(max_results=max_results, space_type=st)
    lines = []
    for i, s in enumerate(spaces, 1):
        last = s.get("lastActivity", "")[:16].replace("T", " ")
        lines.append(f"{i}. {s['title']} ({s.get('type', '?')}) - last active: {last}")
    return f"Found {len(spaces)} spaces:\n" + "\n".join(lines)


@mcp.tool()
@with_token_retry
def get_messages(
    space_name: str,
    after: str = "",
    before: str = "",
    max_messages: int = 200,
) -> str:
    """Fetch messages from a Webex space. Returns a formatted transcript.

    Args:
        space_name: Full or partial name of the Webex space to search for
        after: Only get messages after this time (e.g., "7d", "2w", "2024-01-15")
        before: Only get messages before this time (e.g., "1d", "2024-03-01")
        max_messages: Maximum number of messages to fetch (default 200)
    """
    client = get_client()
    space = _find_space(client, space_name)
    after_dt = parse_timeframe(after) if after else None
    before_dt = parse_timeframe(before) if before else None
    messages = client.get_messages(space["id"], before=before_dt, after=after_dt, max_results=max_messages)
    if not messages:
        return f"No messages found in '{space['title']}' for the specified timeframe."
    transcript = format_messages(messages)
    return f"Transcript from '{space['title']}' ({len(messages)} messages):\n\n{transcript}"


@mcp.tool()
@with_token_retry
def search_messages(
    space_name: str,
    query: str,
    max_messages: int = 200,
) -> str:
    """Search for messages containing a keyword in a Webex space.

    Args:
        space_name: Full or partial name of the Webex space
        query: Keyword or phrase to search for
        max_messages: Maximum messages to scan (default 200)
    """
    client = get_client()
    space = _find_space(client, space_name)
    matches = client.search_messages(space["id"], query, max_results=max_messages)
    if not matches:
        return f"No messages matching '{query}' found in '{space['title']}'."
    lines = []
    for msg in matches[:50]:
        sender = msg.get("personEmail", "Unknown")
        time = msg.get("created", "")[:16].replace("T", " ")
        text = msg.get("text", "")[:300]
        lines.append(f"[{time}] {sender}: {text}")
    return f"Found {len(matches)} messages matching '{query}' in '{space['title']}':\n\n" + "\n".join(lines)


@mcp.tool()
@with_token_retry
def get_space_details(space_name: str) -> str:
    """Get details about a specific Webex space.

    Args:
        space_name: Full or partial name of the Webex space
    """
    client = get_client()
    space = _find_space(client, space_name)
    details = client.get_space_details(space["id"])
    return (
        f"Space: {details.get('title', '?')}\n"
        f"Type: {details.get('type', '?')}\n"
        f"Created: {details.get('created', '?')[:10]}\n"
        f"Last Activity: {details.get('lastActivity', '?')[:16].replace('T', ' ')}\n"
        f"Creator: {details.get('creatorId', '?')}\n"
        f"Is Locked: {details.get('isLocked', False)}"
    )


@mcp.tool()
@with_token_retry
def send_message(
    space_name: str,
    text: str,
    markdown: str = "",
) -> str:
    """Send a message to a Webex space.

    Args:
        space_name: Full or partial name of the Webex space
        text: Plain text message to send
        markdown: Optional markdown-formatted version of the message
    """
    client = get_client()
    space = _find_space(client, space_name)
    client.send_message(space["id"], text, markdown=markdown)
    return f"Message sent to '{space['title']}'"


@mcp.tool()
@with_token_retry
def send_email(
    to: str,
    subject: str,
    body: str,
) -> str:
    """Send an email via SMTP.

    Args:
        to: Recipient email address
        subject: Email subject line
        body: Email body text
    """
    import smtplib
    from email.mime.text import MIMEText

    smtp_host = os.environ.get("SMTP_HOST", "localhost")
    smtp_port = int(os.environ.get("SMTP_PORT", "25"))
    smtp_user = os.environ.get("SMTP_USER", "")
    smtp_pass = os.environ.get("SMTP_PASS", "")
    from_addr = os.environ.get("SMTP_FROM", smtp_user or "webex-agent@localhost")

    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = to

    with smtplib.SMTP(smtp_host, smtp_port) as server:
        if smtp_port == 587:
            server.starttls()
        if smtp_user and smtp_pass:
            server.login(smtp_user, smtp_pass)
        server.send_message(msg)

    return f"Email sent to {to}"


@mcp.tool()
def get_preferences() -> str:
    """Read the current triage preferences that control what's flagged as relevant.

    Returns the contents of preferences.md which contains rules about
    what topics, spaces, and patterns are relevant or irrelevant to the user.
    """
    prefs_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "preferences.md")
    if os.path.exists(prefs_path):
        with open(prefs_path) as f:
            return f.read()
    return "No preferences file found."


@mcp.tool()
def update_preferences(section: str, action: str, rule: str) -> str:
    """Update triage preferences to train what's relevant to the user.

    Args:
        section: Which section to update: "role", "always_relevant", "never_relevant", or "space_rules"
        action: "add" to add a rule, "remove" to remove a rule
        rule: The rule text to add or remove (e.g., "Ignore general IT help desk chatter unless I'm mentioned by name")
    """
    prefs_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "preferences.md")
    if not os.path.exists(prefs_path):
        return "Preferences file not found."

    with open(prefs_path) as f:
        content = f.read()

    section_headers = {
        "role": "## My Role & Focus",
        "always_relevant": "## Always Relevant",
        "never_relevant": "## Never Relevant",
        "space_rules": "## Space-Specific Rules",
    }

    header = section_headers.get(section)
    if not header:
        return f"Unknown section '{section}'. Use: role, always_relevant, never_relevant, space_rules"

    if action == "add":
        # Find the section and append the rule after any existing content
        lines = content.split("\n")
        new_lines = []
        in_section = False
        added = False
        for line in lines:
            new_lines.append(line)
            if line.strip() == header:
                in_section = True
            elif in_section and not added:
                if line.startswith("## ") or (line.startswith("<!--") and not added):
                    # Skip comment lines, add before next section
                    if line.startswith("## "):
                        new_lines.insert(-1, f"- {rule}")
                        added = True
                elif line.strip() == "":
                    new_lines.append(f"- {rule}")
                    added = True
                    in_section = False
        if not added:
            new_lines.append(f"- {rule}")

        content = "\n".join(new_lines)

    elif action == "remove":
        lines = content.split("\n")
        content = "\n".join(l for l in lines if rule.lower() not in l.lower())

    with open(prefs_path, "w") as f:
        f.write(content)

    return f"Preferences updated: {action} '{rule}' in {section}"


@mcp.tool()
def search_knowledge(query: str = "") -> str:
    """Search the personal knowledge base built from Webex conversation insights.

    The knowledge base contains technical insights, domain knowledge, process learnings,
    and people insights extracted from weekly retrospectives.

    Args:
        query: Optional search term to filter results. If empty, returns the full knowledge base.
    """
    kb_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "knowledge.md")
    if not os.path.exists(kb_path):
        return "Knowledge base is empty. It gets populated by the weekly retrospective."
    with open(kb_path) as f:
        content = f.read()
    if not query:
        return content
    # Simple keyword filter — return sections containing the query
    lines = content.split("\n")
    matches = []
    context_buffer = []
    for line in lines:
        context_buffer.append(line)
        if len(context_buffer) > 5:
            context_buffer.pop(0)
        if query.lower() in line.lower():
            matches.extend(context_buffer)
            context_buffer = []
    if not matches:
        return f"No knowledge base entries matching '{query}'."
    return "\n".join(matches)


@mcp.tool()
@with_token_retry
def list_recordings(
    after: str = "30d",
    before: str = "",
    max_results: int = 20,
) -> str:
    """List the user's Webex meeting recordings.

    Only returns recordings the user owns (was the meeting host).

    Args:
        after: Start of date range (e.g., "7d", "30d", "2024-01-15"). Default: 30 days ago.
        before: End of date range (e.g., "1d", "2024-03-01"). Default: now.
        max_results: Maximum recordings to return (default 20)
    """
    client = get_client()
    from_dt = parse_timeframe(after)
    to_dt = parse_timeframe(before) if before else None
    recs = client.list_recordings(from_date=from_dt, to_date=to_dt, max_results=max_results)
    if not recs:
        return "No recordings found in that date range."
    lines = []
    for i, r in enumerate(recs, 1):
        date = r.get("timeRecorded", r.get("createTime", ""))[:10]
        dur_sec = r.get("durationSeconds", 0)
        dur = f"{dur_sec // 60}m {dur_sec % 60}s" if dur_sec else "?"
        lines.append(f"{i}. {r.get('topic', 'Untitled')} ({date}, {dur}) — ID: {r['id']}")
    return f"Found {len(recs)} recordings:\n" + "\n".join(lines)


@mcp.tool()
@with_token_retry
def download_recording(
    recording_id: str,
    output_dir: str = "",
    download_video: bool = True,
    download_transcript: bool = True,
) -> str:
    """Download a Webex recording's video and/or transcript to disk.

    Only works for recordings the user owns (was the meeting host).

    Args:
        recording_id: The Webex recording ID (from list_recordings)
        output_dir: Directory to save files to (default: ~/recordings/)
        download_video: Whether to download the video file (default: true)
        download_transcript: Whether to download the transcript (default: true)
    """
    client = get_client()
    if not output_dir:
        output_dir = os.path.expanduser("~/recordings")
    result = client.download_recording(
        recording_id,
        output_dir=output_dir,
        download_video=download_video,
        download_transcript=download_transcript,
    )
    details = result["details"]
    parts = [f"Recording: {details.get('topic', 'Untitled')}"]
    if result["video_path"]:
        parts.append(f"Video saved: {result['video_path']}")
    if result["transcript_path"]:
        parts.append(f"Transcript saved: {result['transcript_path']}")
    if not result["video_path"] and not result["transcript_path"]:
        parts.append("No download links available (you may not own this recording).")
    return "\n".join(parts)


def _find_space(client: WebexClient, space_name: str) -> dict:
    """Find a space by partial name match."""
    spaces = client.list_spaces(max_results=200)
    matches = [s for s in spaces if space_name.lower() in s["title"].lower()]
    if not matches:
        raise ValueError(f"No space found matching '{space_name}'. Try listing spaces first.")
    if len(matches) == 1:
        return matches[0]
    # Return the most recently active match
    return matches[0]


# --- Triage ---

# Heuristic: Webex auto-names group chats as "Name Name, Name Name"
_GROUP_CHAT_PATTERN = re.compile(r'^[A-Z]\w+(?: [A-Z]\w+)+(, [A-Z]\w+(?: [A-Z]\w+)+)+$')

TRIAGE_PROMPT = """You are a chief-of-staff creating an actionable briefing for {user_email}.

{prefs_block}

Analyze this Webex conversation and categorize into EXACTLY these sections. Only include sections that have content — omit empty sections entirely.

### Blocked on you
People waiting for your input, approval, or response. These are the highest priority — someone else cannot move forward until you act.
For each item: who is waiting, what they need, and a **suggested reply** you could send (in a quoted block).
IMPORTANT: Do NOT include items here where the user sent the last message and is waiting for a reply. Those belong in "Waiting on others."

### Waiting on others
Threads where you've sent a message or made a request and are waiting for someone else to respond. Brief reminder of what you're waiting for and from whom.
Do NOT include suggested replies here — you've already acted.

### Decisions made without you
Decisions, conclusions, or direction changes that happened in this conversation that affect your work. You weren't part of the decision but need to know about it.
For each: what was decided, by whom, and whether you need to weigh in.

### Opportunities to add value
Discussions where your expertise or perspective could meaningfully help, but nobody has asked you directly.
For each: what's being discussed, why your input matters, and a **suggested message** you could send (in a quoted block).

### FYI
Important context or updates — no action needed, but useful to know.

RULES:
- **DIRECTIONALITY IS CRITICAL.** Messages marked "**YOU ({user_email})**" were sent BY the user. Use these to determine who the ball is with:
  - If the user sent the LAST message in a thread/topic, the ball is USUALLY with the other person. Do NOT put this in "Blocked on you."
  - EXCEPTION: If the user's last message commits them to a future action (e.g., "I'll get back to you"), then the ball IS still with the user.
  - "Blocked on you" means someone ELSE needs something from the user AND the user has not yet delivered it.
- ERR ON THE SIDE OF OVER-INFORMING. When in doubt about whether something is relevant, include it.
- ALWAYS include the space name at the start of each item
- Be specific — include names, timestamps, and quote key phrases
- Draft responses should be concise, professional, and ready to send
- Don't include items where the user has already responded
- Prioritize within each section (most urgent first)

Space: {space_name}

Transcript:
{transcript}"""


def _load_preferences() -> str:
    prefs_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "preferences.md")
    if os.path.exists(prefs_path):
        with open(prefs_path) as f:
            return f.read()
    return ""


def _parse_space_lists(preferences: str) -> tuple[set, set]:
    """Parse 'Always Scan' and 'Never Scan' space names from preferences."""
    always_scan = set()
    never_scan = set()
    current_section = None
    for line in preferences.split("\n"):
        stripped = line.strip()
        lower = stripped.lower()
        if lower.startswith("## always scan"):
            current_section = "always"
            continue
        elif lower.startswith("## never scan"):
            current_section = "never"
            continue
        elif stripped.startswith("## "):
            current_section = None
            continue
        if not stripped or stripped.startswith("<!--") or stripped.startswith("###"):
            continue
        if stripped.startswith("- ") and current_section:
            name = stripped[2:].strip()
            if name:
                if current_section == "always":
                    always_scan.add(name.lower())
                elif current_section == "never":
                    never_scan.add(name.lower())
    return always_scan, never_scan


def _is_group_chat(space: dict) -> bool:
    title = space.get("title", "")
    return bool(_GROUP_CHAT_PATTERN.match(title))


def _format_messages_with_user(messages: list[dict], user_email: str) -> str:
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


def _triage_space_with_claude(transcript: str, space_name: str, user_email: str) -> str:
    """Triage a single space's transcript using Claude."""
    import anthropic

    preferences = _load_preferences()
    prefs_block = f"USER PREFERENCES (use these to judge relevance):\n{preferences}" if preferences else ""

    use_bedrock = os.environ.get("CLAUDE_CODE_USE_BEDROCK") == "true"
    if use_bedrock:
        client = anthropic.AnthropicBedrock(aws_profile=os.environ.get("AWS_PROFILE", "default"))
        model = "us.anthropic.claude-sonnet-4-20250514-v1:0"
    else:
        client = anthropic.Anthropic()
        model = "claude-sonnet-4-6-20250514"

    prompt = TRIAGE_PROMPT.format(
        user_email=user_email,
        prefs_block=prefs_block,
        space_name=space_name,
        transcript=transcript,
    )

    response = client.messages.create(
        model=model,
        max_tokens=3000,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text


def _parse_triage_sections(triage_text: str, space_title: str) -> dict:
    """Parse triage output into categorized sections."""
    sections = {"blocked": [], "waiting": [], "decisions": [], "opportunities": [], "fyi": []}
    current_section = None
    current_lines = []

    for line in triage_text.split("\n"):
        lower = line.lower().strip().replace("*", "").replace("#", "").strip()
        detected = None
        if "blocked on you" in lower:
            detected = "blocked"
        elif "waiting on others" in lower:
            detected = "waiting"
        elif "decisions made without you" in lower:
            detected = "decisions"
        elif "opportunities to add value" in lower:
            detected = "opportunities"
        elif lower.startswith("fyi"):
            detected = "fyi"

        if detected:
            if current_section and current_lines:
                content = "\n".join(current_lines).strip()
                if content:
                    sections[current_section].append(f"**{space_title}**\n{content}")
            current_section = detected
            current_lines = []
        else:
            current_lines.append(line)

    if current_section and current_lines:
        content = "\n".join(current_lines).strip()
        if content:
            sections[current_section].append(f"**{space_title}**\n{content}")

    return sections


@mcp.tool()
@with_token_retry
def triage(
    lookback: str = "12h",
    max_spaces: int = 15,
) -> str:
    """Run a full Webex triage — scan relevant spaces and categorize what needs attention.

    Returns a structured briefing with sections:
    - Blocked on you (highest priority — someone is waiting)
    - Waiting on others (you've acted, ball is with them)
    - Decisions made without you (need to review)
    - Opportunities to add value (proactive engagement)
    - FYI (context, no action needed)

    Uses preferences.md to determine which spaces to scan and what's relevant.

    Args:
        lookback: How far back to look (e.g., "12h", "1d", "3d"). Default: 12 hours.
        max_spaces: Maximum number of spaces to analyze (default 15)
    """
    client = get_client()
    user_email = os.environ.get("SUMMARY_USER_EMAIL", "benmyers@cisco.com")
    lookback_dt = parse_timeframe(lookback)
    lookback_utc = lookback_dt.astimezone(timezone.utc) if lookback_dt.tzinfo else lookback_dt.replace(tzinfo=timezone.utc)

    preferences = _load_preferences()
    always_scan, never_scan = _parse_space_lists(preferences)

    # Fetch all spaces and filter by activity window
    all_spaces = client.list_spaces(max_results=200)
    active_spaces = []
    for space in all_spaces:
        last_activity = space.get("lastActivity", "")
        if last_activity:
            activity_time = datetime.fromisoformat(last_activity.replace("Z", "+00:00"))
            if activity_time < lookback_utc:
                continue
        active_spaces.append(space)

    # Determine relevant spaces
    relevant_spaces = []
    for space in active_spaces:
        title = space.get("title", "")
        title_lower = title.lower()

        if title_lower in never_scan:
            continue

        space_type = space.get("type", "group")
        if space_type == "direct" or _is_group_chat(space) or title_lower in always_scan:
            relevant_spaces.append(space)
        # For other channels, we'd need to check mentions — skip for now to keep it fast
        # (the daily_summary.py script does this, but it's slow per-space)

        if len(relevant_spaces) >= max_spaces:
            break

    if not relevant_spaces:
        return "No spaces with recent activity found in the lookback window."

    # Triage each space
    all_sections = {"blocked": [], "waiting": [], "decisions": [], "opportunities": [], "fyi": []}
    skipped = []

    for space in relevant_spaces:
        messages = client.get_messages(space["id"], after=lookback_dt, max_results=200)
        if not messages or len(messages) < 2:
            continue

        transcript = _format_messages_with_user(messages, user_email)
        try:
            triage_text = _triage_space_with_claude(transcript, space["title"], user_email)
        except Exception as e:
            skipped.append(f"{space['title']}: {e}")
            continue

        if "no items requiring your attention" in triage_text.lower():
            continue

        sections = _parse_triage_sections(triage_text, space["title"])
        for key in all_sections:
            all_sections[key].extend(sections[key])

    # Build output
    if not any(all_sections.values()):
        return "No actionable items found across your Webex spaces."

    now_str = datetime.now().strftime("%Y-%m-%d %I:%M %p")
    parts = [f"# Webex Triage — {now_str}\n*Looked back {lookback}, scanned {len(relevant_spaces)} spaces*"]

    if all_sections["blocked"]:
        parts.append("## Blocked on You\n" + "\n\n".join(all_sections["blocked"]))
    if all_sections["waiting"]:
        parts.append("## Waiting on Others\n" + "\n\n".join(all_sections["waiting"]))
    if all_sections["decisions"]:
        parts.append("## Decisions Made Without You\n" + "\n\n".join(all_sections["decisions"]))
    if all_sections["opportunities"]:
        parts.append("## Opportunities to Add Value\n" + "\n\n".join(all_sections["opportunities"]))
    if all_sections["fyi"]:
        parts.append("## FYI\n" + "\n\n".join(all_sections["fyi"]))

    if skipped:
        parts.append("## Errors\n" + "\n".join(f"- {s}" for s in skipped))

    result = "\n\n---\n\n".join(parts)

    # Save locally for other tools to read
    output_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "output")
    os.makedirs(output_dir, exist_ok=True)
    today_str = datetime.now().strftime("%Y-%m-%d")
    output_path = os.path.join(output_dir, f"{today_str}-triage.md")
    with open(output_path, "w") as f:
        f.write(result)

    return result


if __name__ == "__main__":
    mcp.run()
