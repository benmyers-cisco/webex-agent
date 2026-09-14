#!/bin/bash
# Run GitHub triage. THIS IS THE ONLY ENTRY POINT — both the 8:15am weekday LaunchAgent
# (com.webex-agent.github-triage) and the /github-triage slash command call this script.
#
# It used to be one of two: the crontab line inlined its own copy of this setup with a
# different interpreter and a different auth switch, so the scheduled path and the
# on-demand path were different code. That is why "it works when I run it manually" was
# true for ten days while every scheduled run failed.
#
# The schedule moved off cron entirely on 2026-09-02, for two reasons: cron has no login
# Keychain session (see the auth block below), and cron does not catch up a run it missed
# because the laptop was asleep — it silently skipped roughly half its 8:15am firings.
# launchd runs a StartCalendarInterval job on wake if its time has passed.
#
# No `set -e`: we want the python exit code, not an abort before we can read it.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

log() { printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >&2; }

# Mirrors GITHUB_ORG / MAIN_REPO in github_triage.py — used only for the access probe below.
GITHUB_ORG="oort-dev"
MAIN_REPO="centenario"
OUTPUT_DIR="$PROJECT_DIR/output"

# Which of the two daily runs this is. Mirrors SLOT / FILE_SUFFIX / DEFAULT_LOOKBACK in
# github_triage.py — the wrapper needs it independently because write_failure() below runs
# when python never does, and a pm failure writing to the am filename would erase the
# morning briefing that /morning-coffee reads.
#   am (8:00)  → {date}-github-triage.md,    7-day sweep
#   pm (16:00) → {date}-github-triage-pm.md, 1-day delta
SLOT="${GITHUB_TRIAGE_SLOT:-am}"
case "$SLOT" in
  am) FILE_SUFFIX="-github-triage.md"; DEFAULT_LOOKBACK=7 ;;
  pm) FILE_SUFFIX="-github-triage-pm.md"; DEFAULT_LOOKBACK=1 ;;
  *)  log "warning: unknown GITHUB_TRIAGE_SLOT='$SLOT', treating as am"
      SLOT="am"; FILE_SUFFIX="-github-triage.md"; DEFAULT_LOOKBACK=7 ;;
esac
export GITHUB_TRIAGE_SLOT="$SLOT"

# A health check that aborts before python runs would otherwise write no output file at
# all — and /morning-coffee treats a missing file as "not yet available" and moves on.
# That is the exact silent-failure mode this whole exercise was about, so the wrapper has
# to write the failure report itself for the failures it catches.
# write_failure <body> [<headline>]
#
# The headline is a parameter because it used to be hardcoded to "GitHub auth is not usable",
# and on 2026-09-02 that sent a network outage out under an auth banner. The headline has to
# name the actual cause or the report is worse than no report.
write_failure() {
  mkdir -p "$OUTPUT_DIR"
  local path="$OUTPUT_DIR/$(date '+%Y-%m-%d')$FILE_SUFFIX"
  local headline="${2:-GitHub auth is not usable}"
  {
    printf '# GitHub Triage (%s) — %s\n\n' "$SLOT" "$(date '+%Y-%m-%d %I:%M %p')"
    printf 'Source: `%s` org | Lookback: %s days\n\n---\n\n' \
      "$GITHUB_ORG" "${GITHUB_LOOKBACK_DAYS:-$DEFAULT_LOOKBACK}"
    printf '## ⚠️ Triage did not run — %s\n\n' "$headline"
    printf '%s\n' "$1"
    printf '\nNo GitHub data was fetched. **Do not read this as a quiet day.**\n'
    printf '\nLog: `/tmp/github_triage.log`\n'
  } > "$path"
  log "wrote failure report to $path"
}

# cron runs with a minimal PATH; gh lives in homebrew.
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

