# shellcheck shell=bash
#
# Shared network-readiness gate for the scheduled triage jobs. Source it, don't run it.
#
# WHY THIS EXISTS
# ---------------
# On 2026-09-02 the 16:00 Webex triage and both GitHub triage runs failed, and all three
# blamed the wrong thing. The machine was in **DarkWake**, not awake:
#
#   09-02 15:43  Sleep (Idle)
#   09-02 16:12  DarkWake (SleepService)   <- the 16:00 jobs fired 3s into this
#   09-03 07:15  DarkWake (Maintenance)    <- the 08:00 and 08:30 jobs fired in here
#   09-03 08:53  Wake (FullWake, HID)      <- first time the machine was really up
#
# DarkWake runs launchd jobs but does NOT give them working DNS. Webex died with
# `httpx.ConnectError: [Errno 8] nodename nor servname provided`; `gh api user` died the
# same way and the wrapper reported "token expired or revoked — run gh auth login", which
# would have sent Ben to re-authorise a credential that was never broken.
#
# Two lessons are encoded here:
#   1. A dead network and a dead credential are INDISTINGUISHABLE at the call site. Prove
#      the network is up first, so that a later auth failure means what it says.
#   2. A DarkWake fire is early, not doomed — the network usually returns when the machine
#      properly wakes. So wait for it instead of failing instantly.
#
# HOW TO USE
#   . "$SCRIPT_DIR/lib/wait_for_network.sh"
#   hold_system_awake                       # optional but recommended before waiting
#   if ! wait_for_network api.github.com; then
#     write_failure "$(network_failure_message)"
#     exit 75
#   fi

# 5-minute retries for up to 30 minutes -> 7 checks at t=0,5,10,15,20,25,30.
NETWORK_WAIT_MAX_SECS="${NETWORK_WAIT_MAX_SECS:-1800}"
NETWORK_WAIT_INTERVAL="${NETWORK_WAIT_INTERVAL:-300}"

# Callers define their own log(); this is only a fallback so the lib is safe to source
# from a script that has none (run_summary.sh had none).
if ! declare -f log >/dev/null 2>&1; then
  log() { printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >&2; }
fi

# Set by wait_for_network on failure: the hosts still unreachable at the cap.
NETWORK_WAIT_DOWN=""
# Set by wait_for_network on success: seconds spent waiting (0 if it was up immediately).
NETWORK_WAIT_WAITED=0

# Keep the Mac awake for the rest of this job's life.
#
# Without this the gate is close to useless from a DarkWake fire: the maintenance window
# that woke us ends after a few minutes, the machine goes back to sleep, and our `sleep 300`
# stretches past the wall-clock cap without ever getting a live network to test. `-s` holds
# system sleep (effective on AC, which this Mac is), `-i` holds idle sleep, and `-w $$`
# makes caffeinate exit on its own when we do, so there is nothing to clean up.
hold_system_awake() {
  if command -v caffeinate >/dev/null 2>&1; then
    caffeinate -si -w $$ &
    log "holding the system awake for the duration of this run (caffeinate pid $!)"
  else
    log "warning: caffeinate not found — a DarkWake run may sleep through its retries"
  fi
}

