#!/bin/bash
# Wrapper for weekly retrospective cron
# Usage: run_retro.sh [lookback_days]
#   e.g. run_retro.sh 14    # look back 2 weeks
#   e.g. run_retro.sh       # default 7 days
set -a
source "$(dirname "$0")/../.env"
set +a

export PATH="/opt/homebrew/bin:/opt/homebrew/opt/python@3.12/bin:$PATH"

# Attempt to refresh AWS credentials (non-interactive, uses cached browser session).
# Via aws_refresh.sh for the timeout — a bare duo-sso waits on a dead browser forever, which
# is how the hourly refresher hung for 15 days from 2026-08-19. Inline here it would hang the
# retro instead.
AWS_REFRESH_LOG=/dev/stderr AWS_REFRESH_THRESHOLD=7200 \
  bash "$(dirname "$0")/aws_refresh.sh" >> /tmp/webex-retro.log 2>&1 || true

if [ -n "$1" ]; then
    export RETRO_LOOKBACK_DAYS="$1"
fi

cd "$(dirname "$0")/.."
if ! .venv/bin/python3.12 scripts/weekly_retro.py >> /tmp/webex-retro.log 2>&1; then
    # If the script failed, try to notify via Webex (which uses separate OAuth, not AWS)
    .venv/bin/python3.12 -c "
import os, sys
sys.path.insert(0, '.')
sys.path.insert(0, 'servers')
from oauth import get_valid_token
from webex_client import WebexClient
token = get_valid_token(os.environ.get('WEBEX_CLIENT_ID',''), os.environ.get('WEBEX_CLIENT_SECRET',''))
if not token: sys.exit(1)
webex = WebexClient(token)
spaces = webex.list_spaces(max_results=200)
target = [s for s in spaces if 'my webex summaries' in s['title'].lower()]
if target:
    webex.send_message(target[0]['id'], '⚠️ Weekly retro failed — likely expired AWS credentials. Run: duo-sso -profile claudecode', markdown='⚠️ **Weekly retro failed** — likely expired AWS credentials.\n\nRun: \`duo-sso -profile claudecode\`')
" >> /tmp/webex-retro.log 2>&1
fi
