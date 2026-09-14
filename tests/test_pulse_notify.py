import pathlib
import subprocess
from unittest.mock import patch

import pytest

from lib.pulse_notify import (
    SOUND,
    TIMEOUT_S,
    TITLE,
    _run,
    build_body,
    build_command,
    find_notifier,
    notify,
)


def _item(name, channel):
    return {"from": {"name": name}, "channel": channel}


def test_body_names_a_single_sender_and_channel():
    assert build_body([_item("Rory Scott", "C3 + CUI")]) == "1 needs you — Rory Scott (C3 + CUI)"


def test_body_pluralises_and_lists_up_to_two_then_summarises():
    body = build_body([
        _item("Rory Scott", "C3 + CUI"),
        _item("Shyam Srinivasan", "Direct message"),
        _item("Einar Nilsen-Nygaard", "PureCA Identity Fabric"),
    ])
    assert body == "3 need you — Rory Scott (C3 + CUI), Shyam Srinivasan (Direct message), +1"


def test_body_with_exactly_two_has_no_overflow_marker():
    body = build_body([_item("A", "X"), _item("B", "Y")])
    assert body == "2 need you — A (X), B (Y)"


def test_notify_does_nothing_and_reports_false_when_there_is_nothing_new():
    calls = []
    assert notify([], runner=lambda cmd: calls.append(cmd), notifier=None) is False
    assert calls == []


def test_notify_fires_exactly_once_regardless_of_item_count():
    calls = []
    items = [_item(f"P{i}", "C3 + CUI") for i in range(7)]
    assert notify(items, runner=lambda cmd: calls.append(cmd), notifier=None) is True
    assert len(calls) == 1


def test_notify_requests_a_sound():
    calls = []
    notify([_item("A", "X")], runner=lambda cmd: calls.append(cmd), notifier=None)
    # Pin the literal sound value, not just the clause name and not the
    # SOUND constant imported from the same module under test (asserting
    # against that constant is self-referential: if SOUND were changed to
    # "" — silent, defeating the entire point of the priority tier — the
    # module's own output and the test's expectation would drift together
    # and the test would still pass). Hardcode the brief's exact value.
    assert 'sound name "Submarine"' in calls[0][-1]
    assert SOUND == "Submarine"


def test_notify_requests_the_expected_title():
    calls = []
    notify([_item("A", "X")], runner=lambda cmd: calls.append(cmd), notifier=None)
    # Same self-reference concern as the sound assertion above — hardcode
    # the brief's literal title rather than importing TITLE for comparison.
    assert 'with title "Hourly Pulse"' in calls[0][-1]
    assert TITLE == "Hourly Pulse"


def test_notify_escapes_double_quotes_in_names():
    calls = []
    notify([_item('Ro"ry', "C3 + CUI")], runner=lambda cmd: calls.append(cmd), notifier=None)
    assert '\\"' in calls[0][-1]


def test_notify_escapes_a_backslash_in_the_middle_of_a_name():
    # A single actual backslash in the source name must become two actual
    # backslashes in the AppleScript literal — real osascript rejects the
    # unescaped form with "syntax error: A identifier can't go after this".
    calls = []
    notify([_item("Ro\\ry", "C3 + CUI")], runner=lambda cmd: calls.append(cmd), notifier=None)
    script = calls[0][-1]
    assert "Ro\\\\ry" in script


def test_notify_escapes_a_trailing_backslash_so_the_literal_still_closes():
    # A trailing backslash is the sharper case: unescaped, it consumes the
    # closing double-quote and osascript fails with "Expected \"\"\" but
    # found end of script" — the same permanent-silence chain as a raw
    # newline, through a different character. The name is followed by
    # " (channel)" before the string closes, so assert on that exact
    # neighborhood rather than assuming the backslash sits next to the quote.
    calls = []
    notify([_item("Rory\\", "C3 + CUI")], runner=lambda cmd: calls.append(cmd), notifier=None)
    script = calls[0][-1]
    assert "Rory\\\\ (C3 + CUI)" in script
    assert '(C3 + CUI)" with title' in script


