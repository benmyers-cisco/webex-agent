#!/usr/bin/env python3
"""GitHub triage — fetches discussions, issues, and mentions from cisco-sbg, scores relevance, outputs triage markdown."""

import json
import os
import subprocess
import sys
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

import anthropic

OUTPUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "output")
GITHUB_USER = "benmyers-cisco"

# The main board was renamed AND moved orgs: `oort-dev/centenario` is now
# `cisco-sbg/ID-fabric-core`. The three GitHub APIs disagree about whether the old name
# still works, which is why this broke in two different ways at once:
#
#   REST     follows the rename redirect  -> still worked, including the wrapper's health probe
#   GraphQL  follows the rename redirect  -> still worked, so discussions kept arriving
#   Search   does NOT follow it           -> broke
#
# So `gh search issues --repo=oort-dev/centenario` failed loudly ("the listed users and
# repositories cannot be searched"), while anything scoped to `org:oort-dev` returned an
# empty list with no error at all — the repo simply is not in that org anymore. The silent
# half is the dangerous one: it made "assigned to Ben" and "mentions Ben" read as a quiet
# week. Never infer from a working REST call that a search will resolve the same name.
MAIN_OWNER = "cisco-sbg"
MAIN_REPO = "ID-fabric-core"

# Org for the account-wide searches (assigned-to-Ben, mentions-anywhere). One org still
# covers both boards now that the main one moved — and it reaches ID-fabric as a bonus,
# which `org:oort-dev` never could.
SEARCH_ORG = "cisco-sbg"

# docs-gitbook did NOT move; it is still in oort-dev. It keeps its own constants rather than
# riding on a shared org name that would silently retarget it to a repo that does not exist.
DOCS_OWNER = "oort-dev"
DOCS_REPO = "docs-gitbook"

# Discussions are pulled from both boards. They are now in the same org, so the issue and
# mention searches below reach both — before the move they were oort-dev-only and could not
# see ID-fabric at all.
#
# `bonus` is a per-source relevance floor, and it exists for the same reason labels score:
# the container is itself a topic declaration. Every discussion in ID-fabric is about
# Identity Fabric by construction, but its titles assume that context and say things like
# "SessionState Storage and Lifecycle" — no keyword in RELEVANT_KEYWORDS appears, so without
# a floor the most relevant repo on the board would score near zero and be filtered out.
DISCUSSION_SOURCES = [
    {"owner": MAIN_OWNER, "repo": MAIN_REPO, "bonus": 0},
    {"owner": "cisco-sbg", "repo": "ID-fabric", "bonus": 3},
]
GH_ACCOUNT = "benmyers-cisco"

# Two scheduled runs a day, and they are not the same briefing:
#   am (8:00) — 7-day sweep, written to {date}-github-triage.md, read by /morning-coffee
#   pm (16:00) — 1-day delta, written to {date}-github-triage-pm.md, read by /wrap-up
# Separate filenames because a single {date}-github-triage.md would mean the 4pm run erased
# the morning briefing every day, and /morning-coffee reads that exact name.
SLOT = os.environ.get("GITHUB_TRIAGE_SLOT", "am").lower()
if SLOT not in ("am", "pm"):
    log_slot_warning = f"unknown GITHUB_TRIAGE_SLOT={SLOT!r}, treating as 'am'"
    SLOT = "am"
else:
    log_slot_warning = ""

FILE_SUFFIX = "-github-triage.md" if SLOT == "am" else "-github-triage-pm.md"
DEFAULT_LOOKBACK = 7 if SLOT == "am" else 1
# An explicit GITHUB_LOOKBACK_DAYS always wins — that is what /github-triage passes.
LOOKBACK_DAYS = int(os.environ.get("GITHUB_LOOKBACK_DAYS") or DEFAULT_LOOKBACK)

# Every gh/GraphQL failure is recorded here. Without this, a failed call and a genuinely
# empty result are the same value ("" or {}) by the time they reach main(), which is how
# ten days of 401s read as "quiet week" to /morning-coffee.
FETCH_ERRORS: list[str] = []


def log(msg: str) -> None:
    """Timestamped stderr line. cron appends stderr to /tmp/github_triage.log."""
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}", file=sys.stderr, flush=True)

# Announcements is the release-notes bot and is dropped outright. Everything else is kept,
# so this is the full category list for reference rather than a filter — the old
# WATCHED_DISCUSSIONS constant was never read by any code path, which made it look like a
# filter that existed.
SKIP_DISCUSSION_CATEGORIES = {"Announcements"}

