from lib.pulse_notify import build_body, notify


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
    assert notify([], runner=lambda cmd: calls.append(cmd)) is False
    assert calls == []


def test_notify_fires_exactly_once_regardless_of_item_count():
    calls = []
    items = [_item(f"P{i}", "C3 + CUI") for i in range(7)]
    assert notify(items, runner=lambda cmd: calls.append(cmd)) is True
    assert len(calls) == 1


def test_notify_requests_a_sound():
    calls = []
    notify([_item("A", "X")], runner=lambda cmd: calls.append(cmd))
    assert "sound name" in calls[0][-1]


def test_notify_escapes_double_quotes_in_names():
    calls = []
    notify([_item('Ro"ry', "C3 + CUI")], runner=lambda cmd: calls.append(cmd))
    assert '\\"' in calls[0][-1]


def test_notify_swallows_runner_failure_rather_than_killing_the_run():
    def boom(_cmd):
        raise RuntimeError("osascript missing")

    assert notify([_item("A", "X")], runner=boom) is False


# --- Ruling 1: control characters in sender names must not break the AppleScript ---


def test_notify_fires_when_a_sender_name_contains_a_newline():
    calls = []
    result = notify(
        [_item("Ro\nry Scott", "C3 + CUI")],
        runner=lambda cmd: calls.append(cmd),
    )
    assert result is True
    script = calls[0][-1]
    assert "\n" not in script
    assert "\r" not in script


def test_notify_collapses_tabs_and_carriage_returns_in_sender_names():
    calls = []
    notify([_item("Ro\try\r Scott", "C3 + CUI")], runner=lambda cmd: calls.append(cmd))
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