def test_notify_swallows_runner_failure_rather_than_killing_the_run():
    def boom(_cmd):
        raise RuntimeError("osascript missing")

    assert notify([_item("A", "X")], runner=boom, notifier=None) is False


# --- Ruling 1: control characters in sender names must not break the AppleScript ---


def test_notify_fires_when_a_sender_name_contains_a_newline():
    calls = []
    result = notify(
        [_item("Ro\nry Scott", "C3 + CUI")],
        runner=lambda cmd: calls.append(cmd),
        notifier=None,
    )
    assert result is True
    script = calls[0][-1]
    assert "\n" not in script
    assert "\r" not in script


def test_notify_collapses_tabs_and_carriage_returns_in_sender_names():
    calls = []
    notify([_item("Ro\try\r Scott", "C3 + CUI")], runner=lambda cmd: calls.append(cmd), notifier=None)
    script = calls[0][-1]
    assert "\t" not in script
    assert "\r" not in script


# --- Ruling 2: missing/empty sender name and channel must fall back, not print "None" ---


def test_body_falls_back_to_someone_and_question_mark_when_name_is_none():
    body = build_body([{"from": {"name": None}, "channel": ""}])
    assert body == "1 needs you — someone (?)"


def test_body_falls_back_to_someone_when_from_itself_is_none():
    body = build_body([{"from": None, "channel": "C3 + CUI"}])
    assert body == "1 needs you — someone (C3 + CUI)"


# --- The production runner: a failed banner must actually raise (check=True),
# time out (timeout=TIMEOUT_S), and notify() must turn both into False. ---


def test_run_raises_on_a_real_nonzero_exit():
    # /usr/bin/false is cheap and hermetic — no osascript, no banner, no TCC.
    # Without check=True in _run, this would return None instead of raising,
    # and notify() would then report success for a banner that never fired.
    with pytest.raises(subprocess.CalledProcessError):
        _run(["/usr/bin/false"])


def test_run_passes_a_finite_timeout_to_subprocess_run():
    captured = {}

    def fake_subprocess_run(cmd, **kwargs):
        captured.update(kwargs)
        return subprocess.CompletedProcess(cmd, 0)

    with patch("lib.pulse_notify.subprocess.run", side_effect=fake_subprocess_run):
        _run(["osascript", "-e", "beep"])

    # A hung osascript (a TCC permission dialog waiting on a click is the
    # realistic cause) would otherwise hang the whole run forever; launchd's
    # StartInterval never overlaps, so one hang silently kills the periodic
    # job for good.
    assert captured.get("timeout") == TIMEOUT_S


def test_notify_returns_false_when_the_default_runner_hits_a_real_nonzero_exit():
    # End-to-end, no mocking: point the actual production runner (_run, not
    # an injected stub) at a real command that fails (/usr/bin/false), and
    # confirm notify() swallows it as False rather than True. The wrapper
    # only substitutes which command _run runs — everything downstream is
    # real: real subprocess.run, real check=True, real CalledProcessError.
    # Hermetic (no osascript, no banner) and, unlike a mock on subprocess.run
    # itself, cannot be fooled by _run recursing into a patched subprocess.
    def runner(_cmd):
        _run(["/usr/bin/false"])

    assert notify([_item("A", "X")], runner=runner, notifier=None) is False


def test_notify_returns_false_when_the_runner_times_out():
    def timeout_runner(cmd):
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=TIMEOUT_S)

    assert notify([_item("A", "X")], runner=timeout_runner, notifier=None) is False


# --- The icon-carrying bundle. macOS takes a notification's icon from the
# bundle that sent it and offers no way to override it per notification, so a
# Hanuman icon means sending through a bundle whose icon is Hanuman's. That is
# a second command shape, and everything below is about it not costing us the
# banner we already have. ---


FAKE_NOTIFIER = "/nowhere/Hanuman Pulse.app/Contents/MacOS/terminal-notifier"


def test_command_for_the_bundle_passes_the_body_as_an_argument_not_a_script():
    cmd = build_command("2 need you — A (X), B (Y)", FAKE_NOTIFIER)
    assert cmd[0] == FAKE_NOTIFIER
    assert "osascript" not in cmd
    # The body travels as its own argv element, so there is no string literal
    # to break out of and nothing to escape.
    assert "2 need you — A (X), B (Y)" in cmd
    assert "display notification" not in " ".join(cmd)