# Every one of these is searched. Until 2026-09-02 the loop below only walked the first
# three, so MMF issues reached the briefing solely by being assigned to Ben — a label that
# is declared but not searched is worse than no label list at all.
RELEVANT_LABELS = ["fabric", "MMF", "NHI", "AI", "C3", "FY27 Planning", "Agent Security App"]

# Minimum score to reach the briefing. The prompt used to assert this threshold while
# score_relevance() was never called, so nothing was ever actually filtered.
MIN_SCORE = 3

RELEVANT_KEYWORDS = [
    "identity fabric", "CUI", "CII", "cloud control", "duo",
    "entitlement", "provisioning", "trust level", "admin access",
    "astrix", "NHI", "non-human identity", "agent security",
    "meraki", "access manager", "SCIM", "kafka",
]

PROJECT_MAP = {
    "Identity Fabric": ["identity fabric", "fabric", "id fabric", "idf"],
    "CUI Data into Identity Fabric": ["CUI", "CUI data", "cui integration", "cui tenant"],
    "CII Adoption": ["CII", "identity intelligence", "trust level", "admin access", "entitlement"],
    "HMF POC": ["HMF", "hybrid mesh", "firewall", "secure access"],
    "Meraki+Duo": ["meraki", "access manager"],
    "Astrix/NHI": ["astrix", "NHI", "non-human identity", "agent security", "agent inventory"],
}


def _gh_env() -> dict:
    env = os.environ.copy()
    env["GH_CONFIG_DIR"] = os.path.expanduser("~/.config/gh")
    # run_github_triage.sh reads the Keychain token for one named account and exports GH_TOKEN
    # before invoking this script, so normally it is already here. If this script is run
    # directly, fall back to reading the same account's token the same way — matching how the
    # wrapper authenticates rather than letting gh pick whichever account happens to be active
    # (that kind of divergence between the scheduled and on-demand paths hid this bug for ten
    # days). The result stays in the dict handed to the subprocess; it never touches a shell.
    #
    # There is deliberately no GITHUB_TRIAGE_TOKEN branch here any more. That fine-grained PAT
    # was dropped 2026-09-02 as unnecessary — the Keychain OAuth token already reads everything
    # this job touches — and a dead fallback for a retired credential is just a second auth
    # path waiting to diverge from the first. No GitHub credential is on disk.
    if not env.get("GH_TOKEN"):
        try:
            token = subprocess.run(
                ["gh", "auth", "token", "--user", GH_ACCOUNT],
                capture_output=True, text=True, timeout=15,
            ).stdout.strip()
            if token:
                env["GH_TOKEN"] = token
        except (subprocess.SubprocessError, OSError):
            pass  # leave GH_TOKEN unset; the callers record the auth failure loudly
    return env


def run_gh(args: list[str], account: str = GH_ACCOUNT) -> str:
    """Run a gh CLI command and return stdout. Records failures in FETCH_ERRORS."""
    result = subprocess.run(
        ["gh"] + args,
        capture_output=True, text=True, env=_gh_env(), timeout=30
    )
    if result.returncode != 0:
        err = result.stderr.strip()[:200]
        FETCH_ERRORS.append(f"gh {' '.join(args[:3])}: {err}")
        log(f"gh error: {err}")
        return ""
    return result.stdout


def run_gh_graphql(query: str) -> dict:
    """Run a GraphQL query via gh api graphql. Records failures in FETCH_ERRORS."""
    # _gh_env() here too: run_gh set GH_CONFIG_DIR and this path did not, so the two
    # halves of the same job resolved gh config differently.
    result = subprocess.run(
        ["gh", "api", "graphql", "-f", f"query={query}"],
        capture_output=True, text=True, env=_gh_env(), timeout=30
    )
    if result.returncode != 0:
        err = result.stderr.strip()[:200]
        FETCH_ERRORS.append(f"graphql: {err}")
        log(f"graphql error: {err}")
        return {}
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        FETCH_ERRORS.append(f"graphql: unparseable response — {exc}")
        log(f"graphql error: unparseable response — {exc}")
        return {}


def fetch_recent_discussions(since_days: int = LOOKBACK_DAYS) -> list[dict]:
    """Fetch discussions from every source in DISCUSSION_SOURCES, newest first."""
    out: list[dict] = []
    for src in DISCUSSION_SOURCES:
        found = fetch_repo_discussions(src["owner"], src["repo"], since_days)
        log(f"    {src['owner']}/{src['repo']}: {len(found)} in window")
        for d in found:
            # Carried through to scoring and to the prompt — with two repos in play, "#390"
            # alone is ambiguous and the reader cannot tell which board it is on.
            d["repo"] = f"{src['owner']}/{src['repo']}"
            d["source_bonus"] = src["bonus"]
        out.extend(found)
    return sorted(out, key=lambda d: d.get("updatedAt", ""), reverse=True)


