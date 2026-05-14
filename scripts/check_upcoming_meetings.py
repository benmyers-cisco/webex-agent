#!/usr/bin/env python3
"""Check for upcoming Webex meetings and match attendees to tracking files.

Outputs JSON for use as a Claude Code SessionStart hook.
If a meeting is starting within WINDOW_MINUTES and an attendee has a
tracking file in the meetings directory, outputs hookSpecificOutput
with the file contents as additional context.

When run with the webex-agent venv, also pulls recent Webex conversation
history with matched people and summarizes via Claude (Bedrock).

Exit codes:
  0 = always (hook output on stdout, empty {} if no match)
"""

import json
import os
import re
import sys
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone

WINDOW_MINUTES = 10
HISTORY_LOOKBACK_DAYS = 14
MEETINGS_DIR = os.path.expanduser(
    "~/.claude/projects/-Users-benmyers/memory/meetings"
)
NOTES_DIR = os.path.expanduser(
    "~/.claude/projects/-Users-benmyers/memory/notes"
)
TOKEN_FILE = os.path.expanduser("~/Projects/webex-agent/.webex_token.json")
SLACK_ENV_FILE = os.path.expanduser("~/Projects/claude-remote-slack/.env")
SLACK_USER_ID = os.environ.get("SLACK_USER_ID", "U0ATG4ZAHPE")
WEBEX_AGENT_DIR = os.path.expanduser("~/Projects/webex-agent")

# Try to import webex-agent dependencies (available when run from its venv)
_HAS_WEBEX_CLIENT = False
try:
    sys.path.insert(0, WEBEX_AGENT_DIR)
    sys.path.insert(0, os.path.join(WEBEX_AGENT_DIR, "servers"))
    from webex_client import WebexClient
    from oauth import get_valid_token
    import anthropic
    _HAS_WEBEX_CLIENT = True
except ImportError:
    pass


def load_token():
    if not os.path.exists(TOKEN_FILE):
        return None
    with open(TOKEN_FILE) as f:
        return json.load(f).get("access_token")


def get_upcoming_meetings(token):
    """Fetch meetings starting within the next WINDOW_MINUTES."""
    now = datetime.now(timezone.utc)
    from_time = now.isoformat(timespec="seconds").replace("+00:00", "Z")
    to_time = (now + timedelta(minutes=WINDOW_MINUTES)).isoformat(
        timespec="seconds"
    ).replace("+00:00", "Z")

    url = (
        f"https://webexapis.com/v1/meetings"
        f"?meetingType=scheduledMeeting"
        f"&from={from_time}&to={to_time}"
        f"&max=10"
    )
    req = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {token}"}
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read()).get("items", [])


def get_meeting_invitees(token, meeting_id):
    """Fetch invitees for a specific meeting."""
    url = f"https://webexapis.com/v1/meetingInvitees?meetingId={meeting_id}&max=50"
    req = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {token}"}
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read()).get("items", [])
    except urllib.error.HTTPError:
        return []


def load_tracking_files():
    """Load all tracking files and extract email mappings."""
    files = {}
    if not os.path.isdir(MEETINGS_DIR):
        return files

    for fname in os.listdir(MEETINGS_DIR):
        if not fname.endswith(".md"):
            continue
        fpath = os.path.join(MEETINGS_DIR, fname)
        with open(fpath) as f:
            content = f.read()

        # Extract email from **Email:** line
        match = re.search(r"\*\*Email:\*\*\s*(\S+)", content)
        if match:
            email = match.group(1).lower()
            # Extract person's name from the # header line
            name_match = re.search(r"^#\s+(.+?)(?:\s+1:1)?$", content, re.MULTILINE)
            person_name = name_match.group(1).strip() if name_match else ""
            files[email] = {"filename": fname, "content": content, "person_name": person_name}

    return files


def load_slack_token():
    """Load Slack bot token from claude-remote-slack .env file."""
    if not os.path.exists(SLACK_ENV_FILE):
        return None
    with open(SLACK_ENV_FILE) as f:
        for line in f:
            line = line.strip()
            if line.startswith("SLACK_BOT_TOKEN="):
                return line.split("=", 1)[1].strip()
    return None


