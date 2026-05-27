#!/usr/bin/env python3
"""Weekly retrospective — analyzes the past week's Webex conversations for patterns,
commitments, relationship health, and learning opportunities."""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

import anthropic
from webex_client import WebexClient

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "servers"))
from oauth import get_valid_token

KNOWLEDGE_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "knowledge.md")


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


def get_claude_client():
    if os.environ.get("CLAUDE_CODE_USE_BEDROCK") == "true":
        return anthropic.AnthropicBedrock(
            aws_profile=os.environ.get("AWS_PROFILE", "default"),
        )
    return anthropic.Anthropic()


def get_model() -> str:
    if os.environ.get("CLAUDE_CODE_USE_BEDROCK") == "true":
        return "us.anthropic.claude-sonnet-4-20250514-v1:0"
    return "claude-sonnet-4-6-20250514"


def load_preferences() -> str:
    prefs_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "preferences.md")
    if os.path.exists(prefs_path):
        with open(prefs_path) as f:
            return f.read()
    return ""


def load_knowledge() -> str:
    if os.path.exists(KNOWLEDGE_FILE):
        with open(KNOWLEDGE_FILE) as f:
            return f.read()
    return ""


def save_knowledge(content: str):
    with open(KNOWLEDGE_FILE, "w") as f:
        f.write(content)


def format_messages(messages: list[dict]) -> str:
    lines = []
    for msg in reversed(messages):
        sender = msg.get("personEmail", "Unknown")
        timestamp = msg.get("created", "")[:16].replace("T", " ")
        text = msg.get("text", "[non-text content]")
        lines.append(f"[{timestamp}] {sender}: {text}")
    return "\n".join(lines)


def find_active_spaces(webex: WebexClient, after: datetime, max_spaces: int = 30) -> list[dict]:
    """Find spaces where user was active/mentioned in the lookback window."""
    all_spaces = webex.list_spaces(max_results=200)
    relevant = []
    total = len(all_spaces)
    for i, space in enumerate(all_spaces, 1):
        print(f"  Checking activity ({i}/{total}): {space['title'][:40]}...", end="\r")
        activity = webex.has_my_activity(space["id"], after=after)
        if activity["posted"] or activity["mentioned"]:
            space["_posted"] = activity["posted"]
            space["_mentioned"] = activity["mentioned"]
            relevant.append(space)
            if len(relevant) >= max_spaces:
                break
    print()
    return relevant


def build_week_transcript(webex: WebexClient, spaces: list[dict], after: datetime) -> dict[str, str]:
    """Fetch and format messages from each space for the week."""
    transcripts = {}
    for space in spaces:
        messages = webex.get_messages(space["id"], after=after, max_results=500)
        if messages:
            transcripts[space["title"]] = format_messages(messages)
    return transcripts


def generate_retrospective(claude, transcripts: dict[str, str], user_email: str, preferences: str) -> str:
    """Generate the weekly retrospective analysis."""
    # Combine transcripts with space labels
    combined = ""
    for space_name, transcript in transcripts.items():
        combined += f"\n\n=== SPACE: {space_name} ===\n{transcript}"

    # Truncate if too long (keep most recent)
    max_chars = 80000
    if len(combined) > max_chars:
        combined = combined[-max_chars:]

    prefs_block = f"\nUser preferences:\n{preferences}" if preferences else ""

    response = claude.messages.create(
        model=get_model(),
        max_tokens=4096,
        messages=[{
            "role": "user",
            "content": f"""You are a strategic advisor creating a weekly retrospective for {user_email}.
{prefs_block}

Analyze ALL the conversations below from this past week and produce a thoughtful retrospective. This is not a summary of messages — it's a reflection on patterns, effectiveness, and growth.

## Communication Patterns
- Where did you spend the most communication time this week? Was it aligned with your priorities?
- Which conversations were productive (led to decisions, unblocked work)?
- Which conversations went in circles or consumed time without clear outcomes?
- Are you spending time in the right spaces, or getting pulled into low-value discussions?

## Commitments Tracker
- What did you commit to this week? (explicit promises, action items you accepted)
- Are any commitments at risk or overdue?
- What did others commit to you that you should follow up on?

## Relationship Health
- Who did you collaborate with most this week?
- Who reached out to you that you haven't responded to?
- Are there key relationships you're neglecting? (people you usually engage with who went quiet, or vice versa)
- Any new people engaging with you that you should build a relationship with?

## Wins & Impact
- Where did you add the most value this week?
- What decisions were you part of that moved things forward?
- Any recognition or positive feedback from others?

## Watch List
- Emerging issues or risks you should keep an eye on
- Conversations that might need your attention next week
- Topics gaining momentum that could affect your work

## Reflection Questions
- End with 2-3 thought-provoking questions for the user to consider over the weekend

Be honest and specific. Use names and reference actual conversations. Don't manufacture positivity — if the week was chaotic, say so. The goal is self-awareness, not a pat on the back.

Conversations from this week:
{combined}""",
        }],
    )
    return response.content[0].text