def fetch_repo_discussions(owner: str, repo: str, since_days: int = LOOKBACK_DAYS) -> list[dict]:
    """Fetch one repo's discussions updated within the window, paging until past the cutoff.

    This used to ask for the 25 newest and stop. Because the Announcements release-notes bot
    posts several times a week and its posts are dropped only AFTER the fetch, bot activity
    consumed slots in that window of 25 — so on a busy week real discussions fell off the end
    and were never seen. The repo holds 221 discussions, so the truncation was silent and
    unbounded. Ordering by UPDATED_AT descending means the first page past the cutoff ends the
    walk, and PAGE_CAP is only a runaway guard.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=since_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    PAGE_CAP = 5  # 5 × 50 = 250, more than ID-fabric-core's 224 or ID-fabric's 34 currently hold
    kept: list[dict] = []
    after = "null"

    for _ in range(PAGE_CAP):
        query = """
{
  repository(owner: "%s", name: "%s") {
    discussions(first: 50, after: %s, orderBy: {field: UPDATED_AT, direction: DESC}) {
      pageInfo { hasNextPage endCursor }
      nodes {
        number
        title
        url
        updatedAt
        createdAt
        closed
        author { login }
        category { name }
        body
        comments(last: 5) {
          totalCount
          nodes {
            author { login }
            body
            createdAt
          }
        }
      }
    }
  }
}""" % (owner, repo, after)
        data = run_gh_graphql(query)
        if not data:
            return kept  # the error is already in FETCH_ERRORS; main() will flag the run

        block = data.get("data", {}).get("repository", {}).get("discussions", {}) or {}
        nodes = block.get("nodes", []) or []
        if not nodes:
            break

        for d in nodes:
            if d.get("updatedAt", "") < cutoff:
                return kept  # sorted desc, so everything after this is older too
            if (d.get("category") or {}).get("name") in SKIP_DISCUSSION_CATEGORIES:
                continue
            kept.append(d)

        page = block.get("pageInfo", {}) or {}
        if not page.get("hasNextPage"):
            break
        after = '"%s"' % page["endCursor"]
    else:
        log(f"  note: hit the {PAGE_CAP}-page cap on {owner}/{repo}; older in-window items may be missed")

    return kept


# body is requested so score_relevance() has something to read. Without it every issue was
# scored on its title alone, which is why the scoring looked useless enough to leave unwired.
# state is requested so drop_closed() has something to check — without it a fetcher that
# forgets --state=open cannot be caught downstream.
ISSUE_JSON_FIELDS = "number,title,url,updatedAt,author,labels,assignees,body,commentsCount,state"


def fetch_relevant_issues(since_days: int = LOOKBACK_DAYS) -> list[dict]:
    """Fetch recently updated issues by label, assignment, or mention.

    Each issue carries a `sources` set naming which query found it. That provenance is what
    lets score_relevance() credit "assigned to Ben" — the old scorer only looked for his
    username in the TEXT, so an issue assigned to him whose title happened to miss every
    keyword scored 0.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=since_days)).strftime("%Y-%m-%d")

    issues: list[dict] = []

    def collect(args: list[str], source: str) -> None:
        raw = run_gh(["search", "issues"] + args + ["--json", ISSUE_JSON_FIELDS])
        if not raw:
            return
        try:
            found = json.loads(raw)
        except json.JSONDecodeError as exc:
            FETCH_ERRORS.append(f"issues/{source}: unparseable response — {exc}")
            log(f"issues error ({source}): unparseable response — {exc}")
            return
        for item in found:
            item["source"] = source
            issues.append(item)

    # Every label in RELEVANT_LABELS, not just the first three. --label is used rather than a
    # raw `label:x` qualifier because two of these contain spaces ("FY27 Planning", "Agent
    # Security App") and would not survive as a bare qualifier.
    for label in RELEVANT_LABELS:
        collect([
            f"--repo={MAIN_OWNER}/{MAIN_REPO}",
            f"--label={label}",
            "--state=open",
            f"--updated=>={cutoff}",
            "--limit", "10",
        ], f"label:{label}")

    # Issues assigned to Ben. This query had NO date bound, so it returned every open issue
    # ever assigned to him — which is how a July 2025 and a March 2025 issue led the briefing
    # as "needs your attention". Bounded now: an assigned issue surfaces when it MOVES.
    collect([
        f"--assignee={GITHUB_USER}",
        f"--owner={SEARCH_ORG}",
        "--state=open",
        f"--updated=>={cutoff}",
        "--limit", "15",
    ], "assigned")

    # Issues mentioning Ben. This deliberately included closed ones on the theory that a
    # question aimed at him on a closed issue still needs an answer. In practice it did the
    # opposite: on 2026-09-15 three of the five items leading "Needs Your Input" were closed
    # — #19048 closed in December 2024, #23036 in July 2025 — because a late comment or a
    # label change moves an ancient issue's updatedAt into the window, and the briefing then
    # asked Ben to act on work that was already finished or explicitly not planned.
    collect([
        f"--repo={MAIN_OWNER}/{MAIN_REPO}",
        f"--mentions={GITHUB_USER}",
        "--state=open",
        f"--updated=>={cutoff}",
        "--limit", "10",
    ], "mentioned")

    # Dedup by number, merging provenance: an issue that is both assigned AND label:fabric
    # should score for both.
    by_number: dict[int, dict] = {}
    for issue in issues:
        num = issue.get("number")
        if not num:
            continue
        if num in by_number:
            by_number[num].setdefault("sources", set()).add(issue["source"])
        else:
            issue["sources"] = {issue["source"]}
            by_number[num] = issue

    return list(by_number.values())


