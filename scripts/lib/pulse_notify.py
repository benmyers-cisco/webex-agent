"""The single per-run notification.

One banner and one sound per run when new priority items exist, and nothing
otherwise. The item count does not change the loudness; neither does who sent
them or where.

Fired via osascript from Python rather than from the Hub, because launchd runs
regardless of whether the Tauri app is open. A Hub-side notification would go
silent the moment Ben closed the window.
"""
from __future__ import annotations

import subprocess

TITLE = "Hourly Pulse"
SOUND = "Submarine"
NAMED_IN_BODY = 2
TIMEOUT_S = 10


def _escape(value: str) -> str:
    """Neutralize characters that would break the AppleScript string literal.

    Backslashes and double quotes are escaped so the literal still parses.
    Control characters (\\n, \\r, \\t) are collapsed to a single space rather
    than escaped: a raw newline inside an AppleScript string literal makes
    osascript fail to compile, which makes notify() return False, which means
    Task 11 will keep treating the item as not-yet-notified and retry with the
    same (attacker-controlled) display name forever. Collapsing beats
    escaping here because there is no in-string escape sequence for a literal
    newline in AppleScript text.
    """
    value = value or ""
    for ch in ("\n", "\r", "\t"):
        value = value.replace(ch, " ")
    return value.replace("\\", "\\\\").replace('"', '\\"')


def build_body(priority_items: list[dict]) -> str:
    count = len(priority_items)
    verb = "needs" if count == 1 else "need"
    named = [
        f"{(item.get('from') or {}).get('name') or 'someone'} ({item.get('channel') or '?'})"
        for item in priority_items[:NAMED_IN_BODY]
    ]
    body = f"{count} {verb} you — {', '.join(named)}"
    overflow = count - len(named)
    if overflow > 0:
        body += f", +{overflow}"
    return body


def _run(cmd: list[str]) -> None:
    subprocess.run(cmd, check=True, timeout=TIMEOUT_S, capture_output=True)


def notify(priority_items: list[dict], runner=_run) -> bool:
    """Fire one notification. Returns whether it fired.

    Never raises: a failed banner (missing osascript, a timeout, a nonzero
    exit, anything) must never fail the run that called it.
    """
    if not priority_items:
        return False

    script = (
        f'display notification "{_escape(build_body(priority_items))}" '
        f'with title "{TITLE}" sound name "{SOUND}"'
    )
    try:
        runner(["osascript", "-e", script])
    except Exception:  # noqa: BLE001 — a failed banner must not fail the run
        return False
    return True
