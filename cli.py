#!/usr/bin/env python3
"""Webex Agent - Summarize and search your Webex spaces."""

import os
import sys
import click
import anthropic
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.markdown import Markdown

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "servers"))

from webex_client import WebexClient
from summarizer import summarize_messages, semantic_search, analyze_topic

load_dotenv()
console = Console()


def get_webex_token() -> str:
    """Get a valid Webex token via OAuth (preferred) or env var."""
    client_id = os.environ.get("WEBEX_CLIENT_ID", "")
    client_secret = os.environ.get("WEBEX_CLIENT_SECRET", "")
    if client_id and client_secret:
        from oauth import get_valid_token
        token = get_valid_token(client_id, client_secret)
        if token:
            return token
    token = os.environ.get("WEBEX_ACCESS_TOKEN", "")
    if token:
        return token
    console.print("[red]No valid Webex token. Run OAuth login or set WEBEX_ACCESS_TOKEN.[/red]")
    raise SystemExit(1)


def get_clients():
    webex_token = get_webex_token()
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
    if not anthropic_key:
        console.print("[red]ANTHROPIC_API_KEY not set. Add it to .env or export it.[/red]")
        raise SystemExit(1)
    return WebexClient(webex_token), anthropic.Anthropic(api_key=anthropic_key)


def parse_timeframe(timeframe: str) -> datetime:
    """Parse a human-friendly timeframe string into a datetime."""
    now = datetime.now(timezone.utc)
    timeframe = timeframe.lower().strip()

    # Handle relative times like "7d", "2w", "24h", "3m"
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

    # Try ISO date
    try:
        dt = datetime.fromisoformat(timeframe)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        pass

    console.print(f"[red]Cannot parse timeframe: {timeframe}[/red]")
    console.print("Use formats like: 7d, 2w, 24h, 3m, or 2024-01-15")
    raise SystemExit(1)


def pick_space(webex: WebexClient, space_name: str) -> dict:
    """Find a space by name (partial match)."""
    spaces = webex.list_spaces(max_results=200)
    matches = [s for s in spaces if space_name.lower() in s["title"].lower()]
    if not matches:
        console.print(f"[red]No space found matching '{space_name}'[/red]")
        raise SystemExit(1)
    if len(matches) == 1:
        return matches[0]
    # Show options for multiple matches
    table = Table(title="Multiple spaces found")
    table.add_column("#", style="cyan")
    table.add_column("Space Name")
    table.add_column("Type")
    for i, s in enumerate(matches[:20], 1):
        table.add_row(str(i), s["title"], s.get("type", ""))
    console.print(table)
    choice = click.prompt("Select a space number", type=int, default=1)
    return matches[choice - 1]


@click.group()
def cli():
    """Webex Agent - Summarize and search your Webex spaces."""
    pass