def fetch_mentions_and_notifications() -> list[dict]:
    """Check for recent @mentions in comments across the org, both boards."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%d")

    raw = run_gh([
        "search", "issues",
        f"org:{SEARCH_ORG}",
        f"mentions:{GITHUB_USER}",
        f"updated:>={cutoff}",
        "--state=open",
        "--limit", "15",
        "--json", "number,title,url,updatedAt,author,repository,state"
    ])
    if raw:
        return json.loads(raw)
    return []


def fetch_docs_changes(since_days: int = LOOKBACK_DAYS) -> list[dict]:
    """Fetch recent commits to docs-gitbook."""
    raw = run_gh([
        "api", f"repos/{DOCS_OWNER}/{DOCS_REPO}/commits",
        "--jq", f'[.[] | select(.commit.author.date > "{(datetime.now(timezone.utc) - timedelta(days=since_days)).strftime("%Y-%m-%dT%H:%M:%SZ")}") | {{sha: .sha, message: .commit.message, date: .commit.author.date, author: .commit.author.name}}]'
    ])
    if raw:
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            FETCH_ERRORS.append(f"docs-gitbook commits: unparseable response — {exc}")
            log(f"docs error: unparseable response — {exc}")
    return []


def detect_mention_signals(item: dict) -> dict:
    """Check whether Ben is @mentioned by others vs. merely a participant.

    Returns {directly_mentioned: bool, user_commented: bool} so Claude can
    distinguish "someone asked for Ben's input" from "Ben already weighed in
    on a topically relevant thread."
    """
    user_lc = GITHUB_USER.lower()
    mention_needles = [f"@{user_lc}", "@benmyers"]

    user_commented = False
    directly_mentioned = False

    # Check OP body for @mention (only counts if OP author isn't Ben)
    op_author = (item.get("author") or {}).get("login", "").lower()
    op_body = (item.get("body") or "").lower()
    if op_author != user_lc:
        for needle in mention_needles:
            if needle in op_body:
                directly_mentioned = True
                break

    # Walk comments
    for c in (item.get("comments", {}).get("nodes") or []):
        c_author = (c.get("author") or {}).get("login", "").lower()
        c_body = (c.get("body") or "").lower()

        if c_author == user_lc:
            user_commented = True
        else:
            for needle in mention_needles:
                if needle in c_body:
                    directly_mentioned = True

    return {"directly_mentioned": directly_mentioned, "user_commented": user_commented}


def classify_project(text: str) -> str:
    """Simple keyword-based project classification."""
    text_lower = text.lower()
    for project, keywords in PROJECT_MAP.items():
        for kw in keywords:
            if kw.lower() in text_lower:
                return project
    return "Uncategorized"


def score_relevance(item: dict, item_type: str) -> int:
    """Score relevance 0-10 from provenance, keywords, and recency.

    Weights are deliberate but untuned — they have never run against real data before today,
    so treat the first week of output as calibration rather than as a verdict. Anything at or
    above MIN_SCORE reaches the briefing; the score travels with the item so Claude can rank
    within that set.
    """
    score = 0
    text = f"{item.get('title', '')} {(item.get('body') or '')[:500]}".lower()
    sources = item.get("sources") or set()

    # Provenance beats text. Assignment and mention are explicit claims on Ben's attention,
    # and neither is reliably visible in the body — GitHub records the assignee in a field,
    # not in prose, so the old text-only check missed every one of them.
    if "assigned" in sources:
        score += 5
    if "mentioned" in sources or item_type == "mention":
        score += 4
    if GITHUB_USER.lower() in text or "@benmyers" in text:
        score += 5

    # Labels. These were worth NOTHING in the original scorer, which is the single biggest
    # reason it would have looked broken the moment it was switched on: a label is the repo's
    # own declaration of topic, applied deliberately by a human, and it is strictly better
    # evidence than a substring in a title. #23733 carried both MMF and NHI and scored 0.
    labels = {(l.get("name") or "").lower() for l in (item.get("labels") or [])}
    label_hits = len(labels & {l.lower() for l in RELEVANT_LABELS})
    label_points = min(2 + (label_hits - 1), 4) if label_hits else 0

    # Keywords. The first match is worth 3, not 2, so that a single unambiguous hit clears
    # MIN_SCORE on its own — a discussion titled "…CII S3 Stored Schema" scored 2 under the
    # old flat weighting and was dropped, which is not a defensible outcome for a CII item.
    # Note the list wants phrases: "identity fabric" is here but bare "fabric" is not, so
    # Fabric issues earn their points from the `fabric` LABEL rather than the title.
    # A keyword that merely restates a label already credited is not new evidence. Without
    # this exclusion, every NHI-labelled UI bug scored label(2) + keyword "NHI"(3) = 5 and
    # sailed through — "`Last Seen` column is blank" outranked the CII schema discussion. The
    # same concept counted twice is the classic way a scorer inflates its own noise floor.
    credited = labels & {l.lower() for l in RELEVANT_LABELS}
    kw_hits = sum(
        1 for kw in RELEVANT_KEYWORDS
        if kw.lower() in text and kw.lower() not in credited
    )
    kw_points = min(3 + (kw_hits - 1) * 2, 6) if kw_hits else 0

    # Cap the topical half so no amount of topic matching outweighs a direct claim on his
    # attention (assigned = 5, mentioned = 4).
    score += min(label_points + kw_points, 7)

    # Per-source floor. See DISCUSSION_SOURCES: a repo dedicated to one of Ben's projects is a
    # topic signal in its own right, and ID-fabric titles omit the words a keyword list needs.
    score += item.get("source_bonus", 0)

    # Recent activity boost
    updated = item.get("updatedAt", "")
    if updated:
        try:
            updated_dt = datetime.fromisoformat(updated.replace("Z", "+00:00"))
            hours_ago = (datetime.now(timezone.utc) - updated_dt).total_seconds() / 3600
            if hours_ago < 24:
                score += 2
            elif hours_ago < 48:
                score += 1
        except (ValueError, TypeError):
            pass

    return min(score, 10)


def drop_closed(items: list[dict], kind: str) -> tuple[list[dict], int]:
    """Drop items that are already closed. Returns (kept, dropped_count).

    Every fetcher above now asks GitHub for open items only, so in normal operation this
    drops nothing. It exists as a single chokepoint because relying on each query to
    remember is exactly what failed: the `mentions` query omitted the filter, and three
    long-closed issues led the 2026-09-15 briefing as things needing Ben's input. A fifth
    fetcher added later cannot reintroduce that.

    Issues and mentions carry `state` ("open"/"closed"); discussions carry `closed` (bool).
    Both are handled. An item with NEITHER field is kept — a field GitHub didn't return is
    not evidence the item is closed, and silently dropping live work is worse than the bug
    this guards against. Docs commits have no state and are not passed through here at all.
    """
    kept: list[dict] = []
    for item in items:
        closed = item.get("closed")
        if closed is None:
            closed = (item.get("state") or "").lower() == "closed"
        if closed:
            log(f"    dropping closed {kind} #{item.get('number')}: "
                f"{(item.get('title') or '')[:60]}")
            continue
        kept.append(item)
    return kept, len(items) - len(kept)


def rank_and_filter(items: list[dict], item_type: str) -> tuple[list[dict], int]:
    """Attach score + project to each item, drop those under MIN_SCORE, sort best-first.

    Returns (kept, dropped_count). The count is reported in the file: a filter that silently
    discards is indistinguishable from a quiet day, which is the failure mode this whole
    script has been bitten by twice.
    """
    for item in items:
        text = f"{item.get('title', '')} {(item.get('body') or '')[:500]}"
        item["score"] = score_relevance(item, item_type)
        item["project"] = classify_project(text)
        if item_type == "discussion":
            item.update(detect_mention_signals(item))
        elif item_type in ("issue", "mention"):
            sources = item.get("sources") or set()
            item["directly_mentioned"] = "mentioned" in sources or "assigned" in sources
            item["user_commented"] = False  # not tracked for issues yet
    kept = sorted(
        (i for i in items if i["score"] >= MIN_SCORE),
        key=lambda i: (-i["score"], i.get("updatedAt", "")),
    )
    return kept, len(items) - len(kept)


def use_bedrock() -> bool:
    val = os.environ.get("CLAUDE_CODE_USE_BEDROCK", "")
    return val.lower() in ("true", "1", "yes")


def get_claude_client():
    """Create Claude client."""
    if use_bedrock():
        # Clear empty AWS_BEARER_TOKEN_BEDROCK that conflicts with aws_profile
        if not os.environ.get("AWS_BEARER_TOKEN_BEDROCK"):
            os.environ.pop("AWS_BEARER_TOKEN_BEDROCK", None)
        return anthropic.AnthropicBedrock(
            aws_profile=os.environ.get("AWS_PROFILE", "default"),
        )
    return anthropic.Anthropic()


def build_triage_prompt(discussions: list, issues: list, mentions: list, docs: list) -> str:
    """Build the prompt for Claude to generate the triage markdown."""
    context = """You are generating a GitHub triage briefing for Ben Myers, a PM at Cisco working on Identity Fabric, CII (Cisco Identity Intelligence), CUI integration, and Meraki+Duo.

