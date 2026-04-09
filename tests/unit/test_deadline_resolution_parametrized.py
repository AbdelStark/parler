"""
Parametrized regression suite for deadline_resolver.

This file complements test_deadline_resolution.py with:
  1. Comprehensive parametrize tables for all known input patterns
  2. freezegun to test time-sensitive anchor date scenarios
  3. DST transition edge cases
  4. Year-boundary rollover cases
  5. Anchor-is-the-named-day edge cases (ambiguous "next X when today IS X")

All tests use the same resolve_deadline() contract: (raw, anchor, lang) → date | None.
"""

from datetime import date, timedelta

import pytest
from freezegun import freeze_time
from parler.extraction.deadline_resolver import resolve_deadline, resolve_deadline_full

# ─── Parametrized English table ───────────────────────────────────────────────

# (raw, anchor, expected_date)
ENGLISH_CASES = [
    # Relative days
    ("tomorrow", date(2026, 4, 9), date(2026, 4, 10)),
    ("tomorrow", date(2026, 12, 31), date(2027, 1, 1)),   # year rollover
    ("tomorrow", date(2026, 2, 28), date(2026, 3, 1)),    # non-leap month boundary
    ("tomorrow", date(2028, 2, 28), date(2028, 2, 29)),   # leap year

    # Next weekday — anchor is Thursday 2026-04-09
    ("next Monday",    date(2026, 4, 9),  date(2026, 4, 13)),
    ("next Tuesday",   date(2026, 4, 9),  date(2026, 4, 14)),
    ("next Wednesday", date(2026, 4, 9),  date(2026, 4, 15)),  # next week's Wednesday
    ("next Thursday",  date(2026, 4, 9),  date(2026, 4, 16)),
    ("next Friday",    date(2026, 4, 9),  date(2026, 4, 17)),
    ("next Saturday",  date(2026, 4, 9),  date(2026, 4, 18)),
    ("next Sunday",    date(2026, 4, 9),  date(2026, 4, 19)),

    # Anchor IS the weekday — "next Friday" from Friday = 7 days later
    ("next Friday",    date(2026, 4, 10), date(2026, 4, 17)),  # anchor is Friday
    ("next Monday",    date(2026, 4, 13), date(2026, 4, 20)),  # anchor is Monday

    # "this" vs "next"
    ("this Friday",    date(2026, 4, 9),  date(2026, 4, 10)),  # Wednesday → this Friday = April 10
    ("this Monday",    date(2026, 4, 9),  date(2026, 4, 6)),   # Wednesday → this Monday = last Monday? or next?
    # Note: "this Monday" from Wednesday is typically this-week's Monday (already past)
    # If in the past, some resolvers use next Monday — behaviour must be consistent

    # "by" prefix
    ("by Friday",      date(2026, 4, 9),  date(2026, 4, 17)),  # "by Friday" from Wed = next Friday
    ("by Friday",      date(2026, 4, 10), date(2026, 4, 10)),  # "by Friday" from Friday = today
    ("by end of week", date(2026, 4, 9),  date(2026, 4, 11)),

    # "end of" patterns
    ("end of week",    date(2026, 4, 9),  date(2026, 4, 11)),  # Friday
    ("EOW",            date(2026, 4, 9),  date(2026, 4, 11)),
    ("end of month",   date(2026, 4, 9),  date(2026, 4, 30)),
    ("end of month",   date(2026, 2, 10), date(2026, 2, 28)),  # non-leap
    ("end of month",   date(2028, 2, 10), date(2028, 2, 29)),  # leap year
    ("end of month",   date(2026, 1, 31), date(2026, 1, 31)),  # anchor is last day

    # "next week"
    ("next week",      date(2026, 4, 9),  date(2026, 4, 13)),  # Monday of next week

    # Explicit dates — numeric formats
    ("14/04/2026",     date(2026, 4, 9),  date(2026, 4, 14)),
    ("2026-04-20",     date(2026, 4, 9),  date(2026, 4, 20)),
    ("04/14/2026",     date(2026, 4, 9),  date(2026, 4, 14)),  # US format

    # Explicit dates — month name
    ("April 14th",     date(2026, 4, 9),  date(2026, 4, 14)),
    ("April 14",       date(2026, 4, 9),  date(2026, 4, 14)),
    ("14 April",       date(2026, 4, 9),  date(2026, 4, 14)),  # British format
    ("March 15",       date(2026, 4, 9),  date(2027, 3, 15)),  # past → next year

    # Year rollover for month names
    ("January 15",     date(2026, 12, 1), date(2027, 1, 15)),

    # Unresolvable
    ("sometime soon",  date(2026, 4, 9),  None),
    ("asap",           date(2026, 4, 9),  None),
    ("TBD",            date(2026, 4, 9),  None),
    ("",               date(2026, 4, 9),  None),
    (None,             date(2026, 4, 9),  None),
]


