"""The single per-run notification.

One banner and one sound per run when new priority items exist, and nothing
otherwise. The item count does not change the loudness; neither does who sent
them or where.

Fired from Python rather than from the Hub, because launchd runs regardless of
whether the Tauri app is open. A Hub-side notification would go silent the
moment Ben closed the window.

Sent through a small app bundle when one is installed, and via osascript when it
is not. That is only about the icon: macOS takes a notification's icon from the
bundle that sent it and offers no API to override it per notification, so an
osascript banner is attributed to Script Editor and wears Script Editor's icon.
`scripts/build_notifier_app.sh` builds a bundle carrying the Hanuman icon; this
module sends through it when it is there. The osascript path stays as the floor,
because the bundle needs a permission grant that can be refused — see notify().
"""
from __future__ import annotations

import pathlib
import subprocess

TITLE = "Hourly Pulse"
SOUND = "Submarine"
NAMED_IN_BODY = 2
TIMEOUT_S = 10

# What a click on the banner opens: the Hub itself, whose Pulse panel is the
# thing the banner is telling Ben to go read. This is the real app, not the
# notifier bundle below — they are deliberately different identifiers.
ACTIVATE_BUNDLE_ID = "com.benmyers.hanuman-hub"

# Built by scripts/build_notifier_app.sh. In ~/Applications rather than a build
# directory because LaunchServices has to be able to resolve the bundle: an
# unregistered one does not error, it hangs the first send indefinitely.
NOTIFIER_BIN = (
    pathlib.Path.home()
    / "Applications"
    / "Hanuman Pulse.app"
    / "Contents"
    / "MacOS"
    / "terminal-notifier"
)


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


# Distinguishes "resolve the bundle yourself" from an explicit `notifier=None`,
# which means "use osascript". Tests pass None so they never depend on whether
# the bundle happens to be built on the machine running them.
_RESOLVE = object()


def find_notifier(binary: pathlib.Path = NOTIFIER_BIN) -> str | None:
    """Path to the icon-carrying bundle's binary, or None if it is not built.

    Checked per call rather than at import, so building the bundle takes effect
    on the next run without restarting anything that imported this module.
    """
    return str(binary) if binary.exists() else None


def build_command(body: str, notifier: str | None) -> list[str]:
    """The argv for one banner, through the bundle if there is one.

    Note what is *not* done on the bundle path: no escaping. The body travels as
    its own argv element, so there is no string literal to break out of —
    doubling a backslash there would put a backslash on Ben's screen that was
    never in the sender's name. The osascript path needs `_escape` for exactly
    the opposite reason, and the two must not be confused.

    Only the bundle path can carry a click action. `display notification` has no
    way to express one, so the fallback gives up the icon and the click together
    — which is the trade it exists to make.
    """
    if notifier:
        return [
            notifier,
            "-title", TITLE,
            "-message", body,
            "-sound", SOUND,
            "-activate", ACTIVATE_BUNDLE_ID,
        ]
    return [
        "osascript",
        "-e",
        f'display notification "{_escape(body)}" '
        f'with title "{TITLE}" sound name "{SOUND}"',
    ]


def notify(priority_items: list[dict], runner=_run, notifier=_RESOLVE) -> bool:
    """Fire one notification. Returns whether it fired.

    Never raises: a failed banner (missing osascript, a timeout, a nonzero
    exit, anything) must never fail the run that called it.

    Tries the icon-carrying bundle first and falls back to osascript, because
    the bundle can fail in a way osascript cannot: it needs a notification
    permission grant recorded against its bundle id, and a denial there is
    effectively permanent — `tccutil reset UserNotification` refuses to clear it
    for an id with no ncprefs entry, and the store itself needs Full Disk Access
    to touch. Without the fallback, one denied prompt would cost every banner
    from then on, and since the caller only marks an item notified when this
    returns True, every item would be retried every run forever. Losing the icon
    is the acceptable failure; losing the banner is the one this module exists to
    prevent.
    """
    if not priority_items:
        return False

    if notifier is _RESOLVE:
        notifier = find_notifier()

    body = build_body(priority_items)
    # The bundle first when present, then osascript. Deduplicated so a machine
    # without the bundle makes one attempt, not two identical ones.
    candidates = [notifier, None] if notifier else [None]
    for candidate in candidates:
        try:
            runner(build_command(body, candidate))
        except Exception:  # noqa: BLE001 — a failed banner must not fail the run
            continue
        return True
    return False
