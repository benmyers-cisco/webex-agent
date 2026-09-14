#!/bin/bash
# Wrapper for the Webex triage LaunchAgents and manual runs — loads env, runs daily summary.
#
# Usage: run_summary.sh [lookback_hours]
#   e.g. run_summary.sh 24    # look back 24 hours
#   e.g. run_summary.sh       # auto (since last run)
#
# Called by com.webex-agent.morning-triage (08:30 M–F) and
# com.webex-agent.afternoon-triage (16:00 M–F). Both write the SAME dated output file, so
# the afternoon run overwrites the morning one — which is why write_failure() below appends
# rather than clobbers when today's file already holds real content.
#
# On 2026-09-02 the 16:00 run and the 2026-09-03 08:30 run both died with
# `httpx.ConnectError: [Errno 8] nodename nor servname provided` — launchd fired them during
# a DarkWake, which has no working DNS. Two things were wrong beyond the outage itself:
# the job wrote no output file at all, so /wrap-up and /morning-coffee read the silence as a
# quiet day; and its only failure path was a Webex message, which needs the very network
# that had just failed. Both are fixed below.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
OUTPUT_DIR="$PROJECT_DIR/output"

log() { printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >&2; }

export PATH="/opt/homebrew/bin:/opt/homebrew/opt/python@3.12/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

# Which of the two daily runs this is. Used only to label the failure report — the output
# filename is the same either way.
if [ "$(date '+%H')" -lt 12 ]; then SLOT_LABEL="morning"; else SLOT_LABEL="afternoon"; fi

# A scheduled run that produces no artifact is indistinguishable from a quiet day, and
# /morning-coffee explicitly treats a missing file as "not yet available" and moves on.
#
# The append branch matters: the afternoon run overwrites the morning's file, so a plain
# `>` here would delete a perfectly good morning briefing to report an afternoon outage.
write_failure() {
  mkdir -p "$OUTPUT_DIR"
  local path="$OUTPUT_DIR/$(date '+%Y-%m-%d')-triage.md"

  if [ -s "$path" ] && ! grep -q 'Triage did not run' "$path" 2>/dev/null; then
    {
      printf '\n---\n\n## ⚠️ The %s run failed — %s\n\n' "$SLOT_LABEL" "$(date '+%I:%M %p')"
      printf '%s\n' "$1"
      printf '\nEverything above this line is from the earlier run and is still good. Nothing\n'
      printf 'newer than that has been fetched. **Do not read this as a quiet %s.**\n' "$SLOT_LABEL"
      printf '\nLog: `/tmp/webex-summary.log`\n'
    } >> "$path"
    log "appended a failure note to $path (kept the earlier run's content)"
  else
    {
      printf '# Webex Triage — %s\n\n' "$(date '+%Y-%m-%d %I:%M %p')"
      printf '## ⚠️ Triage did not run — the %s run failed\n\n' "$SLOT_LABEL"
      printf '%s\n' "$1"
      printf '\nNo Webex data was fetched. **Do not read this as a quiet day.**\n'
      printf '\nLog: `/tmp/webex-summary.log`\n'
    } > "$path"
    log "wrote failure report to $path"
  fi
}

if [ ! -f "$PROJECT_DIR/.env" ]; then
  log "FATAL: $PROJECT_DIR/.env not found."
  write_failure "\`$PROJECT_DIR/.env\` is missing, so the run aborted before fetching anything.
It holds the Webex OAuth client and the AWS settings the summary needs."
  exit 78 # EX_CONFIG
fi
set -a
# shellcheck disable=SC1091
source "$PROJECT_DIR/.env"
set +a

# Network before anything else. duo-sso, the Webex OAuth refresh, and Bedrock all need DNS,
# and each of them reports a DNS failure as its own kind of credential problem — the old
# fallback message here blamed "expired AWS credentials" for what was actually a dead
# network. Prove the network first so those messages mean what they say.
# shellcheck source=lib/wait_for_network.sh
. "$SCRIPT_DIR/lib/wait_for_network.sh"

# Must run after `source .env` (so AWS_PROFILE is set) and before duo-sso and the python.
scrub_inherited_credentials

hold_system_awake

# Overridable only so the failure path can be exercised in a test; nothing sets it in normal
# operation. Word-splitting is intended.
NETWORK_PROBE_HOSTS="${NETWORK_PROBE_HOSTS:-webexapis.com bedrock-runtime.us-east-1.amazonaws.com}"
# shellcheck disable=SC2086
if ! wait_for_network $NETWORK_PROBE_HOSTS; then
  write_failure "$(network_failure_message)"
  exit 75 # EX_TEMPFAIL
fi

# Refresh AWS credentials (non-interactive, uses the cached browser session).
#
# Goes through aws_refresh.sh rather than calling duo-sso inline, as it did until 2026-09-04.
# The inline call had no timeout, and duo-sso waits on a dead browser forever — the hourly
# refresher hung that way for 15 days. Here the consequence would have been worse than a
# missed refresh: a hang on this line would have hung the entire Webex triage, silently, with
# `caffeinate` holding the Mac awake for it.
AWS_REFRESH_LOG=/dev/stderr AWS_REFRESH_THRESHOLD=7200 \
  bash "$SCRIPT_DIR/aws_refresh.sh" >> /tmp/webex-summary.log 2>&1 || true

export SUMMARY_DEBUG=1

if [ -n "${1:-}" ]; then
    export SUMMARY_LOOKBACK_H="$1"
fi

cd "$PROJECT_DIR"
log "starting $SLOT_LABEL Webex triage (lookback ${SUMMARY_LOOKBACK_H:-auto})"

# Capture the status directly, not after an `if`: a bare `if cmd; then ... fi` whose
# condition fails leaves $? at 0, which would report every failure as exit 0.
.venv/bin/python3.12 scripts/daily_summary.py >> /tmp/webex-summary.log 2>&1
CODE=$?
log "daily_summary.py exited $CODE"

if [ "$CODE" -eq 0 ]; then
    exit 0
fi

# The network is known good at this point, so an AWS/credential diagnosis is now honest.
write_failure "\`daily_summary.py\` exited $CODE. The network passed its reachability check
first, so this is not connectivity — the usual cause is expired AWS credentials. Run:

\`\`\`
duo-sso -profile claudecode
\`\`\`

then re-run \`bash scripts/run_summary.sh\`, or just start \`/morning-coffee\`, which now
re-runs a failed triage on its own."

# Also try to notify via Webex, which uses its own OAuth rather than AWS. Best effort: the
# file above is the real signal, this is only a nudge.
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
    webex.send_message(target[0]['id'], '⚠️ Daily triage failed — likely expired AWS credentials. Run: duo-sso -profile claudecode', markdown='⚠️ **Daily triage failed** — likely expired AWS credentials.\n\nRun: \`duo-sso -profile claudecode\`')
" >> /tmp/webex-summary.log 2>&1 || log "Webex failure notification also failed (see log)"

exit $CODE