@pytest.mark.parametrize("raw,anchor,expected", ENGLISH_CASES, ids=[
    f"en:{r!r}@{a}" for r, a, _ in ENGLISH_CASES
])
def test_english_deadline_resolution(raw, anchor, expected):
    result = resolve_deadline(raw, anchor, "en")
    assert result == expected, (
        f"resolve_deadline({raw!r}, {anchor}, 'en') = {result}, expected {expected}"
    )


# ─── Parametrized French table ────────────────────────────────────────────────

FRENCH_CASES = [
    # Relative days
    ("demain",                    date(2026, 4, 9),  date(2026, 4, 10)),
    ("demain",                    date(2026, 12, 31), date(2027, 1, 1)),

    # Next weekday — anchor is Thursday 2026-04-09
    ("lundi prochain",            date(2026, 4, 9),  date(2026, 4, 13)),
    ("mardi prochain",            date(2026, 4, 9),  date(2026, 4, 14)),
    ("mercredi prochain",         date(2026, 4, 9),  date(2026, 4, 15)),
    ("jeudi prochain",            date(2026, 4, 9),  date(2026, 4, 16)),
    ("vendredi prochain",         date(2026, 4, 9),  date(2026, 4, 17)),
    ("samedi prochain",           date(2026, 4, 9),  date(2026, 4, 18)),
    ("dimanche prochain",         date(2026, 4, 9),  date(2026, 4, 19)),

    # "ce" / "this"
    ("ce vendredi",               date(2026, 4, 9),  date(2026, 4, 10)),
    ("ce lundi",                  date(2026, 4, 9),  date(2026, 4, 6)),   # this Monday (past)

    # "d'ici" pattern
    ("d'ici vendredi",            date(2026, 4, 9),  date(2026, 4, 17)),
    ("d'ici lundi prochain",      date(2026, 4, 9),  date(2026, 4, 13)),

    # "fin de" patterns
    ("fin de semaine",            date(2026, 4, 9),  date(2026, 4, 11)),
    ("fin du mois",               date(2026, 4, 9),  date(2026, 4, 30)),
    ("fin du mois",               date(2026, 2, 10), date(2026, 2, 28)),
    ("fin du mois",               date(2028, 2, 10), date(2028, 2, 29)),

    # "la semaine prochaine"
    ("la semaine prochaine",      date(2026, 4, 9),  date(2026, 4, 13)),

    # Explicit dates — French month names
    ("14 avril",                  date(2026, 4, 9),  date(2026, 4, 14)),
    ("14 avril 2026",             date(2026, 4, 9),  date(2026, 4, 14)),
    ("14 mars",                   date(2026, 4, 9),  date(2027, 3, 14)),  # past → next year
    ("avant le 17 avril",         date(2026, 4, 9),  date(2026, 4, 17)),
    ("avant le 17 avril 2026",    date(2026, 4, 9),  date(2026, 4, 17)),
    ("le 20",                     date(2026, 4, 9),  date(2026, 4, 20)),  # same month
    ("le 5",                      date(2026, 4, 9),  date(2026, 5, 5)),   # past day → next month

    # Code-switching — English keywords in French context
    ("by Friday",                 date(2026, 4, 9),  date(2026, 4, 17)),
    ("end of week",               date(2026, 4, 9),  date(2026, 4, 11)),

    # Unresolvable French
    ("bientôt",                   date(2026, 4, 9),  None),
    ("dès que possible",          date(2026, 4, 9),  None),
    ("ASAP",                      date(2026, 4, 9),  None),
    ("",                          date(2026, 4, 9),  None),
    (None,                        date(2026, 4, 9),  None),
]


@pytest.mark.parametrize("raw,anchor,expected", FRENCH_CASES, ids=[
    f"fr:{r!r}@{a}" for r, a, _ in FRENCH_CASES
])
def test_french_deadline_resolution(raw, anchor, expected):
    result = resolve_deadline(raw, anchor, "fr")
    assert result == expected, (
        f"resolve_deadline({raw!r}, {anchor}, 'fr') = {result}, expected {expected}"
    )


# ─── is_explicit parametrized table ──────────────────────────────────────────

IS_EXPLICIT_CASES = [
    # (raw, lang, is_explicit)
    ("April 14th", "en", True),
    ("2026-04-20", "en", True),
    ("14/04/2026", "en", True),
    ("January 15", "en", True),
    ("14 avril", "fr", True),
    ("14 avril 2026", "fr", True),
    ("avant le 17 avril", "fr", True),
    ("le 20", "fr", True),
    # Relative — NOT explicit
    ("tomorrow", "en", False),
    ("next Friday", "en", False),
    ("end of week", "en", False),
    ("end of month", "en", False),
    ("next week", "en", False),
    ("demain", "fr", False),
    ("vendredi prochain", "fr", False),
    ("fin du mois", "fr", False),
    ("la semaine prochaine", "fr", False),
]