def get_webex_history(webex, email, lookback_days=HISTORY_LOOKBACK_DAYS):
    """Pull recent Webex DM history with a specific person."""
    after = datetime.now(timezone.utc) - timedelta(days=lookback_days)

    # Find the DM space with this person
    spaces = webex.list_spaces(max_results=200, space_type="direct")
    target_space = None
    for space in spaces:
        # DM space titles show the other person's display name, but we need
        # to check messages to match by email since Webex doesn't expose
        # the other party's email in the space object
        messages = webex.get_messages(space["id"], max_results=1)
        if not messages:
            continue
        # Check if any message in this DM is from the target email
        for msg in messages:
            if msg.get("personEmail", "").lower() == email:
                target_space = space
                break
        if target_space:
            break

    if not target_space:
        return None

    messages = webex.get_messages(target_space["id"], after=after, max_results=200)
    if not messages:
        return None

    # Format messages chronologically
    lines = []
    for msg in reversed(messages):
        sender = msg.get("personEmail", "Unknown")
        timestamp = msg.get("created", "")[:16].replace("T", " ")
        text = msg.get("text", "[non-text content]")
        lines.append(f"[{timestamp}] {sender}: {text}")

    return "\n".join(lines)


def summarize_history(transcript, person_email, meeting_title):
    """Summarize conversation history using Claude via Bedrock."""
    client = anthropic.AnthropicBedrock(
        aws_profile=os.environ.get("AWS_PROFILE", "claudecode"),
    )
    model = "us.anthropic.claude-sonnet-4-20250514-v1:0"

    response = client.messages.create(
        model=model,
        max_tokens=1000,
        messages=[{
            "role": "user",
            "content": f"""Summarize this Webex DM conversation history to help me prepare for an upcoming meeting ({meeting_title}) with {person_email}.

Focus on:
- Open threads or unresolved questions between us
- Things they're waiting on from me (or I'm waiting on from them)
- Key topics we've been discussing recently
- Any context that would be useful going into this meeting

Be concise — bullet points, not paragraphs. Skip greetings and small talk.
If the conversation is sparse or purely logistical, say so briefly.

FORMATTING: This will be displayed in Slack. Use Slack mrkdwn syntax:
- *bold* (single asterisks, NOT double)
- _italic_ (underscores)
- No headings — use *bold text* on its own line as section headers
- Bullet points with • or plain - (no nested indentation)
- `code` with single backticks
- No ## or **text** — those don't render in Slack

Transcript:
{transcript}""",
        }],
    )
    return response.content[0].text