@cli.command()
@click.option("--limit", "-l", default=50, help="Max number of spaces to show")
@click.option("--type", "-t", "space_type", type=click.Choice(["direct", "group"]), help="Filter by space type")
@click.option("--mine", is_flag=True, help="Only show spaces I've posted in or been tagged in")
@click.option("--since", "-s", "since_str", help="Lookback for --mine filter (e.g., 7d, 2w, 3m)")
def spaces(limit, space_type, mine, since_str):
    """List your Webex spaces and direct chats.

    Examples:
        webex-agent spaces                          # All spaces and DMs
        webex-agent spaces --type group             # Only group spaces
        webex-agent spaces --type direct            # Only direct chats
        webex-agent spaces --mine                   # Spaces I've posted in or been tagged in
        webex-agent spaces --mine --since 7d        # Same, but only in the last 7 days
    """
    webex, _ = get_clients()

    since_dt = parse_timeframe(since_str) if since_str else None
    if since_str and not mine:
        console.print("[yellow]--since only applies with --mine. Adding --mine automatically.[/yellow]")
        mine = True

    with console.status("Fetching spaces..."):
        space_list = webex.list_spaces(max_results=limit, space_type=space_type)

    if mine:
        filtered = []
        total = len(space_list)
        for i, space in enumerate(space_list, 1):
            with console.status(f"Checking activity in spaces ({i}/{total})..."):
                activity = webex.has_my_activity(space["id"], after=since_dt)
                if activity["posted"] or activity["mentioned"]:
                    space["_posted"] = activity["posted"]
                    space["_mentioned"] = activity["mentioned"]
                    filtered.append(space)
        space_list = filtered

    title = f"Your Webex Spaces ({len(space_list)} shown)"
    if mine:
        title = f"Spaces You've Posted In or Been Tagged In ({len(space_list)} shown)"

    table = Table(title=title)
    table.add_column("#", style="cyan", width=4)
    table.add_column("Space Name", style="bold")
    table.add_column("Type", width=8)
    table.add_column("Last Activity", width=20)
    if mine:
        table.add_column("Your Activity", width=16)

    for i, s in enumerate(space_list, 1):
        last = s.get("lastActivity", "")[:16].replace("T", " ")
        row = [str(i), s["title"], s.get("type", ""), last]
        if mine:
            tags = []
            if s.get("_posted"):
                tags.append("posted")
            if s.get("_mentioned"):
                tags.append("tagged")
            row.append(", ".join(tags))
        table.add_row(*row)

    console.print(table)


@cli.command()
@click.argument("space_name")
@click.option("--after", "-a", "after_str", help="Start of timeframe (e.g., 7d, 2w, 2024-01-15)")
@click.option("--before", "-b", "before_str", help="End of timeframe (e.g., 1d, 2024-03-01)")
@click.option("--max-messages", "-m", default=500, help="Max messages to fetch")
def summarize(space_name, after_str, before_str, max_messages):
    """Summarize a Webex space conversation.

    SPACE_NAME: Full or partial name of the Webex space.

    Examples:
        webex-agent summarize "Project Alpha"
        webex-agent summarize "Team Chat" --after 7d
        webex-agent summarize "Design Review" --after 2024-01-01 --before 2024-02-01
    """
    webex, claude = get_clients()

    with console.status("Finding space..."):
        space = pick_space(webex, space_name)

    after_dt = parse_timeframe(after_str) if after_str else None
    before_dt = parse_timeframe(before_str) if before_str else None

    timeframe_desc = ""
    if after_dt:
        timeframe_desc += f" from {after_dt.strftime('%Y-%m-%d')}"
    if before_dt:
        timeframe_desc += f" to {before_dt.strftime('%Y-%m-%d')}"

    with console.status(f"Fetching messages from '{space['title']}'{timeframe_desc}..."):
        messages = webex.get_messages(space["id"], before=before_dt, after=after_dt, max_results=max_messages)

    console.print(f"\nFound [cyan]{len(messages)}[/cyan] messages in [bold]{space['title']}[/bold]{timeframe_desc}\n")

    if not messages:
        console.print("[yellow]No messages found.[/yellow]")
        return

    with console.status("Generating summary with Claude..."):
        summary = summarize_messages(claude, messages, space["title"])

    console.print(Panel(Markdown(summary), title=f"Summary: {space['title']}", border_style="green"))


