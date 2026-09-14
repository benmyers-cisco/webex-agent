#!/bin/bash
# Watchdog wrapper around the duo-sso credential refresh. THIS IS THE ONLY WAY THE REFRESH
# SHOULD BE INVOKED — by com.webex-agent.aws-refresh (hourly) and by every triage wrapper
# that needs live Bedrock credentials.
#
# WHY THIS EXISTS
# ---------------
# On 2026-09-04 the 08:00 GitHub triage died with
# `403 The security token included in the request is expired`. The cause was NOT a
# credential someone forgot to refresh. `com.webex-agent.aws-refresh` — the hourly
# `duo-sso -valid-session-threshold` job whose entire purpose is to stop exactly that —
# had been HUNG SINCE 2026-08-19. It decided to renew, launched Chrome, Chrome never came
# up, and duo-sso has no timeout of its own, so it sat there for 15 days 17 hours drawing a
# progress spinner into its log. The log reached 320 MB of spinner frames.
#
# Two properties of launchd turn that one hang into a silent, total outage:
#
#   1. StartInterval does NOT overlap. launchd will not start a second instance of a job
#      while the previous instance is still running, so ONE wedged process killed every
#      subsequent hourly refresh. Nothing errors. The only symptom is an absence.
#   2. The symptom surfaces somewhere else entirely. Every Bedrock-dependent job starts
#      failing on expired credentials hours later, in a different log, with a message that
#      accuses AWS rather than the refresher that stopped running.
#
# So this wrapper adds the three things the bare command was missing:
#   - a hard timeout, because duo-sso will wait on a dead browser forever;
#   - a sweep for an already-wedged instance, so one hang cannot outlive its own hour;
#   - one summary line per run instead of raw terminal output, so the log stays readable
#     and bounded.
#
# `timeout(1)` is NOT installed on this machine — no coreutils, no gtimeout — hence the
# hand-rolled watchdog below rather than the one-word fix.
#
# No `set -e`: we need duo-sso's exit status, and the watchdog's kill must not abort us.
set -uo pipefail

PROFILE="${AWS_REFRESH_PROFILE:-claudecode}"
# Renew when less than this much of the session is left. The hourly job uses 3600 so it
# always renews at least one full hour before expiry; callers that run right before a
# Bedrock call pass more, so a long run cannot expire mid-flight.
THRESHOLD="${AWS_REFRESH_THRESHOLD:-3600}"
# A healthy refresh takes a few seconds against a warm SSO cookie. 120s is generous for a
# cold Chrome start and still bounded well inside the hourly interval.
TIMEOUT_SECS="${AWS_REFRESH_TIMEOUT:-120}"
# Where the summary lines go. The triage wrappers pass /dev/stderr so the refresh narrates
# into the same stream as their own log() output — the job's log file when scheduled, the
# terminal when run by hand.
LOG="${AWS_REFRESH_LOG:-/tmp/webex-aws-refresh.log}"
# Cap the log so a future noisy failure mode cannot fill the disk the way the last one
# nearly did. 5 MB of one-line-per-run is years of history.
LOG_MAX_BYTES="${AWS_REFRESH_LOG_MAX_BYTES:-5242880}"
# duo-sso -chrome-persistent keeps its browser profile here. Used only to clean up a Chrome
# that outlived the duo-sso that spawned it; scoped to this path so it can never match the
# browser Ben is actually using.
CHROME_PROFILE_DIR="$HOME/.local/state/duo-sso/chrome"

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

log() { printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >> "$LOG"; }