# The network gate must come before the auth checks, not after. See the long comment in the
# lib: on 2026-09-02 a DarkWake fire with no DNS made `gh api user` fail, and the auth check
# below reported "token expired or revoked" about a token that was perfectly fine.
# shellcheck source=lib/wait_for_network.sh
. "$SCRIPT_DIR/lib/wait_for_network.sh"

if [ ! -f "$PROJECT_DIR/.env" ]; then
  log "FATAL: $PROJECT_DIR/.env not found. Aborting."
  # This exit was the last silent one in the script — it aborted before write_failure() and
  # so left /morning-coffee to read a missing file as "not yet available" and move on.
  write_failure "\`$PROJECT_DIR/.env\` is missing, so the run aborted before fetching anything.
No GitHub credential belongs in it any more, but the Webex and AWS settings the triage
needs to publish its output do." "the .env file is missing"
  exit 78 # EX_CONFIG
fi
set -a
# shellcheck disable=SC1091
source "$PROJECT_DIR/.env"
set +a

# Must run after `source .env` (so AWS_PROFILE is set) and before the python calls Bedrock.
scrub_inherited_credentials

# Hold the machine awake before waiting on the network — from a DarkWake fire the retry loop
# would otherwise sleep straight through its own window as the maintenance wake expires.
hold_system_awake

# Both hosts, because a run that can reach GitHub but not Bedrock fetches everything and
# then dies at the write step with all the API calls already spent.
#
# Overridable only so the failure path can be exercised in a test; nothing sets it in normal
# operation. Word-splitting is intended.
NETWORK_PROBE_HOSTS="${NETWORK_PROBE_HOSTS:-api.github.com bedrock-runtime.us-east-1.amazonaws.com}"
# shellcheck disable=SC2086
if ! wait_for_network $NETWORK_PROBE_HOSTS; then
  write_failure "$(network_failure_message)" "the network was down, not your credentials"
  exit 75 # EX_TEMPFAIL
fi

# Refresh the AWS credentials BEFORE spending a single GitHub API call.
#
# This line is the fix for 2026-09-04's 08:00 failure. run_summary.sh:96 and run_retro.sh:13
# have always had it and this script never did — and the schedules make that gap fatal
# rather than merely untidy: GitHub triage fires at 08:00, the Webex triage that refreshes
# the token fires at 08:30. So this job ran half an hour before the only thing left that
# renewed credentials, fetched 33 discussions and 13 issues, and then died on
# `403 The security token included in the request is expired` with every API call spent.
#
# Same divergent-setup failure class as the two-entry-points comment at the top of this file:
# three scripts needed the same prelude and only two of them had it.
AWS_REFRESH_LOG=/dev/stderr AWS_REFRESH_THRESHOLD=7200 \
  bash "$SCRIPT_DIR/aws_refresh.sh" || \
  log "warning: the AWS refresh did not succeed — the credential probe below is the real gate"

# Prove the CREDENTIAL, not just that Bedrock's hostname answers a handshake.
#
# NETWORK_PROBE_HOSTS above already includes bedrock-runtime for exactly the reason its
# comment gives — "a run that can reach GitHub but not Bedrock fetches everything and then
# dies at the write step with all the API calls already spent". But reachability is not
# authentication, so on 2026-09-04 that probe passed cleanly and the run died at the write
# step anyway. Nothing caches the fetch, so the whole sweep was thrown away.
#
# sts:GetCallerIdentity is the right question to ask: it costs nothing, it cannot be denied
# by IAM policy, and it answers precisely "will Bedrock accept this token?". It runs after
# scrub_inherited_credentials so AWS_PROFILE is the single source of truth, and after the
# network gate so a failure here means the credential and not the DNS.
AWS_PROBE="$("$PROJECT_DIR/.venv/bin/python3" -c '
import os, sys
try:
    import boto3, botocore.exceptions
except Exception as e:
    print(f"probe unavailable: {e}"); sys.exit(2)
try:
    # Region is explicit: with no ~/.aws/config on this machine boto3 would otherwise raise
    # NoRegionError, and a broken probe must not be mistaken for a broken credential.
    boto3.client("sts", region_name=os.environ.get("AWS_REGION") or "us-east-1") \
         .get_caller_identity()
