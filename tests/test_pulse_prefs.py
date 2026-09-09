from lib.pulse_prefs import parse_prefs

PREFS = """# Webex Agent Preferences

## My Role & Focus
- Ben Myers (benmyers@cisco.com)

## Watchlist
<!-- Anything from these people is a priority notification, in any channel. -->
- Rory Scott <rorscott@cisco.com>
- Matt Caulfield <mcaulfie@cisco.com>
- Taylor

## Always Scan
<!-- comment that must not become a title -->

### Priority 1 — Interrupt Me
- C3 + CUI
- Mini EC with CUI

### Priority 2 — Tagged Only
- SCC - CII Discussion

## Mentions Only
- Duo PM Sync

## Never Scan
- Social / watercooler channels
"""


def test_p1_and_p2_are_kept_separate():
    prefs = parse_prefs(PREFS)
    assert prefs.p1 == {"c3 + cui", "mini ec with cui"}
    assert prefs.p2 == {"scc - cii discussion"}


def test_tier_of_is_case_and_whitespace_insensitive():
    prefs = parse_prefs(PREFS)
    assert prefs.tier_of("  C3 + CUI  ") == "p1"
    assert prefs.tier_of("scc - cii discussion") == "p2"
    assert prefs.tier_of("Duo PM Sync") == "unlisted"
    assert prefs.tier_of("Something Nobody Listed") == "unlisted"


def test_watchlist_maps_email_to_name():
    prefs = parse_prefs(PREFS)
    assert prefs.watchlist["rorscott@cisco.com"] == "Rory Scott"
    assert prefs.watchlist["mcaulfie@cisco.com"] == "Matt Caulfield"


def test_watchlist_lookup_is_case_insensitive():
    prefs = parse_prefs(PREFS)
    assert prefs.is_watchlist("RorScott@Cisco.COM") is True
    assert prefs.is_watchlist("stranger@cisco.com") is False


def test_watchlist_entry_without_an_email_is_reported_not_dropped():
    prefs = parse_prefs(PREFS)
    assert prefs.unresolved == ["Taylor"]


def test_html_comments_are_never_treated_as_titles():
    prefs = parse_prefs(PREFS)
    assert not any("comment" in t for t in prefs.p1 | prefs.p2)


def test_missing_sections_yield_empty_sets_not_errors():
    prefs = parse_prefs("# Empty\n")
    assert prefs.p1 == set()
    assert prefs.p2 == set()
    assert prefs.watchlist == {}