# The /dev/* guard is not cosmetic: when a caller passes /dev/stderr and its own stderr is
# redirected to a file, /dev/stderr resolves to THAT file — so an unguarded rotation would
# rename the calling job's log out from under it mid-run.
case "$LOG" in
  /dev/*) : ;;
  *) if [ -f "$LOG" ] && [ "$(wc -c < "$LOG" | tr -d ' ')" -gt "$LOG_MAX_BYTES" ]; then
       mv -f "$LOG" "$LOG.1" 2>/dev/null && log "rotated previous log to $LOG.1"
     fi ;;
esac

# --- stale-instance sweep ---------------------------------------------------------------
#
# Convert ps's etime ([[DD-]HH:]MM:SS) to seconds. macOS ps has no `etimes` keyword, so the
# formatted field is all there is. `10#` is required: an etime field like `08:30` would
# otherwise be read as invalid octal and abort the arithmetic.
etime_to_secs() {
  local e="$1" days=0 h=0 m=0 s=0
  case "$e" in *-*) days="${e%%-*}"; e="${e#*-}" ;; esac
  local IFS=:
  # shellcheck disable=SC2086
  set -- $e
  case $# in
    3) h="$1"; m="$2"; s="$3" ;;
    2) m="$1"; s="$2" ;;
    1) s="$1" ;;
  esac
  echo $(( 10#$days * 86400 + 10#$h * 3600 + 10#$m * 60 + 10#$s ))
}

# Match only the wrapper's own invocation shape. An interactive `duo-sso -profile claudecode`
# that Ben is running by hand has no -valid-session-threshold, so it can never match here —
# which matters, because that one may legitimately sit for minutes waiting on a Duo push.
STALE_PATTERN="duo-sso -profile $PROFILE -valid-session-threshold"
while read -r spid setime; do
  [ -n "${spid:-}" ] || continue
  age="$(etime_to_secs "$setime")"
  # Older than our own timeout means it can only be a leftover: a healthy instance is gone
  # well inside that window.
  if [ "$age" -gt "$TIMEOUT_SECS" ]; then
    log "WEDGED: killing a stale duo-sso (pid $spid, age ${age}s) left over from an earlier run"
    kill "$spid" 2>/dev/null
    sleep 2
    kill -9 "$spid" 2>/dev/null
  fi
done < <(ps -o pid=,etime=,command= -ax | grep -F -- "$STALE_PATTERN" | grep -v grep | awk '{print $1, $2}')

# --- the refresh itself ------------------------------------------------------------------
OUT="$(mktemp -t aws_refresh)"
# shellcheck disable=SC2064
trap "rm -f '$OUT'" EXIT

duo-sso -profile "$PROFILE" -valid-session-threshold "$THRESHOLD" -chrome-persistent \
  > "$OUT" 2>&1 &
PID=$!

# Watchdog subshell rather than a kill -0 polling loop: `kill -0` succeeds for a zombie, so a
# poll can spin until bash happens to reap the child. Waiting on the pid directly and letting
# a background timer shoot it means the exit status is always definitive.
( sleep "$TIMEOUT_SECS"
  if kill -0 "$PID" 2>/dev/null; then
    kill "$PID" 2>/dev/null
    sleep 5
    kill -9 "$PID" 2>/dev/null
  fi ) >/dev/null 2>&1 &
WATCHDOG=$!

wait "$PID"
CODE=$?
kill "$WATCHDOG" 2>/dev/null
wait "$WATCHDOG" 2>/dev/null

# Reduce duo-sso's terminal output to the lines that carry information. The spinner redraws
# itself with carriage returns, so splitting on \r turns each frame into its own line; the
# braille frame characters and the ANSI escapes are non-ASCII or non-printable and get
# dropped, leaving the bare repeated phrase to filter out. LC_ALL=C because sed on macOS
# aborts with "RE error: illegal byte sequence" on the braille bytes in a UTF-8 locale.
summarize_output() {
  LC_ALL=C tr '\r' '\n' < "$OUT" \
    | LC_ALL=C tr -cd '\11\12\40-\176' \
    | LC_ALL=C sed 's/\[?25[lh]//g; s/\[0K//g; s/\[[0-9;]*m//g' \
    | grep -v 'Authenticating to Cisco SSO' \
    | grep -v '^[[:space:]]*$' \
    | tail -3 \
    | sed 's/^[[:space:]]*/  | /'
}

# 143 is SIGTERM and 137 is SIGKILL, and the watchdog above is the only thing that signals
# this child — so either one means the timeout fired, not that duo-sso decided anything.
if [ "$CODE" -eq 143 ] || [ "$CODE" -eq 137 ]; then
  log "TIMEOUT: duo-sso exceeded ${TIMEOUT_SECS}s and was killed (this is the 2026-08-19 hang)"
  summarize_output >> "$LOG"
  # A Chrome that outlived its duo-sso would keep the profile directory locked and make the
  # next attempt fail too. Scoped to duo-sso's own --user-data-dir, never Ben's browser.
  case "$CHROME_PROFILE_DIR" in
    */duo-sso/chrome) pkill -f -- "$CHROME_PROFILE_DIR" 2>/dev/null && \
                        log "also killed a Chrome still holding $CHROME_PROFILE_DIR" ;;
  esac
  exit 75 # EX_TEMPFAIL — the next hourly run gets a clean slot, which is the whole point
fi

if [ "$CODE" -ne 0 ]; then
  log "duo-sso exited $CODE — credentials may be stale"
  summarize_output >> "$LOG"
  exit "$CODE"
fi

# Report what the session actually looks like now, so the log answers "were the credentials
# good at 08:00?" without needing the downstream job's log to infer it.
REMAINING="$(duo-sso -session-expires 2>/dev/null | awk -v p="$PROFILE" '$1 == p {print $2, $3}')"
log "ok — ${REMAINING:-session state unknown} (threshold ${THRESHOLD}s)"
exit 0