except botocore.exceptions.ClientError as e:
    print(e.response.get("Error", {}).get("Code", "ClientError")); sys.exit(1)
except Exception as e:
    print(f"probe unavailable: {type(e).__name__}: {e}"); sys.exit(2)
print("ok")
' 2>&1)"
AWS_PROBE_CODE=$?

case "$AWS_PROBE_CODE" in
  0) log "AWS credentials accepted by STS" ;;
  # Exit 2 is the probe itself failing — a missing venv, no boto3, an import error. That is
  # not evidence about the credential, so it must not block a run that might well succeed.
  2) log "warning: could not run the AWS credential probe ($AWS_PROBE) — proceeding anyway" ;;
  *)
    log "FATAL: STS rejected the AWS credentials ($AWS_PROBE)."
    log "       Aborting BEFORE the fetch so the GitHub API calls are not wasted."
    write_failure "STS rejected the credentials in profile \`${AWS_PROFILE:-unset}\` with
\`$AWS_PROBE\`, so the Bedrock summary step would have failed after spending the whole
GitHub fetch. **Nothing was fetched** — this is the cheap failure, on purpose.

\`bedrock-runtime\` answered its reachability check moments earlier, so **this is the
credential, not the network**. The hourly refresher
(\`com.webex-agent.aws-refresh\`) should have prevented it; check that it is not wedged:

\`\`\`
launchctl list | grep aws-refresh
tail -5 /tmp/webex-aws-refresh.log
\`\`\`

Then refresh by hand and re-run:

\`\`\`
duo-sso -profile claudecode
bash scripts/run_github_triage.sh
\`\`\`" "the AWS credentials are expired"
    exit 75 # EX_TEMPFAIL — transient by nature; the next scheduled run should be fine
    ;;
esac

export GH_CONFIG_DIR="$HOME/.config/gh"

# Auth comes from the Keychain, and this check must run BEFORE any API call.
#
# gh keeps its OAuth tokens in the macOS Keychain. A Keychain read needs a login session,
# which is why this job is a LaunchAgent and not a cron line: cron gets no Keychain
# session, so gh silently fell back to anonymous requests — 401 on REST, an IP-based rate
# limit on GraphQL — and the old code turned both into empty lists. A LaunchAgent runs
# inside the GUI session, so the keyring is readable.
#
# The fine-grained PAT this used to require is GONE (dropped 2026-09-02). It sat pending
# org-owner approval for five days and was never needed: the existing gho_ OAuth token
# already reads everything the job touches. Nothing in .env is consulted for auth now, so
# no GitHub credential lives on disk.
#
# `gh auth token --user` is doing real work here. It reads the Keychain for ONE named
# account and hands the token to this process only:
#   - pinned account. hosts.yml holds both benmyers-cisco and benxmy, and only the Cisco
#     account can see oort-dev. Relying on whichever is "active" means the job breaks the
#     next time Ben switches accounts in a terminal, with only 404s to explain it.
#   - no global mutation. The `gh auth switch` pair this replaces wrote to
#     ~/.config/gh/hosts.yml, which every interactive shell shares, and the trailing
#     switch hardcoded benxmy rather than restoring what had been active — so the old cron
#     flipped Ben's active account out from under him twice a day.
GH_ACCOUNT="benmyers-cisco"

# Clear any inherited GH_TOKEN first: gh prefers the environment over the keyring, so a
# stale one in the ambient shell would be echoed straight back by `gh auth token` and the
# --user pin would be silently ignored.
unset GH_TOKEN GITHUB_TOKEN

if ! GH_TOKEN="$(gh auth token --user "$GH_ACCOUNT" 2>/dev/null)" || [ -z "$GH_TOKEN" ]; then
  log "FATAL: could not read a Keychain token for '$GH_ACCOUNT'."
  log "       Either gh is not logged in as that account, or this process has no login"
  log "       Keychain session (the classic symptom of running from cron, not launchd)."
  write_failure "\`gh\` could not read a Keychain token for \`$GH_ACCOUNT\`.

Two causes: that account is not logged in (check \`gh auth status\`, re-run
\`gh auth login\` if needed), or this process has no login Keychain session — the classic
symptom of being run from **cron instead of the LaunchAgent**
(\`com.webex-agent.github-triage\`). Keychain reads need a GUI session."
  exit 78 # EX_CONFIG
fi
export GH_TOKEN

WHOAMI="$(gh api user --jq .login 2>/dev/null || echo unknown)"
if [ "$WHOAMI" = "unknown" ]; then
  log "FATAL: got a token for $GH_ACCOUNT but cannot read /user — expired or revoked."
  write_failure "The Keychain token for \`$GH_ACCOUNT\` was read but cannot call \`/user\`.

\`api.github.com\` answered the reachability check moments before this call, so **this is
the credential, not the network** — most likely expired or revoked by an SSO re-auth. Run
\`gh auth login\` to refresh it." "the GitHub token is expired or revoked"
  exit 77 # EX_NOPERM
fi

# Authenticating is NOT the same as being able to read the sources, and the difference is
# not academic: the dropped PAT authenticated perfectly, returned a valid login from
# /user, and then 404d on every single oort-dev repo. Probe the actual targets instead of
# trusting the handshake.
#
# Both repos are probed because they are in DIFFERENT ORGS — oort-dev and cisco-sbg carry
# independent SAML/SSO grants, so one can lapse while the other keeps working. A probe that
# only checked centenario would pass cleanly while every ID-fabric discussion silently
# vanished from the briefing.
#
# Policy on a partial failure: proceed if ANY source is readable, and let github_triage.py
# stamp the file with its ⚠️ FAILED banner for the source that broke. Aborting the whole run
# would throw away the readable repo's data to report the unreadable one, and the python
# layer already records per-call failures loudly. Only a total loss exits 77.
PROBE_TARGETS="$GITHUB_ORG/$MAIN_REPO cisco-sbg/ID-fabric"
READABLE=0
UNREADABLE=""
for TARGET in $PROBE_TARGETS; do
  if gh api "repos/$TARGET" --jq .full_name >/dev/null 2>&1; then
    READABLE=$((READABLE + 1))
  else
    UNREADABLE="$UNREADABLE $TARGET"
    log "WARNING: authenticated as $WHOAMI but cannot read $TARGET — its org grant may have lapsed."
  fi
done

if [ "$READABLE" -eq 0 ]; then
  log "FATAL: cannot read any source repo ($PROBE_TARGETS)."
  log "       The token is valid but not authorised for either org. Aborting before"
  log "       wasting API calls."
  write_failure "Authenticated as \`$WHOAMI\` but **cannot read any source repo** —
\`$PROBE_TARGETS\`. The token is valid but not authorised for either org.

A private repo reads as 404, not 403, to a caller that cannot see it. The network passed its
reachability check and \`/user\` answered, so this is authorisation: most likely the SAML/SSO
grants have lapsed on this token. Re-run \`gh auth login\` and re-authorise both
\`$GITHUB_ORG\` and \`cisco-sbg\`." "the token is valid but not authorised for either org"
  exit 77 # EX_NOPERM
fi

if [ -n "$UNREADABLE" ]; then
  log "proceeding with a PARTIAL run — unreadable:$UNREADABLE"
fi
log "gh authenticated as $WHOAMI, $READABLE/2 source repos readable"
log "starting $SLOT triage (lookback ${GITHUB_LOOKBACK_DAYS:-$DEFAULT_LOOKBACK} days, writing *$FILE_SUFFIX)"

"$PROJECT_DIR/.venv/bin/python3" "$SCRIPT_DIR/github_triage.py"
CODE=$?

log "github_triage.py exited $CODE"
exit $CODE