@cli.command()
@click.argument("space_name")
@click.argument("topic")
@click.option("--after", "-a", "after_str", help="Start of timeframe (e.g., 7d, 2w, 2024-01-15)")
@click.option("--before", "-b", "before_str", help="End of timeframe (e.g., 1d, 2024-03-01)")
@click.option("--max-messages", "-m", default=500, help="Max messages to fetch")
def analyze(space_name, topic, after_str, before_str, max_messages):
    """Analyze a space for deep insights on a specific topic or concept.

    Unlike 'search' which finds matching messages, 'analyze' reads the full
    conversation and synthesizes a briefing — connecting ideas, tracking how
    thinking evolved, and identifying decisions, open questions, and related themes.

    SPACE_NAME: Full or partial name of the Webex space.
    TOPIC: The topic, concept, or question to analyze.

    Examples:
        webex-agent analyze "Security Team" "MFA rollout"
        webex-agent analyze "IT Ops" "migration to cloud" --after 30d
        webex-agent analyze "Product" "customer churn concerns" --after 2024-01-01
        webex-agent analyze "Duo Team" "SSO integration challenges" --after 2w
    """
    webex, claude = get_clients()

    with console.status("Finding space..."):
        space = pick_space(webex, space_name)

    after_dt = parse_timeframe(after_str) if after_str else None
    before_dt = parse_timeframe(before_str) if before_str else None

    timeframe_desc = ""
    if after_dt:
        timeframe_desc += f" from {after_dt.strftime('%Y-%m-%d')}"
    if before_dt:
        timeframe_desc += f" to {before_dt.strftime('%Y-%m-%d')}"

    with console.status(f"Fetching messages from '{space['title']}'{timeframe_desc}..."):
        messages = webex.get_messages(space["id"], before=before_dt, after=after_dt, max_results=max_messages)

    console.print(f"\nAnalyzing [cyan]{len(messages)}[/cyan] messages in [bold]{space['title']}[/bold]{timeframe_desc}")
    console.print(f"Topic: [bold magenta]{topic}[/bold magenta]\n")

    if not messages:
        console.print("[yellow]No messages found.[/yellow]")
        return

    with console.status("Analyzing conversation with Claude (this may take a moment)..."):
        analysis = analyze_topic(claude, messages, topic, space["title"])

    console.print(Panel(
        Markdown(analysis),
        title=f"Analysis: \"{topic}\" in {space['title']}",
        border_style="magenta",
    ))


@cli.command()
@click.argument("query")
@click.option("--spaces", "-s", "space_filter", help="Comma-separated space names to search (default: all)")
@click.option("--after", "-a", "after_str", help="Only search messages after this time (e.g., 7d, 2w)")
@click.option("--max-messages", "-m", default=200, help="Max messages per space to search")
@click.option("--keyword-only", "-k", is_flag=True, help="Use keyword matching only (no Claude)")
def search(query, space_filter, after_str, max_messages, keyword_only):
    """Search across your Webex spaces for topics, keywords, or concepts.

    QUERY: What to search for. Supports semantic search by default.

    Examples:
        webex-agent search "budget approval"
        webex-agent search "MFA rollout timeline" --spaces "Security Team,IT Ops"
        webex-agent search "deployment issues" --after 7d
        webex-agent search "kubernetes" --keyword-only
    """
    webex, claude = get_clients()
    after_dt = parse_timeframe(after_str) if after_str else None

    with console.status("Fetching spaces..."):
        all_spaces = webex.list_spaces(max_results=200)

    if space_filter:
        filter_names = [n.strip().lower() for n in space_filter.split(",")]
        all_spaces = [s for s in all_spaces if any(f in s["title"].lower() for f in filter_names)]

    if not all_spaces:
        console.print("[red]No matching spaces found.[/red]")
        return

    console.print(f"Searching [cyan]{len(all_spaces)}[/cyan] spaces for: [bold]{query}[/bold]\n")

    results_found = False
    for space in all_spaces:
        with console.status(f"Searching '{space['title']}'..."):
            if keyword_only:
                matches = webex.search_messages(space["id"], query, max_results=max_messages)
                if matches:
                    results_found = True
                    console.print(f"\n[bold green]{space['title']}[/bold green] - {len(matches)} keyword matches:")
                    for msg in matches[:10]:
                        sender = msg.get("personEmail", "Unknown")
                        time = msg.get("created", "")[:16].replace("T", " ")
                        text = msg.get("text", "")[:200]
                        console.print(f"  [{time}] {sender}: {text}")
            else:
                messages = webex.get_messages(space["id"], after=after_dt, max_results=max_messages)
                if not messages:
                    continue
                result = semantic_search(claude, messages, query, space["title"])
                if "nothing is relevant" not in result.lower() and "no relevant" not in result.lower():
                    results_found = True
                    console.print(Panel(
                        Markdown(result),
                        title=f"Results: {space['title']}",
                        border_style="blue",
                    ))

    if not results_found:
        console.print(f"\n[yellow]No results found for '{query}'.[/yellow]")


