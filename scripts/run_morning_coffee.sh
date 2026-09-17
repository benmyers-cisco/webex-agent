#!/bin/bash
# Unattended /morning-coffee runner — writes the day's briefing before Ben sits down.
#
# Called by com.benmyers.morning-coffee (07:20 M–F). Writes
# ~/claude-memory/daily/YYYY-MM-DD-briefing.md, which Hanuman Hub reads and shows when you
# open /morning-coffee (see claude-hub server/routes/skills.js LAST_RUN_SOURCES).
#
# It lives in webex-agent/scripts, not with claude-hub, for one reason: the network gate,
# the caffeinate hold, and the credential scrub in lib/wait_for_network.sh are the accumulated
# answer to every way a 6am launchd job has silently failed on this Mac. Duplicating them
# somewhere tidier would mean re-learning them.
#
# WHAT IT DOES NOT DO
# -------------------
# It does not write ~/today-plan.md or the daily plan file. Those are the record of what Ben
# committed to, and nothing unattended gets to commit on his behalf — the briefing ends with
# *suggested* focus items and the interactive run is what locks them in.
#
# THE TWO MODES, AND WHY THE SECOND ONE EXISTS
# -------------------------------------------
# On 2026-09-17 all three morning jobs failed identically and the network gate could not have
# saved any of them. The Mac was in DarkWake with no DNS from 02:00 onward and only reached
# FullWake at 09:07:53, on HID activity — 67 minutes after this job gave up. `caffeinate -si`
# keeps the retry loop from being cut short by sleep, but it cannot promote a DarkWake into a
# FullWake, so waiting longer inside the 07:20 slot would only have waited longer in the dark.
# And StartCalendarInterval never re-fires a missed or failed occurrence, so 08:00 was terminal
# for the day: the network came back at 09:07 and nothing was left to notice.
#
#   --catchup  A second LaunchAgent fires this every 15 minutes, 07:45–10:45 M–F. It probes the
#              network for 60s instead of 30 minutes and exits QUIETLY (75) if it is still dark,
#              leaving the primary run's ⚠️ briefing in place to try again a quarter hour later.
#              The first fire after Ben wakes the machine finds real DNS, repairs both triage
#              artifacts, and replaces the ⚠️ file with the real briefing. One mechanism heals
#              all three morning jobs, because ensure_triage already re-runs the other two.
#
# The two modes are separate LaunchAgents, which means they can overlap — the primary can still
# be inside its 30-minute wait at 07:45. Hence the lock.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
# Overridable only so a test can point the two artifact directories at a sandbox and exercise
# the whole run — including the catch-up replacing a ⚠️ briefing — without touching real output
# or re-posting a Webex summary. Nothing sets them in normal operation.
TRIAGE_DIR="${TRIAGE_DIR:-$PROJECT_DIR/output}"
BRIEFING_DIR="${BRIEFING_DIR:-$HOME/claude-memory/daily}"
TODAY="$(date '+%Y-%m-%d')"
BRIEFING="$BRIEFING_DIR/$TODAY-briefing.md"
LOG=/tmp/morning-coffee.log
LOCK_DIR=/tmp/morning-coffee.lock

MODE=primary
for arg in "$@"; do
  case "$arg" in
    --catchup) MODE=catchup ;;
    *) printf 'usage: %s [--catchup]\n' "$0" >&2; exit 64 ;; # EX_USAGE
  esac
done

log() { printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$MODE: $*" >&2; }

export PATH="/Users/benmyers/.local/bin:/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

# How long to let the claude run take before killing it. A hang here is worse than a failure:
# StartCalendarInterval will not start a second instance while one is still running, so one
# wedged process silently kills every future morning until someone notices.
CLAUDE_TIMEOUT_SECS="${CLAUDE_TIMEOUT_SECS:-1500}"
# Overridable only so the timeout and failure paths can be exercised without a real model run.
# Nothing sets it in normal operation.
CLAUDE_BIN="${CLAUDE_BIN:-claude}"
# How long to wait for a triage run that is already in flight (DarkWake retries can stretch
# the 06:45 Webex job well past its usual 10 minutes).
TRIAGE_WAIT_SECS="${TRIAGE_WAIT_SECS:-900}"
# Captured BEFORE lib/wait_for_network.sh is sourced, because sourcing it defaults these to
# 1800/300 — after which `${NETWORK_WAIT_MAX_SECS:-60}` would silently resolve to 1800 and the
# catch-up mode would inherit the half-hour wait it exists to avoid. Empty means "not overridden".
NETWORK_WAIT_MAX_SECS_ENV="${NETWORK_WAIT_MAX_SECS:-}"
NETWORK_WAIT_INTERVAL_ENV="${NETWORK_WAIT_INTERVAL:-}"

# True when today's briefing file exists and is a real briefing rather than a failure report.
have_good_briefing() {
  [ -s "$BRIEFING" ] && ! grep -q 'did not run' "$BRIEFING" 2>/dev/null
}

# A missing briefing file and a stale one look identical in the hub panel except for a
# timestamp, and a timestamp is easy to skim past. So a failed run writes a briefing that
# says so, in Ben's own reading order, rather than leaving yesterday's to be mistaken for
# today's.
#
# The append branch is not hypothetical: Ben can run `/morning-coffee` by hand at 7:05, and a
# plain `>` here would then delete a perfectly good briefing at 7:20 in order to report a
# failure that no longer matters to him.
write_failure() {
  mkdir -p "$BRIEFING_DIR"

  if have_good_briefing; then
    {
      printf '\n---\n\n## ⚠️ The unattended %s run failed — %s\n\n' \
        "$([ "$MODE" = catchup ] && echo catch-up || echo 07:20)" "$(date '+%-I:%M %p')"
      printf '%s\n' "$1"
      printf '\nEverything above this line is from the earlier run and is still good.\n'
      printf '\nLog: `%s`\n' "$LOG"
    } >> "$BRIEFING"
    log "appended a failure note to $BRIEFING (kept the earlier briefing)"
    return
  fi

  {
    printf '# Morning Briefing — %s\n\n' "$(date '+%A, %B %-d, %Y')"
    printf '## ⚠️ The unattended briefing did not run\n\n'
    printf '%s\n' "$1"
    printf '\n**This is not a quiet morning — it is a missing briefing.**\n'
    printf 'A catch-up run retries every 15 minutes until 10:45 and will replace this file with\n'
    printf 'the real briefing on its own. Run `/morning-coffee` if you want it right now.\n'
    printf '\nLog: `%s`\n' "$LOG"
  } > "$BRIEFING"
  log "wrote failure briefing to $BRIEFING"
}

# OK / MISSING / FAILED for one triage artifact. Checking for the ⚠️ banner and not just for
# the file is the point: a failed triage now writes a small report rather than nothing,
# precisely so it cannot be mistaken for a quiet day — which means the file existing is no
# longer proof it has data in it.
triage_state() {
  local path="$TRIAGE_DIR/$TODAY-$1.md"
  if [ ! -s "$path" ]; then echo MISSING
  elif grep -q 'Triage did not run\|run failed' "$path" 2>/dev/null; then echo FAILED
  else echo OK
  fi
}

# Bring one triage artifact to OK if we can. Waits on a run already in flight before starting
# a second one — two concurrent daily_summary.py processes write the same file.
ensure_triage() {
  local suffix="$1" script="$2" procpat="$3" label="$4"
  local state waited=0

  while :; do
    state="$(triage_state "$suffix")"
    [ "$state" = OK ] && { log "$label triage: OK"; return 0; }

    if pgrep -f "$procpat" >/dev/null 2>&1; then
      if [ "$waited" -ge "$TRIAGE_WAIT_SECS" ]; then
        log "$label triage: still running after ${waited}s — giving up waiting"
        return 1
      fi
      log "$label triage: $state but a run is in flight — waiting (${waited}s of ${TRIAGE_WAIT_SECS}s)"
      sleep 60
      waited=$((waited + 60))
      continue
    fi

    log "$label triage: $state and nothing running — re-running it now"
    # 300s rather than the scheduled 1800s: the network gate below already proved DNS works,
    # so a failure here is a real failure and should say so instead of stalling the briefing
    # for half an hour.
    NETWORK_WAIT_MAX_SECS=300 bash "$SCRIPT_DIR/$script" >> "$LOG" 2>&1
    state="$(triage_state "$suffix")"
    if [ "$state" = OK ]; then log "$label triage: repaired"; return 0; fi
    log "$label triage: re-run left it $state"
    return 1
  done
}

# Run a command with a hard wall-clock cap. macOS ships no coreutils `timeout`.
#
# `disown` is what keeps the log readable: without it the shell announces the watchdog's own
# death ("Terminated: 15  ( sleep ... )") on the way out, which reads in the log like the job
# failed on a run that actually succeeded.
run_with_timeout() {
  local secs="$1"; shift
  "$@" &
  local pid=$!
  ( sleep "$secs"; kill -TERM "$pid" 2>/dev/null; sleep 15; kill -KILL "$pid" 2>/dev/null ) &
  local watchdog=$!
  disown "$watchdog" 2>/dev/null || true
  wait "$pid"
  local code=$?
  kill "$watchdog" 2>/dev/null || true
  return $code
}

# Only one instance of this runner at a time, across BOTH LaunchAgents.
#
# launchd will not start a second instance of the same label, but primary and catch-up are
# different labels: the 07:20 run can still be inside its 30-minute network wait when the 07:45
# catch-up fires. Two concurrent `claude -p "/morning-coffee --unattended"` runs would race on
# the same briefing file, and ensure_triage's own guard only covers the triage scripts.
#
# `mkdir` is the atomic test-and-set. The pid file makes a stale lock recoverable: if the owner
# is gone — killed at the TimeOut cap, or lost to a hard reboot — the next fire steals it rather
# than every future morning declining to run.
acquire_lock() {
  local owner
  if mkdir "$LOCK_DIR" 2>/dev/null; then
    printf '%s\n' "$$" > "$LOCK_DIR/pid"
    trap 'rm -rf "$LOCK_DIR"' EXIT
    return 0
  fi

  owner="$(cat "$LOCK_DIR/pid" 2>/dev/null)"
  if [ -n "$owner" ] && kill -0 "$owner" 2>/dev/null; then
    log "another run (pid $owner) is in flight — standing down"
    return 1
  fi

  log "clearing a stale lock left by pid ${owner:-unknown}"
  rm -rf "$LOCK_DIR"
  if mkdir "$LOCK_DIR" 2>/dev/null; then
    printf '%s\n' "$$" > "$LOCK_DIR/pid"
    trap 'rm -rf "$LOCK_DIR"' EXIT
    return 0
  fi
  log "could not take the lock after clearing it — standing down"
  return 1
}

# Nothing left for a catch-up fire to do: real briefing, and both triage artifacts have data.
all_good() {
  have_good_briefing \
    && [ "$(triage_state github-triage)" = OK ] \
    && [ "$(triage_state triage)" = OK ]
}

# shellcheck source=lib/wait_for_network.sh
. "$SCRIPT_DIR/lib/wait_for_network.sh"

# Claude Code authenticates to Bedrock with the long-lived IAM keys in ~/.claude/settings.json
# `env`, so unlike the triage jobs this one needs no duo-sso refresh and must NOT source
# webex-agent/.env — that would put AWS_PROFILE in front of the keys claude expects. The
# scrub still runs so a manual invocation from inside a Claude Code session behaves like the
# scheduled one.
scrub_inherited_credentials

# graph.microsoft.com is in the list because the calendar is section A of the briefing — the
# one part with no substitute. bedrock-runtime is how claude itself talks to the model.
NETWORK_PROBE_HOSTS="${NETWORK_PROBE_HOSTS:-graph.microsoft.com bedrock-runtime.us-east-1.amazonaws.com webexapis.com}"

if [ "$MODE" = catchup ]; then
  # Cheapest possible no-op, and it needs no network: reading three files answers whether this
  # fire has anything to do. Thirteen fires a morning, so the common case must cost nothing.
  if all_good; then
    log "briefing and both triage artifacts are already good — nothing to do"
    exit 0
  fi

  acquire_lock || exit 0

  # 60s, not 30 minutes. A catch-up fire is not trying to outlast a DarkWake — it is asking
  # "is the machine properly awake yet?" and the answer costs nothing to ask again at :00.
  NETWORK_WAIT_MAX_SECS="${NETWORK_WAIT_MAX_SECS_ENV:-60}"
  NETWORK_WAIT_INTERVAL="${NETWORK_WAIT_INTERVAL_ENV:-30}"
  # shellcheck disable=SC2086
  if ! wait_for_network $NETWORK_PROBE_HOSTS; then
    # Deliberately NOT write_failure. The primary run's ⚠️ briefing already says the right
    # thing, and overwriting it once a quarter hour would churn the file Hanuman Hub reads while
    # adding no information. Exit 75 so `launchctl print` still shows the morning went wrong.
    log "still dark — leaving the existing briefing alone and waiting for the next fire"
    exit 75 # EX_TEMPFAIL
  fi
  hold_system_awake
else
  acquire_lock || exit 0
  hold_system_awake
  # shellcheck disable=SC2086
  if ! wait_for_network $NETWORK_PROBE_HOSTS; then
    write_failure "$(network_failure_message)"
    exit 75 # EX_TEMPFAIL
  fi
fi

# Both triage jobs are scheduled ahead of this one (06:35 GitHub, 06:45 Webex) specifically so
# their output exists by now. Independent of each other, so a failure in one does not stop the
# other.
ensure_triage github-triage run_github_triage.sh github_triage.py GitHub || TRIAGE_GITHUB_BAD=1
ensure_triage triage        run_summary.sh       daily_summary.py  Webex  || TRIAGE_WEBEX_BAD=1

mkdir -p "$BRIEFING_DIR"

# An early riser who already ran `/morning-coffee` by hand has the briefing he wanted, built
# from fresher signal than this run would use. Rebuilding it would cost a full model run to
# replace something better.
if have_good_briefing; then
  log "today's briefing already exists and looks good — nothing to do"
  exit 0
fi

log "starting unattended /morning-coffee (cap ${CLAUDE_TIMEOUT_SECS}s)"
run_with_timeout "$CLAUDE_TIMEOUT_SECS" \
  "$CLAUDE_BIN" -p "/morning-coffee --unattended" \
  --dangerously-skip-permissions >> "$LOG" 2>&1
CODE=$?
log "claude exited $CODE"

# The exit code alone is not the test. The deliverable is the file, and claude can exit 0
# having written nothing — so verify the artifact, and verify it is TODAY's.
if have_good_briefing; then
  log "briefing written: $BRIEFING"
  [ -n "${TRIAGE_GITHUB_BAD:-}" ] && log "note: GitHub triage was not OK — the briefing should say so"
  [ -n "${TRIAGE_WEBEX_BAD:-}" ] && log "note: Webex triage was not OK — the briefing should say so"
  exit 0
fi

if [ "$CODE" -eq 143 ] || [ "$CODE" -eq 137 ]; then
  write_failure "The \`claude\` run was killed at its ${CLAUDE_TIMEOUT_SECS}s cap without finishing.
The network passed its reachability check first, so this is not connectivity. Check \`$LOG\`
for where it stopped."
else
  write_failure "\`claude -p \"/morning-coffee --unattended\"\` exited $CODE without writing
\`$BRIEFING\`. The network passed its reachability check first, so this is not connectivity.
Check \`$LOG\`."
fi

# Never exit 0 here. `claude` exiting 0 having written nothing is the most likely way this
# breaks, and reporting that to launchd as success is how it would go unnoticed for a week.
[ "$CODE" -eq 0 ] && CODE=70 # EX_SOFTWARE
exit "$CODE"