Discussions come from TWO repos: `cisco-sbg/ID-fabric-core` (the CII/Cloud Control product
board, formerly `oort-dev/centenario`) and `cisco-sbg/ID-fabric` (the Identity Fabric design board, where
architecture proposals and RFCs land). Issue numbers collide between them, so always name the
repo. ID-fabric items are design proposals Ben often needs to weigh in on before they settle,
so lean toward "Worth Reviewing" for those rather than "Project Updates" — but only promote
to "Needs Your Input" if Ben is directly @mentioned or explicitly asked a question.

His active projects:
- Identity Fabric (CUI data flowing into Fabric, Cloud Control integration)
- CII Adoption (trust levels, admin access, GA)
- Meraki+Duo via Access Manager (Duo as identity broker)
- HMF POC (Duo as identity broker for Firewall+Secure Access)
- Astrix/NHI (non-human identity, agent security)

Key people he works with: Brianna Penney, Ben Murray (chmurray-cisco), Anton Khitrenovich (khitrenovich), Didi (Didithekli), Yana Vaisman (yanavais), Mrudula Gaidhani, Adam Greer, Sean (spdaley), Aaron Woland (aawoland)

Generate a triage markdown file with these sections:
1. **Needs Your Input** — ONLY items where Ben is directly @mentioned, assigned, or explicitly asked a question by another participant. The data includes a "Directly @mentioned" flag — if it says NO and there is no explicit question directed at Ben in the comments, this item does NOT belong here. A discussion about one of Ben's project areas is NOT enough — someone must have specifically requested his input. Ben having commented on a thread does NOT mean he was mentioned.
2. **Worth Reviewing** — Discussions/issues about Ben's projects where his input would add value, but nobody explicitly asked for it. This is the home for topically relevant items that score high but lack a direct @mention or question. If "Directly @mentioned: NO" and the item is relevant to his projects, it goes here.
3. **Project Updates** — New activity on issues/discussions tied to his projects (group by project)
4. **FYI** — Interesting but no action needed (docs changes, peripheral discussions)

