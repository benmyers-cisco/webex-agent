# Webex Agent

A Claude Code plugin that connects to Webex as a conversational "chief of staff" — triaging messages, summarizing conversations, and helping you stay on top of what matters across your Webex spaces.

## What it does

- **Triage briefings** — Scans your spaces and categorizes what needs attention into four priority levels: blocked on you, decisions made without you, opportunities to add value, and FYI
- **Conversational agent** — Ask natural questions like "what did I miss?" or "summarize the Security Team space this week"
- **Smart space selection** — DMs and small group chats always included; large channels only if you're @mentioned or newly added
- **Draft responses** — Generates ready-to-send replies for items that need your attention
- **Trainable preferences** — Teach it what's relevant to you over time
- **Scheduled briefings** — Automated daily triage and weekly retrospectives via cron
- **Pre-meeting prep** — Pulls Webex DM history with upcoming meeting invitees and sends a Slack briefing
- **Recording downloads** — List and download your Webex recordings (video + transcript) via the API
- **Knowledge base** — Extracts and stores insights from weekly retrospectives for future reference

## Quick start

### Prerequisites

- Python 3.12+
- [Claude Code](https://docs.anthropic.com/en/docs/claude-code)
- A Webex account (OAuth integration or personal access token)

### 1. Clone and install

```bash
git clone https://github.com/benmyers-cisco/webex-agent.git
cd webex-agent
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Run the setup wizard

```bash
python scripts/setup.py
```

This walks you through environment config, OAuth, preferences, and verification interactively.

Or do it manually:

```bash
cp .env.example .env        # Edit with your credentials
cp preferences.example.md preferences.md  # Edit with your spaces/role
set -a && source .env && set +a
python servers/oauth.py     # Complete OAuth flow in browser
```

### 3. Enable the plugin in Claude Code

Add the plugin directory to your Claude Code project or install it as a plugin. The `.claude-plugin/plugin.json` and `.mcp.json` configure the MCP server automatically.

### 4. Run onboarding

Open Claude Code in the webex-agent directory and run:

```
/webex-onboard
```

This interactive skill walks you through configuring your role, classifying your spaces, and running your first calibration triage (~5-10 min).

### 5. Start using it

- "What needs my attention in Webex?"
- "Summarize the Project Alpha space from the last 3 days"
- "Search for discussions about the API migration"
- `/webex-triage` — on-demand triage briefing
- `/webex-triage teach` — update relevance preferences

For a more detailed walkthrough, see [docs/setup-guide.md](docs/setup-guide.md).

## Authentication

### Option A: OAuth (recommended for enterprise/Cisco orgs)

1. Create a Webex Integration at [developer.webex.com](https://developer.webex.com/my-apps/new/integration)
2. Set the redirect URI to `http://localhost:8844/callback`
3. Request these scopes: `spark:messages_read`, `spark:messages_write`, `spark:rooms_read`, `spark:rooms_write`, `spark:memberships_write`, `spark:people_read`, `spark:recordings_read`, `meeting:recordings_read`, `meeting:schedules_read`
4. Add to your `.env`:

```
WEBEX_CLIENT_ID=your_client_id
WEBEX_CLIENT_SECRET=your_client_secret
```

5. Run `python servers/oauth.py` to complete the OAuth flow

### Option B: Personal access token

For personal use or testing, get a token from [developer.webex.com](https://developer.webex.com/docs/getting-started):

```
WEBEX_ACCESS_TOKEN=your_token_here
```

Note: Personal tokens expire after 12 hours.

## Environment variables

| Variable | Required | Description |
|---|---|---|
| `WEBEX_CLIENT_ID` | For OAuth | Webex integration client ID |
| `WEBEX_CLIENT_SECRET` | For OAuth | Webex integration client secret |
| `WEBEX_ACCESS_TOKEN` | For token auth | Personal access token (fallback if OAuth not configured) |
| `SUMMARY_USER_EMAIL` | No | Your Webex email (used to identify your messages in triage) |
| `SUMMARY_DELIVERY` | No | Delivery method: `webex`, `email`, or `both` |
| `SUMMARY_WEBEX_SPACE` | No | Webex space name to post briefings to |
| `SUMMARY_EMAIL_TO` | No | Email address for briefing delivery |
| `SUMMARY_LOOKBACK_H` | No | Override lookback hours for daily summary |
| `CLAUDE_CODE_USE_BEDROCK` | No | Set to `true` if using Claude via AWS Bedrock |
| `AWS_PROFILE` | No | AWS profile for Bedrock access |
| `SLACK_USER_ID` | No | Your Slack user ID (for pre-meeting briefing DMs) |
| `SLACK_ENV_FILE` | No | Path to Slack bot .env file (for pre-meeting DMs) |
| `MEETINGS_DIR` | No | Path to meeting tracking files (default: `~/.claude/memory/meetings`) |
| `NOTES_DIR` | No | Path to notes directory (default: `~/.claude/memory/notes`) |
| `MEETING_WINDOW_MINUTES` | No | Minutes before meeting to trigger prep (default: 10) |
| `MEETING_HISTORY_DAYS` | No | Days of DM history to pull for meeting prep (default: 14) |

## Project structure

```
webex-agent/
  agents/
    webex-analyst.md        # Conversational agent with triage framework
  servers/
    webex_mcp.py            # MCP server — exposes Webex tools to Claude
    oauth.py                # OAuth2 flow, token storage and refresh
  scripts/
    daily_summary.py        # Automated daily triage (cron)
    weekly_retro.py         # Weekly retrospective with learning extraction
    check_upcoming_meetings.py  # Pre-meeting Slack briefings
    run_summary.sh          # Manual summary runner
    run_retro.sh            # Manual retro runner
  skills/
    webex-onboard.md        # Interactive first-time setup (run after install)
    webex-triage.md         # Shareable triage skill (no infra needed)
    weekly-retrospective/   # Weekly learning extraction methodology
    decision-log/           # Decision capture from conversations
    meeting-debrief/        # Post-meeting structured debriefs
  webex_client.py           # HTTP client for Webex API (messages, spaces, recordings)
  preferences.example.md    # Template for trainable relevance rules (copy to preferences.md)
  knowledge.md              # Persistent insights from retros (gitignored)
  .env.example              # Template for environment variables
```

## MCP tools

The MCP server (`servers/webex_mcp.py`) exposes these tools to Claude:

| Tool | Description |
|---|---|
| `list_spaces` | List your Webex spaces and DMs |
| `get_messages` | Fetch messages from a space with time filters |
| `search_messages` | Keyword search within a space |
| `get_space_details` | Space metadata (type, created, last activity) |
| `send_message` | Send a message to a space |
| `send_email` | Send email via SMTP |
| `get_preferences` | Read triage relevance rules |
| `update_preferences` | Train what's relevant to you |
| `search_knowledge` | Query the personal knowledge base |
| `list_recordings` | List your Webex recordings with date filters |
| `download_recording` | Download recording video and/or transcript |
| `triage` | On-demand triage — scan spaces and categorize what needs attention |

All tools automatically retry with a fresh OAuth token on 401 errors.

## The triage framework

The core value of this plugin is how it decides what deserves your attention:

**Space selection:**
- DMs and small group chats (<=10 people): always included if there's new activity
- Large channels (>10 people): only if you're @mentioned and haven't responded, or you were newly added in the last 24 hours
- Spaces in "Always Scan" preferences: included if they have any activity in the lookback window
- Spaces in "Never Scan": skipped entirely (zero API calls)

**Priority buckets:**
1. **Blocked on You** — someone explicitly asked you something or is waiting for a deliverable you committed to, and you haven't responded. Strict criteria: must be a clear ask, not a vague suggestion.
2. **Waiting on Others** — threads where you sent the last message or made a request
3. **Decisions Made Without You** — concrete decisions that affect your work
4. **Opportunities to Add Value** — discussions where your specific expertise would change the outcome (high bar — not just "could chime in")
5. **FYI** — context only, no action needed

Results are grouped by priority across all spaces, not per-space — so the most urgent items are always at the top.

**New space detection:**
- The agent tracks which spaces it has seen across runs (`.known_spaces.json`)
- When a new channel appears in your triage for the first time, it generates a classification suggestion (Always Scan P1/P2/P3, Mentions Only, or Never Scan) based on the content it analyzed
- DMs and group chats are labeled "no action needed" since they're always scanned
- Confirm or override suggestions via the `update_preferences` MCP tool

## Using the triage skill standalone

If you already have a Webex MCP server and just want the triage logic, copy `skills/webex-triage.md` into your `~/.claude/commands/` directory. It works with any MCP server that provides `list_spaces` and `get_messages` tools — no Python scripts or additional infrastructure needed.

## Scheduled briefings (optional)

For automated daily and weekly briefings, add cron entries:

```bash
# Daily triage at 8:30am and 4pm ET (weekdays)
30 12 * * 1-5 cd /path/to/webex-agent && set -a && source .env && set +a && .venv/bin/python3.12 scripts/daily_summary.py
0 20 * * 1-5 cd /path/to/webex-agent && set -a && source .env && set +a && .venv/bin/python3.12 scripts/daily_summary.py

# Weekly retrospective Friday 1pm ET
0 17 * * 5 cd /path/to/webex-agent && set -a && source .env && set +a && .venv/bin/python3.12 scripts/weekly_retro.py
```

The daily summary uses a `.last_run` file to automatically look back to the previous run, so you never miss messages between runs.

## Trainable preferences

Teach the agent what matters to you by copying `preferences.example.md` to `preferences.md` and editing it:

```bash
cp preferences.example.md preferences.md
# Edit with your spaces, role, and rules
```

The preference file has these sections:

| Section | Purpose |
|---------|---------|
| **My Role & Focus** | Who you are and what you care about (context for the AI) |
| **Always Scan** | Spaces to always triage, grouped by priority tier (P1/P2/P3) |
| **Mentions Only** | Large/noisy spaces — only included if you're @mentioned |
| **Never Scan** | Skip entirely (social, off-topic, personal) |
| **Space-Specific Rules** | Per-space instructions in natural language |
| **Noise Patterns to Ignore** | Global filters (bot messages, greetings, automated alerts) |

Priority tiers control ordering in triage output — P1 spaces appear first, P3 last.

You can also update preferences conversationally via the `update_preferences` MCP tool.

## License

MIT
