#!/usr/bin/env python3
"""MCP server exposing Webex API tools for Claude Code."""

import os
import sys

# Add parent directory so we can import webex_client
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime, timezone, timedelta
from mcp.server.fastmcp import FastMCP
from webex_client import WebexClient

mcp = FastMCP("webex")

_client = None


def get_client() -> WebexClient:
    global _client
    if _client is None:
        # Try OAuth token first, fall back to env var
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
        _client = WebexClient(token)
    return _client


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


if __name__ == "__main__":
    mcp.run()