@cli.command()
@click.option("--after", "-a", "after_str", help="Start of date range (e.g., 7d, 30d, 2024-01-15)")
@click.option("--before", "-b", "before_str", help="End of date range")
@click.option("--limit", "-l", default=20, help="Max recordings to show")
def recordings(after_str, before_str, limit):
    """List your Webex recordings.

    Examples:
        webex-agent recordings                     # Last 30 days
        webex-agent recordings --after 7d          # Last 7 days
        webex-agent recordings --after 2024-03-01  # Since March 1
    """
    webex = WebexClient(get_webex_token())

    from_dt = parse_timeframe(after_str) if after_str else None
    to_dt = parse_timeframe(before_str) if before_str else None

    with console.status("Fetching recordings..."):
        recs = webex.list_recordings(from_date=from_dt, to_date=to_dt, max_results=limit)

    if not recs:
        console.print("[yellow]No recordings found.[/yellow]")
        return

    table = Table(title=f"Your Recordings ({len(recs)} found)", show_lines=False)
    table.add_column("#", style="cyan", width=3)
    table.add_column("Topic", style="bold", no_wrap=False, max_width=50)
    table.add_column("Date", width=10)
    table.add_column("Dur", width=8)
    table.add_column("ID", style="dim", overflow="fold")

    for i, r in enumerate(recs, 1):
        date = r.get("timeRecorded", r.get("createTime", ""))[:10]
        dur_sec = r.get("durationSeconds", 0)
        dur = f"{dur_sec // 60}m {dur_sec % 60}s" if dur_sec else "?"
        table.add_row(str(i), r.get("topic", "Untitled"), date, dur, r.get("id", ""))

    console.print(table)


@cli.command()
@click.argument("recording_id")
@click.option("--output", "-o", "output_dir", default=None, help="Output directory (default: ~/recordings/)")
@click.option("--video/--no-video", default=True, help="Download video file")
@click.option("--transcript/--no-transcript", default=True, help="Download transcript")
def download(recording_id, output_dir, video, transcript):
    """Download a Webex recording and/or transcript.

    RECORDING_ID: The recording ID (from `webex-agent recordings`) or a
    1-based index like '#3' to pick from recent recordings.

    Examples:
        webex-agent download <recording-id>
        webex-agent download '#1'                              # Download most recent
        webex-agent download <id> --no-video                   # Transcript only
        webex-agent download <id> -o ~/Projects/my-project/    # Custom output dir
    """
    webex = WebexClient(get_webex_token())

    # Support '#N' shorthand to pick from recent recordings
    if recording_id.startswith("#"):
        try:
            idx = int(recording_id[1:]) - 1
        except ValueError:
            console.print(f"[red]Invalid index: {recording_id}[/red]")
            raise SystemExit(1)
        with console.status("Fetching recent recordings..."):
            recs = webex.list_recordings(max_results=20)
        if idx < 0 or idx >= len(recs):
            console.print(f"[red]Index out of range. You have {len(recs)} recent recordings.[/red]")
            raise SystemExit(1)
        recording_id = recs[idx]["id"]
        console.print(f"Selected: [bold]{recs[idx].get('topic', 'Untitled')}[/bold]")

    if output_dir is None:
        output_dir = os.path.expanduser("~/recordings")

    with console.status("Downloading recording..."):
        result = webex.download_recording(
            recording_id,
            output_dir=output_dir,
            download_video=video,
            download_transcript=transcript,
        )

    details = result["details"]
    console.print(f"\n[bold green]Downloaded:[/bold green] {details.get('topic', 'Untitled')}")
    if result["video_path"]:
        console.print(f"  Video:      {result['video_path']}")
    if result["transcript_path"]:
        console.print(f"  Transcript: {result['transcript_path']}")
    if not result["video_path"] and not result["transcript_path"]:
        console.print("[yellow]  No download links available (you may not own this recording).[/yellow]")


if __name__ == "__main__":
    cli()
