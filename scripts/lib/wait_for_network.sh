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
# AND THE LIMIT OF THOSE LESSONS, learned 2026-09-17
# --------------------------------------------------
# "Wait for it" only works if the machine wakes inside the window. That day it did not:
#
#   09-17 02:00-09:07  DarkWake / Deep Idle, no DNS the whole time
#   09-17 09:07:53     DarkWake to FullWake, due to HID Activity   <- Ben sat down
#
# All three morning jobs burned their full 30 minutes in the dark (06:35, 06:45, 07:20) and gave
# up 67 minutes before the network came back. No value of NETWORK_WAIT_MAX_SECS fixes that,
# because StartCalendarInterval never re-fires a failed occurrence — so the fix cannot live in
# this file. It lives in run_morning_coffee.sh's --catchup mode, which re-checks every 15 minutes
# until 10:45. Callers of this gate should assume a hard failure here may just mean "not yet".
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
#
# What it does NOT do — and 2026-09-17 is the proof — is promote a DarkWake into a FullWake. It
# holds whatever wake state we are already in, so from a dark fire it faithfully holds us in the
# dark, with no DNS, for the entire retry window. `caffeinate -u` (a user assertion) or a
# `sudo pmset repeat wakeorpoweron` schedule are the levers that produce a real wake.
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

# Capture, at the moment of failure, the three facts that separate the two explanations curl
# cannot tell apart. Added 2026-09-17 because after that morning there was no way to settle
# whether the Mac's network stack was never up (DarkWake) or the house simply had no internet —
# every probe returns the same 000 either way, and both stories fit the evidence equally well.
#
#   no IP / gateway unreachable        -> this Mac's network was not up. DarkWake, dock asleep,
#                                         Wi-Fi not associated.
#   gateway up, resolver unreachable   -> the LAN is fine and name resolution is not. On THIS Mac
#                                         that is more likely Cisco Secure Client than the ISP —
#                                         see net_secure_client_state below.
#   resolver answers, hosts still down -> transit or a real service outage. Not this machine.
#
# Everything here is bounded to ~2s per probe and runs only on the failure path, so it costs
# nothing on a normal morning.

# The 2026-09-17 outage turned out to be a **VPN/Secure Client problem on the laptop**, which is
# a case the gateway/resolver split above gets actively WRONG: it would have reported "the LAN is
# up and the upstream is not — ISP or router," sending Ben to reboot a router that was fine.
#
# The reason is that on this Mac the resolver IS the corporate stack. Nameservers are Cisco
# Umbrella (208.67.222.222/220.220) because `acumbrellaagent` puts them there, and `csc_swgagent`
# and `csc_zta_agent` carry web and zero-trust access. There is usually no `utun` at all — this is
# not a classic AnyConnect tunnel — so "is a tunnel up?" is the wrong question. The right one is
# whether those agents are alive, because when they are not, name resolution dies while the LAN
# stays perfectly healthy. Identical symptom, completely different fix.
net_secure_client_state() {
  local agents="" p utun
  for p in acumbrellaagent csc_swgagent csc_zta_agent vpnagentd; do
    pgrep -f "$p" >/dev/null 2>&1 && agents="$agents $p"
  done
  utun="$(ifconfig 2>/dev/null | awk '/^utun/{i=$1} /inet /{if(i!=""){print i; i=""}}' | paste -sd, - )"
  printf 'Secure Client agents running:%s. Tunnel interfaces: %s.' \
    "${agents:- NONE}" "${utun:-none (normal here — Umbrella/ZTA, not a tunnel)}"
}
net_diagnose() {
  local iface gw ip res gw_ok="no" res_ok="no" resolved=""

  iface="$(route -n get default 2>/dev/null | awk '/interface:/{print $2}')"
  gw="$(route -n get default 2>/dev/null | awk '/gateway:/{print $2}')"
  res="$(scutil --dns 2>/dev/null | awk '/nameserver\[0\]/{print $3; exit}')"
  [ -n "$iface" ] && ip="$(ipconfig getifaddr "$iface" 2>/dev/null)" || ip=""

  [ -n "$gw" ] && ping -c1 -t2 "$gw" >/dev/null 2>&1 && gw_ok="yes"
  [ -n "$res" ] && nc -z -G2 -w2 "$res" 53 >/dev/null 2>&1 && res_ok="yes"
  [ -n "$res" ] && resolved="$(dig +short +time=2 +tries=1 "@$res" apple.com 2>/dev/null | head -1)"

  # The verdict, stated plainly, because the point of this block is to stop the next person
  # guessing — including the next me.
  local verdict
  if [ -z "$ip" ] || [ "$gw_ok" = no ]; then
    verdict="**this Mac had no working network at all** — no route off the machine. DarkWake, a \
sleeping dock, or Wi-Fi not associated. Not the internet."
  elif [ "$res_ok" = no ]; then
    verdict="**the LAN was up and name resolution was not** — the gateway answered and the \
resolver did not. On this Mac the resolver is Cisco's own stack, so **check Secure Client before \
the ISP**; 2026-09-17 was exactly this and it was a VPN fault, not the internet."
  elif [ -z "$resolved" ]; then
    verdict="**DNS was reachable but not answering** — resolver accepted TCP/53 and returned no \
record. Umbrella-side, a captive portal, or a half-connected Secure Client."
  else
    verdict="**name resolution worked and the hosts still did not** — transit or a genuine \
service outage upstream. Nothing to fix here."
  fi

  printf 'At the moment it gave up: interface `%s`, address `%s`, gateway `%s` (%s), resolver `%s` (tcp/53 %s, resolved apple.com to `%s`). %s\n\nDiagnosis: %s\n' \
    "${iface:-none}" "${ip:-none}" "${gw:-none}" "$gw_ok" "${res:-none}" "$res_ok" \
    "${resolved:-nothing}" "$(net_secure_client_state)" "$verdict"
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

**Nothing is wrong with your credentials.** Two things look identical from here — the Mac never
came properly awake (launchd fires jobs during **DarkWake**, which may not have working DNS), or
there was simply no internet. So rather than guess:

$(net_diagnose)"
}