def send_slack_dm(matched):
    """Send meeting context as a Slack DM."""
    slack_token = load_slack_token()
    if not slack_token:
        return

    # Open a DM channel with the user
    open_data = json.dumps({"users": SLACK_USER_ID}).encode()
    req = urllib.request.Request(
        "https://slack.com/api/conversations.open",
        data=open_data,
        headers={
            "Authorization": f"Bearer {slack_token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read())
        if not result.get("ok"):
            return
        channel_id = result["channel"]["id"]
    except (urllib.error.URLError, OSError, KeyError):
        return

    # Build Slack message with mrkdwn blocks
    for m in matched:
        # Convert prep notes from markdown to Slack mrkdwn
        slack_content = m["content"]
        slack_content = re.sub(r"^# (.+)$", r"*\1*", slack_content, flags=re.MULTILINE)
        slack_content = re.sub(r"^## (.+)$", r"*\1*", slack_content, flags=re.MULTILINE)
        slack_content = re.sub(r"\*\*(.+?)\*\*", r"*\1*", slack_content)
        slack_content = re.sub(r"_(.+?)_", r"_\1_", slack_content)

        parts = [
            f":calendar: *Upcoming: {m['meeting']}*",
            f"Starts: {m['start']}",
            "",
            slack_content,
        ]

        if m.get("history_summary"):
            parts.append("")
            parts.append("*Recent conversation context (14 days):*")
            parts.append(m["history_summary"])

        text = "\n".join(parts)
        msg_data = json.dumps({
            "channel": channel_id,
            "text": text,
            "unfurl_links": False,
        }).encode()
        req = urllib.request.Request(
            "https://slack.com/api/chat.postMessage",
            data=msg_data,
            headers={
                "Authorization": f"Bearer {slack_token}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=10):
                pass
        except (urllib.error.URLError, OSError):
            pass


def load_todays_notes():
    """Load notes from today's date in the notes directory."""
    today = datetime.now().strftime("%Y-%m-%d")
    notes = []
    if not os.path.isdir(NOTES_DIR):
        return notes

    for fname in os.listdir(NOTES_DIR):
        if not fname.startswith(today) or not fname.endswith(".md"):
            continue
        fpath = os.path.join(NOTES_DIR, fname)
        with open(fpath) as f:
            content = f.read()

        # Extract title from first line
        title = ""
        for line in content.splitlines():
            if line.startswith("# "):
                title = line[2:].strip()
                break

        notes.append({
            "filename": fname,
            "filepath": fpath,
            "title": title,
            "content": content,
        })

    return notes


def _note_matches_meeting(note_title, meeting_title):
    """Check if a note title fuzzy-matches a meeting subject.

    Tokenizes both into words, ignores common filler words, and checks
    if enough note words appear in the meeting title.
    """
    filler = {"the", "a", "an", "and", "or", "of", "for", "in", "on", "to",
              "with", "is", "at", "by", "from", "-", "—", "weekly", "biweekly",
              "monthly", "daily", "sync", "wkgrp", "meeting", "call"}
    note_words = {w.lower() for w in re.split(r'[\s\-—/]+', note_title) if len(w) > 1}
    note_words -= filler
    if not note_words:
        return False

    meeting_lower = meeting_title.lower()
    matches = sum(1 for w in note_words if w in meeting_lower)
    return matches >= len(note_words) * 0.5 and matches >= 2


def _send_notes_slack(matched_notes):
    """Send matched meeting notes as Slack DMs."""
    slack_token = load_slack_token()
    if not slack_token:
        return

    # Open DM channel
    open_data = json.dumps({"users": SLACK_USER_ID}).encode()
    req = urllib.request.Request(
        "https://slack.com/api/conversations.open",
        data=open_data,
        headers={
            "Authorization": f"Bearer {slack_token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read())
        if not result.get("ok"):
            return
        channel_id = result["channel"]["id"]
    except (urllib.error.URLError, OSError, KeyError):
        return

    for m in matched_notes:
        # Convert markdown to Slack mrkdwn
        slack_content = m["content"]
        slack_content = re.sub(r"^# (.+)$", r"*\1*", slack_content, flags=re.MULTILINE)
        slack_content = re.sub(r"^## (.+)$", r"*\1*", slack_content, flags=re.MULTILINE)
        slack_content = re.sub(r"^### (.+)$", r"*\1*", slack_content, flags=re.MULTILINE)
        slack_content = re.sub(r"\*\*(.+?)\*\*", r"*\1*", slack_content)

        # Parse start time for display
        start_time = ""
        if m.get("start"):
            try:
                dt = datetime.fromisoformat(m["start"].replace("Z", "+00:00"))
                start_time = dt.strftime("%-I:%M %p")
            except (ValueError, AttributeError):
                start_time = m["start"]

        text = f":memo: *Meeting prep — {m['meeting']}* ({start_time})\n\n{slack_content}"

        msg_data = json.dumps({
            "channel": channel_id,
            "text": text,
            "unfurl_links": False,
        }).encode()
        req = urllib.request.Request(
            "https://slack.com/api/chat.postMessage",
            data=msg_data,
            headers={
                "Authorization": f"Bearer {slack_token}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=10):
                pass
        except (urllib.error.URLError, OSError):
            pass


def _meeting_title_matches_person(title, person_name):
    """Check if a meeting title likely refers to a 1:1 with this person.

    Matches if any part of the person's name (first or last) appears in the
    meeting title, case-insensitive. E.g., "Ben / Adam" matches "Adam Greer",
    "Greer 1:1" matches "Adam Greer".
    """
    title_lower = title.lower()
    name_parts = person_name.lower().split()
    return any(part in title_lower for part in name_parts if len(part) > 2)


def main():
    token = load_token()
    if not token:
        print("{}")
        return

    try:
        meetings = get_upcoming_meetings(token)
    except (urllib.error.URLError, urllib.error.HTTPError, OSError):
        print("{}")
        return

    if not meetings:
        print("{}")
        return

    tracking = load_tracking_files()
    if not tracking:
        print("{}")
        return

    # Set up WebexClient for history pull if available
    webex = None
    if _HAS_WEBEX_CLIENT:
        try:
            client_id = os.environ.get("WEBEX_CLIENT_ID", "")
            client_secret = os.environ.get("WEBEX_CLIENT_SECRET", "")
            webex_token = None
            if client_id and client_secret:
                webex_token = get_valid_token(client_id, client_secret)
            if not webex_token:
                webex_token = token
            webex = WebexClient(webex_token)
        except Exception:
            webex = None

    # Check each meeting's invitees against tracking files
    matched = []
    for meeting in meetings:
        meeting_title = meeting.get("title", "Untitled")
        meeting_start = meeting.get("start", "")
        meeting_id = meeting.get("id")

        if not meeting_id:
            continue

        invitees = get_meeting_invitees(token, meeting_id)
        invitee_emails = [
            inv.get("email", "").lower() for inv in invitees
        ]

        for email in invitee_emails:
            if email in tracking:
                info = tracking[email]

                # Only trigger if the meeting title contains the person's name
                # (indicates a 1:1 or small meeting with them, not a large group call)
                person_name = info.get("person_name", "")
                if person_name and not _meeting_title_matches_person(meeting_title, person_name):
                    continue

                entry = {
                    "meeting": meeting_title,
                    "start": meeting_start,
                    "email": email,
                    "file": info["filename"],
                    "content": info["content"],
                    "history_summary": None,
                }

                # Pull and summarize Webex history if available
                if webex:
                    try:
                        transcript = get_webex_history(webex, email)
                        if transcript:
                            entry["history_summary"] = summarize_history(
                                transcript, email, meeting_title
                            )
                    except Exception:
                        pass  # Graceful degradation — still send prep notes

                matched.append(entry)

    # Also check today's notes against upcoming meeting titles
    todays_notes = load_todays_notes()
    matched_notes = []
    for meeting in meetings:
        meeting_title = meeting.get("title", "Untitled")
        meeting_start = meeting.get("start", "")
        for note in todays_notes:
            if _note_matches_meeting(note["title"], meeting_title):
                matched_notes.append({
                    "meeting": meeting_title,
                    "start": meeting_start,
                    "file": note["filename"],
                    "filepath": note["filepath"],
                    "title": note["title"],
                    "content": note["content"],
                })

    # Send matched notes via Slack
    if matched_notes:
        _send_notes_slack(matched_notes)

    if not matched and not matched_notes:
        print("{}")
        return

    # Build context string
    parts = ["UPCOMING MEETING CONTEXT:"]
    for m in matched:
        parts.append(f"\nMeeting: {m['meeting']} (starts {m['start']})")
        parts.append(f"Tracking file: {m['file']}")
        parts.append(m["content"])
        if m.get("history_summary"):
            parts.append(f"\nRecent Webex conversation summary with {m['email']}:")
            parts.append(m["history_summary"])
        parts.append("---")

    for m in matched_notes:
        parts.append(f"\nMeeting: {m['meeting']} (starts {m['start']})")
        parts.append(f"Note: {m['file']}")
        parts.append(m["content"])
        parts.append("---")

    context = "\n".join(parts)

    # Send to Slack as DM
    send_slack_dm(matched)

    output = {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": context,
        }
    }
    print(json.dumps(output))


if __name__ == "__main__":
    main()