For each item include:
- Title with link
- Who's involved
- One-line summary of what happened / what's needed
- Project classification

Every item below has already been scored 0-10 and filtered — anything under the threshold was
dropped before you saw it, so do NOT re-apply a relevance cutoff. Use the score to ORDER within
each section (highest first) and to decide how much space an item deserves, not whether to
include it. Each item also carries a keyword-derived project guess; correct it if the text says
otherwise, since it is a first-substring match and gets ties wrong.

Scores are provenance-weighted: assigned-to-Ben and mentions-Ben contribute most, so a
high-scoring item is usually one with a direct claim on his attention rather than merely a
topical match. Each item also has "Directly @mentioned" and "Ben commented" flags — use these
to decide between "Needs Your Input" and "Worth Reviewing".

Keep it concise. Use the same tone as a chief of staff briefing.
"""

    data_section = "## Raw Data\n\n"

    if discussions:
        data_section += ("### Recent Discussions\n"
                        "(from two repos — always name which one, the numbers overlap)\n")
        for d in discussions:
            comments_text = ""
            if d.get("comments", {}).get("nodes"):
                comments_text = "\n".join([
                    f"  - [{c['createdAt'][:10]}] {c['author']['login']}: {c['body'][:150]}"
                    for c in d["comments"]["nodes"]
                ])
            mention_flag = "YES" if d.get("directly_mentioned") else "NO"
            participated = "YES" if d.get("user_commented") else "NO"
            data_section += f"""
