#!/bin/bash
# Wrapper for the hourly pulse LaunchAgent and manual runs.
#
# Mirrors run_summary.sh deliberately: prove the network before blaming a
# credential, refresh AWS through the timeout-bounded helper rather than calling
# duo-sso inline, and always leave an artifact behind. A scheduled run that
# writes nothing is indistinguishable from a quiet hour.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
OUTPUT_DIR="$PROJECT_DIR/output"
# Overridable only so the ruling-37 freshness test can force hourly_pulse.py to
# fail without writing its own artifact. Nothing sets this in normal operation.
PYTHON="${PYTHON:-$PROJECT_DIR/.venv/bin/python3.12}"

log() { printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >&2; }

export PATH="/opt/homebrew/bin:/opt/homebrew/opt/python@3.12/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

# The pulse's own failure artifact, in the same shape hourly_pulse.py uses, so
# the Hub needs no special case.
#
# Two departures from a naive re-implementation:
#  - Imports failure_payload from the project's own scripts/lib/pulse_output.py
#    rather than re-declaring the JSON shape inline, so the two writers cannot
#    drift out of sync with each other. The day is taken from the LOCAL clock
#    (datetime.now().astimezone()), not UTC, because archive_if_new_day
#    compares against a locally-derived `today` — a naive UTC stamp would tag
#    an evening failure after 20:00 EDT with tomorrow's date.
#  - Renders to pulse.json.tmp and mv's into place only once python exits 0,
#    rather than redirecting python's stdout straight at pulse.json. A plain
#    `> pulse.json` truncates the target the instant the shell opens it, before
#    python runs at all — so if python itself fails to import or write, the
#    artifact left behind is a destroyed, zero-byte file instead of whatever
#    was there before.
#
# Takes project_dir as an argument (rather than reading $PROJECT_DIR directly)
# because this can fire before this script's own `cd "$PROJECT_DIR"` — the
# missing-.env path below runs before that point.
write_failure() {
  local reason="$1" project_dir="$2"
  mkdir -p "$OUTPUT_DIR"
  local tmp="$OUTPUT_DIR/pulse.json.tmp"
  if "$PYTHON" - "$reason" "$project_dir" <<'PY' > "$tmp"
import json, sys
from datetime import datetime

sys.path.insert(0, sys.argv[2] + "/scripts")
from lib.pulse_output import failure_payload  # noqa: E402

now = datetime.now().astimezone()
payload = failure_payload(sys.argv[1], now.isoformat(), day=now.strftime("%Y-%m-%d"))
json.dump(payload, sys.stdout, indent=2)
PY
  then
    mv -f "$tmp" "$OUTPUT_DIR/pulse.json"
    log "wrote failure artifact to $OUTPUT_DIR/pulse.json"
  else
    rm -f "$tmp"
    log "FATAL: could not even render the failure artifact (python failed to import or run) — leaving any existing pulse.json alone rather than truncate it"
  fi
}

if [ ! -f "$PROJECT_DIR/.env" ]; then
  log "FATAL: $PROJECT_DIR/.env not found."
  write_failure "$PROJECT_DIR/.env is missing, so the run aborted before fetching anything." "$PROJECT_DIR"
  exit 78 # EX_CONFIG
fi
set -a
# shellcheck disable=SC1091
source "$PROJECT_DIR/.env"
set +a

# shellcheck source=lib/wait_for_network.sh
. "$SCRIPT_DIR/lib/wait_for_network.sh"
scrub_inherited_credentials
hold_system_awake

NETWORK_PROBE_HOSTS="${NETWORK_PROBE_HOSTS:-webexapis.com bedrock-runtime.us-east-1.amazonaws.com}"
# shellcheck disable=SC2086
if ! wait_for_network $NETWORK_PROBE_HOSTS; then
  write_failure "$(network_failure_message)" "$PROJECT_DIR"
  exit 75 # EX_TEMPFAIL
fi

AWS_REFRESH_LOG=/dev/stderr AWS_REFRESH_THRESHOLD=7200 \
  bash "$SCRIPT_DIR/aws_refresh.sh" >> /tmp/webex-pulse.log 2>&1 || true

cd "$PROJECT_DIR"
log "starting hourly pulse"

# Captured before invoking python (ruling 37): this is the anchor for deciding,
# after a nonzero exit, whether pulse.json on disk is THIS run's own artifact
# or a stale one left over from an earlier successful run.
START=$(date +%s)

"$PYTHON" scripts/hourly_pulse.py >> /tmp/webex-pulse.log 2>&1
CODE=$?
log "hourly_pulse.py exited $CODE"

if [ "$CODE" -eq 0 ]; then
  exit 0
fi

# hourly_pulse.py writes its own failure artifact on a handled error (rule 1 in
# its module docstring: any escaping exception still writes status="failed").
# The question here is NOT "does pulse.json exist" — a `grep -q '"status"'`
# style check tests existence, not freshness, and a stale artifact from last
# hour's successful run also contains "status". If hourly_pulse.py dies before
# writing anything at all (bad import, OOM, a hard kill), that check would find
# the OLD file, conclude nothing needs doing, and Ben would read last hour's
# successful pulse as the current hour — a false positive, which is worse than
# silence and the exact thing this whole design exists to prevent.
#
# So: compare the artifact's mtime against START. Only treat it as this run's
# own if it was written at or after the moment python was invoked.
#
# `stat -f %m` (BSD/macOS stat) rather than `test -nt`: `-nt` compares mtimes at
# one-second granularity on many systems, so a stale marker and a fresh
# artifact written in the same second compare equal, and that tie resolves
# toward destroying a good artifact. `stat -f %m` needs no working Python
# interpreter to evaluate, which matters because this runs exactly when python
# has just exited nonzero. `|| echo 0` covers the artifact being entirely
# absent, which must also take the write-a-failure branch.
ART_MTIME=$(stat -f %m "$OUTPUT_DIR/pulse.json" 2>/dev/null || echo 0)
if [ "$ART_MTIME" -lt "$START" ]; then
  write_failure "hourly_pulse.py exited $CODE before writing an artifact. The network
passed its reachability check first, so this is not connectivity — the usual cause is
expired AWS credentials. Run: duo-sso -profile claudecode" "$PROJECT_DIR"
fi

exit $CODE
