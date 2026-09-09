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
    assert prefs.tier_of("  duo PM sync ") == "mentions"
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
    assert prefs.mentions == set()
    assert prefs.watchlist == {}


# --- F4: "Mentions only" was indistinguishable from "Never scan" -----------
#
# The Hub's watch form offers four destinations and writes all four. parse_prefs
# knew three, so `## Mentions Only` resolved to "unlisted", space_is_eligible
# said no, and a direct @mention there was never fetched. Ben demotes a noisy
# channel expecting @mentions still to reach him and instead goes invisible in
# it, with sources.webex: "ok" every hour.


def test_the_mentions_only_section_is_parsed_as_its_own_tier():
    prefs = parse_prefs(PREFS)
    assert prefs.mentions == {"duo pm sync"}


def test_never_scan_is_its_own_tier_now_that_unlisted_no_longer_means_unfetched():
    """This test asserted the opposite until group chats became unconditionally
    eligible, and the reversal is the point.

    The original reasoning was sound: "unlisted" already meant "do not fetch", so
    parsing Never Scan would give one behaviour two names. Making group chats
    eligible on a title heuristic broke that equivalence — an unlisted space
    whose title looks like a list of people is now fetched and escalated to P1.
    So the explicit exclusion has to be readable, or the only way to say "not
    this one" is a setting the code cannot see.
    """
    prefs = parse_prefs(PREFS)
    assert prefs.tier_of("Social / watercooler channels") == "never"
    assert prefs.tier_of("Some channel in no list at all") == "unlisted"


def test_never_scan_outranks_a_contradictory_p1_listing():
    """A title in both lists is a contradiction, and the quieter reading is the
    safe one: wrongly staying silent is visible to Ben the moment he looks at the
    panel, while wrongly interrupting him is the failure this project exists to
    remove.
    """
    prefs = parse_prefs("""
### Priority 1 — Interrupt Me
- C3 + CUI

## Never Scan
- C3 + CUI
""")
    assert prefs.tier_of("C3 + CUI") == "never"


def test_a_channel_listed_twice_takes_the_louder_tier():
    """Precedence has to be explicit, or a stale duplicate left behind by a Hub
    "move" silently demotes a P1 channel to mentions-only.
    """
    prefs = parse_prefs("""
### Priority 1 — Interrupt Me
- C3 + CUI

### Priority 2 — Tagged Only
- C3 + CUI

## Mentions Only
- C3 + CUI
""")
    assert prefs.tier_of("C3 + CUI") == "p1"


def test_mentions_beats_nothing_but_loses_to_p2():
    prefs = parse_prefs("""
### Priority 2 — Tagged Only
- Duo PM Sync

## Mentions Only
- Duo PM Sync
""")
    assert prefs.tier_of("Duo PM Sync") == "p2"