**{d.get('repo', '?')}#{d['number']}** [{d['category']['name']}] {d['title']}
Score: {d.get('score', '?')}/10 | Project guess: {d.get('project', 'Uncategorized')}
Directly @mentioned: {mention_flag} | Ben commented: {participated}
Author: {d['author']['login']} | Updated: {d['updatedAt'][:10]} | Comments: {d['comments']['totalCount']}
URL: {d['url']}
Body excerpt: {d['body'][:300]}
Recent comments:
{comments_text}
---
"""

    if issues:
        data_section += "\n### Recent Issues\n"
        for i in issues:
            labels = ", ".join([l["name"] for l in i.get("labels", [])])
            assignees = ", ".join([a["login"] for a in i.get("assignees", [])])
            # `found via` is the provenance the score is built on — it tells Claude whether this
            # is Ben's issue or merely an issue about one of his topics.
            found = ", ".join(sorted(i.get("sources", set()))) or "unknown"
            mention_flag = "YES" if i.get("directly_mentioned") else "NO"
            data_section += (
                f"**[#{i['number']}]({i.get('url', '')})** {i['title']} "
                f"(score: {i.get('score', '?')}/10, project: {i.get('project', 'Uncategorized')}, "
                f"found via: {found}, directly @mentioned: {mention_flag}, "
                f"labels: {labels}, assignees: {assignees}, "
                f"updated: {i.get('updatedAt', '')[:10]})\n"
            )

    if mentions:
        data_section += "\n### Mentions\n"
        for m in mentions:
            repo = m.get("repository", {}).get("name", "unknown")
            data_section += (
                f"**[#{m['number']}]({m.get('url', '')})** [{repo}] {m['title']} "
                f"(score: {m.get('score', '?')}/10, project: {m.get('project', 'Uncategorized')}, "
                f"updated: {m.get('updatedAt', '')[:10]})\n"
            )

    if docs:
        data_section += "\n### Docs Changes\n"
        for d in docs:
            data_section += f"- [{d.get('date', '')[:10]}] {d.get('message', '')[:80]} ({d.get('author', '')})\n"

    return context + "\n\n" + data_section


def generate_triage(discussions, issues, mentions, docs) -> str:
    """Use Claude to generate the triage markdown."""
    client = get_claude_client()
    prompt = build_triage_prompt(discussions, issues, mentions, docs)

    model = "us.anthropic.claude-sonnet-4-20250514-v1:0" if use_bedrock() else "claude-sonnet-4-6-20250514"

    response = client.messages.create(
        model=model,
        # Raised from 4000 when ID-fabric was added: the am sweep now carries ~25 discussions
        # plus ~13 issues, and a briefing that runs out of tokens mid-section looks identical
        # to one that decided the rest was irrelevant.
        max_tokens=8000,
        messages=[{"role": "user", "content": prompt}],
    )

    return response.content[0].text


def write_output(body: str, dropped: int | None = None, closed: int | None = None) -> str:
    """Write today's triage file for this slot. Always called — see main() for why."""
    now = datetime.now()
    label = "morning sweep" if SLOT == "am" else "afternoon delta"
    header = f"# GitHub Triage ({label}) — {now.strftime('%Y-%m-%d %I:%M %p')}\n\n"
    sources = ", ".join(f"`{s['owner']}/{s['repo']}`" for s in DISCUSSION_SOURCES)
    header += f"Discussions: {sources} | Issues/mentions: `{SEARCH_ORG}` | Lookback: {LOOKBACK_DAYS} days"
    # State the drop count. A relevance filter that discards quietly is the same failure this
    # script already had twice: an empty section that could mean "nothing happened" or "the
    # threshold ate it" and no way to tell which from the file alone.
    if dropped:
        header += f" | {dropped} item(s) below score {MIN_SCORE} filtered out"
    # Same reasoning as the drop count: state what was excluded rather than leaving the
    # reader to wonder whether a closed item was suppressed or simply never fetched.
    if closed:
        header += f" | {closed} closed item(s) excluded"
    header += "\n\n---\n\n"
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    path = os.path.join(OUTPUT_DIR, f"{now.strftime('%Y-%m-%d')}{FILE_SUFFIX}")
    with open(path, "w") as f:
        f.write(header + body)
    return path