# Make a manual run behave like a scheduled one.
#
# launchd starts these jobs with a nearly empty environment. A run started from a Claude Code
# session inherits a large one, and two categories of inherited variable actively break the
# job — which is how `/morning-coffee` re-running a failed triage managed to fail every time
# with an error the scheduled run never sees:
#
#   AWS_BEARER_TOKEN_BEDROCK -> the Anthropic SDK reads it as `api_key`, and `.env` also sets
#       AWS_PROFILE, so the client constructor dies with
#       "ValueError: Cannot specify both `api_key` and AWS credentials".
#   AWS_ACCESS_KEY_ID / SECRET / SESSION_TOKEN -> boto3 prefers explicit env keys over
#       AWS_PROFILE, so a stale copy exported when the shell started SILENTLY OVERRIDES the
#       fresh credentials duo-sso just wrote to the profile. That surfaced as
#       "403 The security token included in the request is expired" moments after a
#       successful refresh — a maximally confusing symptom, since the creds on disk were fine.
#
# So: strip them, and let `.env`'s AWS_PROFILE be the single source of truth. AWS_REGION is
# deliberately left alone — it is not a credential, and the scripts already default it.
scrub_inherited_credentials() {
  local scrubbed=""
  for v in AWS_BEARER_TOKEN_BEDROCK ANTHROPIC_API_KEY ANTHROPIC_AUTH_TOKEN \
           AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN AWS_SECURITY_TOKEN \
           AWS_DEFAULT_PROFILE; do
    # `${!v+x}` tests whether the name is SET, not whether it is non-empty. That distinction
    # is the whole bug: Claude Code exports AWS_BEARER_TOKEN_BEDROCK as an EMPTY string, and
    # `os.environ.get("AWS_BEARER_TOKEN_BEDROCK")` returns `""` — which is not None, so the
    # Anthropic SDK treats it as a supplied api_key and refuses to also take aws_profile. A
    # `-n` test skips it as "not set" and the failure survives the scrub.
    if [ -n "${!v+x}" ]; then
      unset "$v"
      scrubbed="$scrubbed $v"
    fi
  done
  if [ -n "$scrubbed" ]; then
    log "scrubbed inherited credential vars so AWS_PROFILE governs:$scrubbed"
  fi
}

# One host, one verdict. Any HTTP status at all proves DNS resolved AND the TCP+TLS
# handshake completed; curl reports 000 for a DNS failure, a refused connection, or a
# timeout. We deliberately do NOT require a 2xx: a 404 from a bare domain still proves the
# network works, and demanding success would make an unrelated service outage look like a
# dead laptop.
net_host_reachable() {
  local code
  code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "https://$1" 2>/dev/null)"
  [ -n "$code" ] && [ "$code" != "000" ]
}

# wait_for_network HOST [HOST...] -> 0 once every host is reachable, 1 at the cap.
wait_for_network() {
  local waited=0 down host
  NETWORK_WAIT_DOWN=""
  NETWORK_WAIT_WAITED=0

  while :; do
    down=""
    for host in "$@"; do
      net_host_reachable "$host" || down="$down $host"
    done

    if [ -z "$down" ]; then
      NETWORK_WAIT_WAITED="$waited"
      if [ "$waited" -gt 0 ]; then
        log "network is up after waiting ${waited}s — proceeding"
      fi
      return 0
    fi

    if [ "$waited" -ge "$NETWORK_WAIT_MAX_SECS" ]; then
      NETWORK_WAIT_DOWN="$down"
      log "FATAL: still no network after ${waited}s. Unreachable:$down"
      return 1
    fi

    log "no network (unreachable:$down) — waited ${waited}s of ${NETWORK_WAIT_MAX_SECS}s, retrying in ${NETWORK_WAIT_INTERVAL}s"
    sleep "$NETWORK_WAIT_INTERVAL"
    waited=$((waited + NETWORK_WAIT_INTERVAL))
  done
}

# The failure text every caller should use, so the diagnosis stays consistent and — this is
# the point — never blames a credential for a network outage.
network_failure_message() {
  # Report seconds when the window is under a minute, so a short override in a test cannot
  # print the nonsense "0 minutes, checked every 0".
  local window interval
  if [ "$NETWORK_WAIT_MAX_SECS" -ge 60 ]; then window="$((NETWORK_WAIT_MAX_SECS / 60)) minutes"
  else window="${NETWORK_WAIT_MAX_SECS}s"; fi
  if [ "$NETWORK_WAIT_INTERVAL" -ge 60 ]; then interval="$((NETWORK_WAIT_INTERVAL / 60)) minutes"
  else interval="${NETWORK_WAIT_INTERVAL}s"; fi

  printf '%s' "The network was unreachable for the whole retry window — \
$window, checked every $interval. Still unreachable:\`${NETWORK_WAIT_DOWN# }\`.

**Nothing is wrong with your credentials.** The usual cause is that launchd fired this job
during a **DarkWake** — the Mac comes up far enough to run scheduled work but has no
working DNS — and it never fully woke inside the window. Re-run it by hand now that the
machine is awake, or let \`/morning-coffee\` re-run it for you."
}