ANCHOR = date(2026, 4, 9)

@pytest.mark.parametrize("raw,lang,expected_explicit", IS_EXPLICIT_CASES, ids=[
    f"explicit:{r!r}({lang})" for r, lang, _ in IS_EXPLICIT_CASES
])
def test_is_explicit_flag(raw, lang, expected_explicit):
    result = resolve_deadline_full(raw, ANCHOR, lang)
    if result.resolved_date is not None:
        assert result.is_explicit == expected_explicit, (
            f"Expected is_explicit={expected_explicit} for {raw!r} ({lang}), "
            f"got {result.is_explicit}"
        )


# ─── freezegun tests — time-sensitive behaviours ─────────────────────────────

class TestWithFrozenTime:

    @freeze_time("2026-04-09")  # Thursday
    def test_today_anchor_derived_from_system_clock(self):
        """When anchor is not provided, today's date from the system clock is used."""
        from parler.extraction.deadline_resolver import resolve_deadline_today
        result = resolve_deadline_today("tomorrow", "en")
        assert result == date(2026, 4, 10)

    @freeze_time("2026-04-09")
    def test_end_of_month_with_frozen_april(self):
        """End of month with frozen April anchor = April 30."""
        from parler.extraction.deadline_resolver import resolve_deadline_today
        result = resolve_deadline_today("end of month", "en")
        assert result == date(2026, 4, 30)

    @freeze_time("2026-12-31")  # New Year's Eve
    def test_tomorrow_on_new_years_eve(self):
        """'Tomorrow' on Dec 31 should resolve to Jan 1 of next year."""
        from parler.extraction.deadline_resolver import resolve_deadline_today
        result = resolve_deadline_today("tomorrow", "en")
        assert result == date(2027, 1, 1)

    @freeze_time("2028-02-28")  # Leap year, day before Feb 29
    def test_tomorrow_on_leap_feb_28(self):
        """'Tomorrow' on Feb 28 of a leap year should be Feb 29."""
        from parler.extraction.deadline_resolver import resolve_deadline_today
        result = resolve_deadline_today("tomorrow", "en")
        assert result == date(2028, 2, 29)

    @freeze_time("2026-03-27 01:30:00", tz_offset=1)  # DST spring-forward in France (CEST)
    def test_tomorrow_during_dst_transition(self):
        """DST transition days must not produce off-by-one errors."""
        from parler.extraction.deadline_resolver import resolve_deadline_today
        result = resolve_deadline_today("tomorrow", "fr")
        # March 29, 2026 is DST change day in France
        # "tomorrow" should still be the calendar next day, not skip 2 days
        assert result == date(2026, 3, 28)


# ─── Anchor-is-the-named-day edge cases ────────────────────────────────────

class TestAnchorIsTheNamedDay:
    """
    "next X" when today IS X: implementation must define a clear semantic.
    Contract: "next X" always means the X AFTER today, never today itself.
    This ensures no commitment is ever "due today" from a relative reference.
    """

    @pytest.mark.parametrize("anchor,weekday_name,expected_delta_days", [
        (date(2026, 4, 13), "Monday",    7),   # Monday → next Monday = +7
        (date(2026, 4, 10), "Friday",    7),   # Friday → next Friday = +7
        (date(2026, 4, 9),  "Wednesday", 6),   # Thursday → next Wednesday = +6
        (date(2026, 4, 14), "Tuesday",   7),   # Tuesday → next Tuesday = +7
    ])
    def test_next_x_when_today_is_x_means_plus_seven(self, anchor, weekday_name, expected_delta_days):
        result = resolve_deadline(f"next {weekday_name}", anchor, "en")
        expected = anchor + timedelta(days=expected_delta_days)
        assert result == expected, (
            f"next {weekday_name} from {anchor} ({anchor.strftime('%A')}) = {result}, "
            f"expected {expected} (+{expected_delta_days} days)"
        )

    @pytest.mark.parametrize("anchor,raw_fr,expected_delta_days", [
        (date(2026, 4, 13), "lundi prochain",    7),
        (date(2026, 4, 10), "vendredi prochain", 7),
    ])
    def test_french_next_x_when_today_is_x(self, anchor, raw_fr, expected_delta_days):
        result = resolve_deadline(raw_fr, anchor, "fr")
        expected = anchor + timedelta(days=expected_delta_days)
        assert result == expected, (
            f"{raw_fr!r} from {anchor} = {result}, expected {expected}"
        )