def main() -> int:
    """Returns a real exit code. Previously returned None always, so the process
    exited 0 even when every single API call had failed — nothing downstream could
    tell a broken run from a quiet one."""
    if log_slot_warning:
        log(f"  warning: {log_slot_warning}")
    log(f"GitHub Triage [{SLOT}] — fetching data from {SEARCH_ORG} "
        f"(lookback {LOOKBACK_DAYS}d, writing *{FILE_SUFFIX})...")

    log("  Fetching discussions...")
    discussions = fetch_recent_discussions()
    discussions, closed_d = drop_closed(discussions, "discussion")
    discussions, dropped_d = rank_and_filter(discussions, "discussion")
    log(f"  Found {len(discussions)} recent discussions (dropped {closed_d} closed, "
        f"{dropped_d} under score {MIN_SCORE})")

    log("  Fetching issues...")
    issues = fetch_relevant_issues()
    issues, closed_i = drop_closed(issues, "issue")
    issues, dropped_i = rank_and_filter(issues, "issue")
    log(f"  Found {len(issues)} relevant issues (dropped {closed_i} closed, "
        f"{dropped_i} under score {MIN_SCORE})")

    log("  Fetching mentions...")
    mentions = fetch_mentions_and_notifications()
    mentions, closed_m = drop_closed(mentions, "mention")
    mentions, dropped_m = rank_and_filter(mentions, "mention")
    log(f"  Found {len(mentions)} mentions (dropped {closed_m} closed, "
        f"{dropped_m} under score {MIN_SCORE})")

    # Docs commits are deliberately NOT score-filtered. score_relevance() reads title/body and
    # a commit has neither — it has `message` — so every docs commit would score 0 and the
    # whole FYI section would vanish. They stay unfiltered, as before.
    log("  Fetching docs changes...")
    docs = fetch_docs_changes()
    log(f"  Found {len(docs)} docs commits (not score-filtered)")

    dropped_total = dropped_d + dropped_i + dropped_m
    closed_total = closed_d + closed_i + closed_m

    # A fetch failure is not a quiet day. This has to be said in the FILE, not just the
    # log: /morning-coffee reads the file and never sees stderr, so a silent 0-item file
    # would read to it as "nothing happened on GitHub."
    if FETCH_ERRORS:
        detail = "\n".join(f"- `{e}`" for e in FETCH_ERRORS[:10])
        more = f"\n\n…and {len(FETCH_ERRORS) - 10} more." if len(FETCH_ERRORS) > 10 else ""
        path = write_output(
            "## ⚠️ Triage FAILED — this file is incomplete\n\n"
            f"{len(FETCH_ERRORS)} GitHub API call(s) failed. **Do not read the absence of "
            "items below as 'nothing happened'** — the data was never retrieved.\n\n"
            f"{detail}{more}\n\n"
            "Most likely cause: `GH_TOKEN` missing, expired, or revoked. "
            "`run_github_triage.sh` health-checks auth before this script runs; if you are "
            "seeing this, check `/tmp/github_triage.log`.\n"
        )
        log(f"FAILED: {len(FETCH_ERRORS)} fetch error(s) — wrote failure report to {path}")
        return 1

    if not any([discussions, issues, mentions, docs]):
        # Distinguish "nothing was there" from "the score filter took everything" — those are
        # very different signals and the old message asserted the first regardless.
        if dropped_total or closed_total:
            parts = []
            if dropped_total:
                parts.append(f"{dropped_total} scored under {MIN_SCORE}")
            if closed_total:
                parts.append(f"{closed_total} were already closed")
            reason = (
                f"All four sources returned zero items **that qualify** for the last "
                f"{LOOKBACK_DAYS} days. Items were fetched, but {' and '.join(parts)}, so this "
                f"is a low-signal period rather than an empty one — lower `MIN_SCORE` in "
                f"`github_triage.py` if that looks wrong."
            )
        else:
            reason = (
                f"All four sources returned zero items for the last {LOOKBACK_DAYS} days, and "
                "nothing was dropped by the relevance or closed-state filters."
            )
        path = write_output(
            f"## No activity\n\n{reason} Every API call succeeded — this is not a failure.\n",
            dropped_total,
            closed_total,
        )
        log(f"no activity, all fetches OK ({dropped_total} filtered, {closed_total} closed) — wrote {path}")
        return 0

    log("  Generating triage with Claude...")
    try:
        triage_md = generate_triage(discussions, issues, mentions, docs)
    except Exception as exc:
        path = write_output(
            "## ⚠️ Triage FAILED — summary could not be generated\n\n"
            "GitHub data was fetched successfully, but the Claude call failed. The items "
            "below were retrieved and are simply unsummarised.\n\n"
            f"```\n{exc}\n```\n\n"
            f"Counts: {len(discussions)} discussions, {len(issues)} issues, "
            f"{len(mentions)} mentions, {len(docs)} docs commits.\n"
        )
        log(f"FAILED: Claude generation error — {exc} — wrote {path}")
        return 1

    path = write_output(triage_md, dropped_total, closed_total)
    log(f"Written to {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
