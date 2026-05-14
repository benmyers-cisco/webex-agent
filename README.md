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
- **Knowledge base** — Extracts and stores insights from weekly retrospectives for future reference

## Quick start

### Prerequisites

- Python 3.12+
- [Claude Code](https://docs.anthropic.com/en/docs/claude-code)
- A Webex account (OAuth integration or personal access token)

### 1. Clone and install

```bash
git clone https://github.com/benxmy/webex-agent.git
cd webex-agent
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure environment

```bash
cp .env.example .env
```

Edit `.env` with your credentials. See [Authentication](#authentication) for details.

### 3. Enable the plugin in Claude Code

Add the plugin directory to your Claude Code project or install it as a plugin. The `.claude-plugin/plugin.json` and `.mcp.json` configure the MCP server automatically.

### 4. Authenticate with Webex

```bash
set -a && source .env && set +a
python servers/oauth.py
```

This opens a browser for Webex OAuth authorization. Tokens are saved to `.webex_token.json` and auto-refresh.

### 5. Start using it

Open Claude Code and ask:

- "What needs my attention in Webex?"
- "Summarize the Project Alpha space from the last 3 days"
- "Search for discussions about the API migration"

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
| `SUMMARY_USER_EMAIL` | No | Your Webex email (default: used for identifying your messages in triage) |
| `SUMMARY_DELIVERY` | No | Delivery method: `webex`, `email`, or `both` |
| `SUMMARY_WEBEX_SPACE` | No | Webex space name to post briefings to |
| `SUMMARY_EMAIL_TO` | No | Email address for briefing delivery |
| `SUMMARY_LOOKBACK_H` | No | Override lookback hours for daily summary |
| `CLAUDE_CODE_USE_BEDROCK` | No | Set to `true` if using Claude via AWS Bedrock |
| `AWS_PROFILE` | No | AWS profile for Bedrock access |

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
    webex-triage.md         # Shareable triage skill (no infra needed)
    weekly-retrospective/   # Weekly learning extraction methodology
    decision-log/           # Decision capture from conversations
    meeting-debrief/        # Post-meeting structured debriefs
  webex_client.py           # HTTP client for Webex API
  preferences.md            # Trainable relevance rules
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

## The triage framework

The core value of this plugin is how it decides what deserves your attention:

**Space selection:**
- DMs and small group chats (<=10 people): always included if there's new activity
- Large channels (>10 people): only if you're @mentioned and haven't responded, or you were newly added in the last 24 hours

**Priority buckets:**
1. **Blocked on You** — someone can't move forward without your input (includes draft responses)
2. **Decisions Made Without You** — things decided that affect your work
3. **Opportunities to Add Value** — where your expertise could help proactively (includes draft messages)
4. **FYI** — context only, no action needed

Results are grouped by priority across all spaces, not per-space — so the most urgent items are always at the top.

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

Teach the agent what matters to you by copying `preferences.example.md` to `preferences.md` and editing it. You can also update it conversationally ("I don't care about help desk chatter"):

```markdown
## My Role & Focus
- Lead engineer on the auth platform team

## Always Relevant
- API breaking changes
- Security incidents

## Never Relevant
- General help desk chatter unless mentioned by name
- Social/watercooler channels

## Space-Specific Rules
- In All-Hands: only flag if directly mentioned
```

## License

MIT