def test_command_for_the_bundle_still_asks_for_title_and_sound():
    cmd = build_command("1 needs you — A (X)", FAKE_NOTIFIER)
    # Hardcoded rather than compared against TITLE/SOUND for the same
    # self-reference reason as the osascript assertions above.
    assert cmd[cmd.index("-title") + 1] == "Hourly Pulse"
    assert cmd[cmd.index("-sound") + 1] == "Submarine"


def test_command_for_the_bundle_does_not_applescript_escape_the_body():
    # On the osascript path a backslash must be doubled or the literal breaks.
    # Here doubling would be a bug the other way: argv has no literal to close,
    # so the user would read an extra backslash that was never in the name.
    cmd = build_command('Ro\\ry "quoted"', FAKE_NOTIFIER)
    assert 'Ro\\ry "quoted"' in cmd


def test_notify_prefers_the_bundle_when_one_is_available():
    calls = []
    assert notify(
        [_item("A", "X")],
        runner=lambda cmd: calls.append(cmd),
        notifier=FAKE_NOTIFIER,
    ) is True
    assert len(calls) == 1
    assert calls[0][0] == FAKE_NOTIFIER


def test_notify_falls_back_to_osascript_when_the_bundle_fails():
    # This is the whole safety argument for the fallback. The bundle needs a
    # notification permission grant that is recorded per bundle id, and a
    # denial is effectively permanent — `tccutil reset UserNotification`
    # refuses to clear it. Without this fallback an unauthorized bundle would
    # cost the banner entirely, and because the caller only marks an item
    # notified when notify() returns True, the same item would be retried
    # every run forever. Losing the icon is the acceptable failure; losing the
    # banner is not.
    calls = []

    def runner(cmd):
        calls.append(cmd)
        if cmd[0] == FAKE_NOTIFIER:
            raise subprocess.CalledProcessError(3, cmd)

    assert notify([_item("A", "X")], runner=runner, notifier=FAKE_NOTIFIER) is True
    assert len(calls) == 2
    assert calls[0][0] == FAKE_NOTIFIER
    assert calls[1][0] == "osascript"


def test_notify_reports_false_only_when_both_paths_fail():
    def boom(_cmd):
        raise RuntimeError("nothing works")

    assert notify([_item("A", "X")], runner=boom, notifier=FAKE_NOTIFIER) is False


def test_notify_does_not_try_the_bundle_twice_when_there_is_none():
    calls = []
    notify([_item("A", "X")], runner=lambda cmd: calls.append(cmd), notifier=None)
    assert len(calls) == 1
    assert calls[0][0] == "osascript"


def test_find_notifier_returns_none_when_the_bundle_is_not_installed():
    # Resolved per call rather than at import, so building the bundle later
    # does not require restarting anything that imported this module.
    assert find_notifier(pathlib.Path("/nowhere/terminal-notifier")) is None


def test_find_notifier_returns_the_path_when_the_bundle_is_installed(tmp_path):
    binary = tmp_path / "terminal-notifier"
    binary.write_text("#!/bin/sh\n")
    assert find_notifier(binary) == str(binary)


def test_command_for_the_bundle_makes_a_click_open_the_hub():
    # The banner's whole job is to send Ben to Pulse, so clicking it should get
    # him there. Hardcoded rather than compared against the module constant for
    # the same self-reference reason as the title and sound assertions: if the
    # id were changed to something that resolves to nothing, comparing against
    # the constant would let module and test drift together.
    cmd = build_command("1 needs you — A (X)", FAKE_NOTIFIER)
    assert cmd[cmd.index("-activate") + 1] == "com.benmyers.hanuman-hub"


def test_the_osascript_fallback_has_no_click_action():
    # Not an oversight — `display notification` has no way to express one. The
    # fallback trades the icon and the click for a banner that always fires.
    cmd = build_command("1 needs you — A (X)", None)
    assert "-activate" not in cmd