def extract_learnings(claude, transcripts: dict[str, str], user_email: str, existing_knowledge: str) -> str:
    """Extract key technical and professional insights from the week's conversations."""
    combined = ""
    for space_name, transcript in transcripts.items():
        combined += f"\n\n=== SPACE: {space_name} ===\n{transcript}"

    max_chars = 80000
    if len(combined) > max_chars:
        combined = combined[-max_chars:]

    existing_block = f"\nExisting knowledge base (avoid duplicates):\n{existing_knowledge}" if existing_knowledge else ""

    response = claude.messages.create(
        model=get_model(),
        max_tokens=2048,
        messages=[{
            "role": "user",
            "content": f"""You are extracting key learnings for {user_email} from this week's Webex conversations.
{existing_block}

Look for:
- Technical insights: new tools, approaches, architectures, patterns discussed
- Domain knowledge: business context, product decisions, customer feedback, market insights
- Process learnings: what worked, what didn't, better ways of doing things
- People insights: who knows what, who's the expert on which topics

For each learning, provide:
- A concise title
- The key insight (1-3 sentences)
- Source: which space and roughly when
- Why it matters

Format as markdown list items. Only include genuinely useful insights — not obvious things or routine updates. If nothing notable was learned this week, say so.

DO NOT duplicate anything already in the existing knowledge base.

Conversations:
{combined}""",
        }],
    )
    return response.content[0].text


def deliver_webex(webex: WebexClient, content: str, space_name: str):
    spaces = webex.list_spaces(max_results=200)
    matches = [s for s in spaces if space_name.lower() in s["title"].lower()]
    if not matches:
        print(f"Could not find delivery space '{space_name}'", file=sys.stderr)
        return
    room_id = matches[0]["id"]
    max_len = 6000
    if len(content) <= max_len:
        webex.send_message(room_id, content, markdown=content)
    else:
        sections = content.split("\n\n---\n\n")
        for i, section in enumerate(sections):
            chunk = section if i == 0 else f"*(continued)*\n\n{section}"
            if len(chunk) > max_len:
                chunk = chunk[:max_len] + "\n\n*(truncated)*"
            webex.send_message(room_id, chunk, markdown=chunk)
    print(f"Posted to '{matches[0]['title']}'")


def main():
    lookback_days = int(os.environ.get("RETRO_LOOKBACK_DAYS", "7"))
    delivery_space = os.environ.get("SUMMARY_WEBEX_SPACE", "")
    user_email = os.environ.get("SUMMARY_USER_EMAIL", "")

    after = datetime.now(timezone.utc) - timedelta(days=lookback_days)
    preferences = load_preferences()
    existing_knowledge = load_knowledge()

    webex = get_webex_client()

    # Auto-detect user email if not configured
    if not user_email:
        try:
            me = webex.get_me()
            user_email = me.get("emails", [""])[0]
        except Exception:
            pass
    claude = get_claude_client()

    print(f"Building weekly retrospective (last {lookback_days} days)...")
    print("Finding your active spaces...")
    spaces = find_active_spaces(webex, after, max_spaces=30)

    if not spaces:
        print("No active spaces found for the week.")
        return

    print(f"Found {len(spaces)} active spaces. Fetching messages...")
    transcripts = build_week_transcript(webex, spaces, after)

    if not transcripts:
        print("No messages found for the week.")
        return

    total_msgs = sum(t.count("\n") + 1 for t in transcripts.values())
    print(f"Analyzing {total_msgs} messages across {len(transcripts)} spaces...")

    # Generate retrospective
    print("Generating weekly retrospective...")
    retro = generate_retrospective(claude, transcripts, user_email, preferences)

    # Extract learnings
    print("Extracting learnings...")
    new_learnings = extract_learnings(claude, transcripts, user_email, existing_knowledge)

    # Update knowledge base
    if new_learnings and "nothing notable" not in new_learnings.lower():
        week_str = datetime.now().strftime("%Y-%m-%d")
        updated_knowledge = existing_knowledge.rstrip()
        if updated_knowledge:
            updated_knowledge += f"\n\n---\n\n## Week of {week_str}\n\n{new_learnings}"
        else:
            updated_knowledge = f"# Personal Knowledge Base\n\nExtracted from Webex conversations.\n\n## Week of {week_str}\n\n{new_learnings}"
        save_knowledge(updated_knowledge)
        print(f"Knowledge base updated: {KNOWLEDGE_FILE}")

    # Build final output
    now_str = datetime.now().strftime("%Y-%m-%d %I:%M %p")
    full_output = f"# Weekly Retrospective — {now_str}\n\n{retro}"

    if new_learnings and "nothing notable" not in new_learnings.lower():
        full_output += f"\n\n---\n\n## This Week's Learnings\n\n{new_learnings}"

    # Deliver
    if delivery_space:
        deliver_webex(webex, full_output, delivery_space)
    else:
        print("No delivery target — printing to stdout:\n")
        print(full_output)


if __name__ == "__main__":
    main()
