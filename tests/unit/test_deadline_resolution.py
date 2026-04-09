"""
TDD specification: deadline_resolver.resolve_deadline()

The deadline resolver converts natural-language deadline references
(in French and English) into absolute ISO 8601 dates, relative to a
known meeting_date.

Design contract:
  - Input: raw string + meeting_date + language hint
  - Output: date | None (None = cannot resolve confidently)
  - Never raises for any input string (graceful degradation to None)
  - Deterministic: same inputs always produce same output
  - All resolutions use the FUTURE interpretation of relative dates
    (e.g., "next Monday" means the Monday AFTER meeting_date, never before)
"""

from datetime import date

from parler.extraction.deadline_resolver import resolve_deadline

# ─── Thursday 2026-04-09 is our test anchor ─────────────────────────────────
ANCHOR = date(2026, 4, 9)  # Thursday


class TestEnglishDeadlines:

    def test_tomorrow(self):
        assert resolve_deadline("tomorrow", ANCHOR, "en") == date(2026, 4, 10)

    def test_next_friday(self):
        assert resolve_deadline("next Friday", ANCHOR, "en") == date(2026, 4, 17)

    def test_by_friday(self):
        # "by Friday" == "next Friday" from a Thursday anchor
        assert resolve_deadline("by Friday", ANCHOR, "en") == date(2026, 4, 17)

    def test_this_friday(self):
        # "this Friday" from Thursday = same week's Friday = April 10
        assert resolve_deadline("this Friday", ANCHOR, "en") == date(2026, 4, 10)

    def test_end_of_week(self):
        assert resolve_deadline("end of week", ANCHOR, "en") == date(2026, 4, 11)

    def test_eow(self):
        assert resolve_deadline("EOW", ANCHOR, "en") == date(2026, 4, 11)

    def test_end_of_month(self):
        assert resolve_deadline("end of month", ANCHOR, "en") == date(2026, 4, 30)

    def test_next_monday(self):
        assert resolve_deadline("next Monday", ANCHOR, "en") == date(2026, 4, 13)

    def test_next_week(self):
        # "next week" = start of next week = Monday 2026-04-13
        assert resolve_deadline("next week", ANCHOR, "en") == date(2026, 4, 13)

    def test_explicit_date_with_month_name(self):
        assert resolve_deadline("April 14th", ANCHOR, "en") == date(2026, 4, 14)

    def test_explicit_date_numeric(self):
        assert resolve_deadline("14/04/2026", ANCHOR, "en") == date(2026, 4, 14)

    def test_explicit_iso_date(self):
        assert resolve_deadline("2026-04-20", ANCHOR, "en") == date(2026, 4, 20)

    def test_explicit_date_in_past_uses_current_year(self):
        # If month/day already passed this year, interpret as next year
        past_anchor = date(2026, 12, 1)
        result = resolve_deadline("January 15", past_anchor, "en")
        assert result == date(2027, 1, 15)

    def test_unresolvable_returns_none(self):
        assert resolve_deadline("sometime soon", ANCHOR, "en") is None

    def test_empty_string_returns_none(self):
        assert resolve_deadline("", ANCHOR, "en") is None

    def test_none_input_returns_none(self):
        assert resolve_deadline(None, ANCHOR, "en") is None

    def test_gibberish_returns_none(self):
        assert resolve_deadline("xyzzy quux blorp", ANCHOR, "en") is None


class TestFrenchDeadlines:

    def test_demain(self):
        assert resolve_deadline("demain", ANCHOR, "fr") == date(2026, 4, 10)

    def test_vendredi_prochain(self):
        assert resolve_deadline("vendredi prochain", ANCHOR, "fr") == date(2026, 4, 17)

    def test_d_ici_vendredi(self):
        assert resolve_deadline("d'ici vendredi", ANCHOR, "fr") == date(2026, 4, 17)

    def test_ce_vendredi(self):
        # "ce vendredi" (this Friday) from Thursday = April 10
        assert resolve_deadline("ce vendredi", ANCHOR, "fr") == date(2026, 4, 10)

    def test_fin_de_semaine(self):
        assert resolve_deadline("fin de semaine", ANCHOR, "fr") == date(2026, 4, 11)

    def test_la_semaine_prochaine(self):
        assert resolve_deadline("la semaine prochaine", ANCHOR, "fr") == date(2026, 4, 13)

    def test_lundi_prochain(self):
        assert resolve_deadline("lundi prochain", ANCHOR, "fr") == date(2026, 4, 13)

    def test_fin_du_mois(self):
        assert resolve_deadline("fin du mois", ANCHOR, "fr") == date(2026, 4, 30)

    def test_explicit_french_date(self):
        # "14 avril" in French
        assert resolve_deadline("14 avril", ANCHOR, "fr") == date(2026, 4, 14)

    def test_avant_le_explicit(self):
        # "avant le 17 avril"
        assert resolve_deadline("avant le 17 avril", ANCHOR, "fr") == date(2026, 4, 17)

    def test_explicit_day_only_uses_current_month(self):
        # "le 20" from April 9 = April 20
        assert resolve_deadline("le 20", ANCHOR, "fr") == date(2026, 4, 20)

    def test_explicit_day_past_in_month_uses_next_month(self):
        # "le 5" from April 9 = May 5 (already past in this month)
        assert resolve_deadline("le 5", ANCHOR, "fr") == date(2026, 5, 5)

    def test_unresolvable_french_returns_none(self):
        assert resolve_deadline("bientôt", ANCHOR, "fr") is None

    def test_dès_que_possible_returns_none(self):
        assert resolve_deadline("dès que possible", ANCHOR, "fr") is None


class TestEdgeCases:

    def test_anchor_is_friday(self):
        # From a Friday, "next Friday" = 7 days later, not today
        friday = date(2026, 4, 10)
        assert resolve_deadline("next Friday", friday, "en") == date(2026, 4, 17)

    def test_anchor_is_friday_by_friday(self):
        # "by Friday" on a Friday = today
        friday = date(2026, 4, 10)
        result = resolve_deadline("by Friday", friday, "en")
        assert result == date(2026, 4, 10)

    def test_case_insensitive(self):
        assert resolve_deadline("NEXT FRIDAY", ANCHOR, "en") == date(2026, 4, 17)
        assert resolve_deadline("Next Friday", ANCHOR, "en") == date(2026, 4, 17)

    def test_whitespace_stripped(self):
        assert resolve_deadline("  next Friday  ", ANCHOR, "en") == date(2026, 4, 17)

    def test_codeswitching_en_word_in_fr_context(self):
        # "by Friday" embedded in a French sentence
        assert resolve_deadline("by Friday", ANCHOR, "fr") == date(2026, 4, 17)

    def test_february_end_of_month(self):
        feb_anchor = date(2026, 2, 10)
        assert resolve_deadline("end of month", feb_anchor, "en") == date(2026, 2, 28)

    def test_february_leap_year(self):
        feb_anchor = date(2028, 2, 10)  # 2028 is a leap year
        assert resolve_deadline("end of month", feb_anchor, "en") == date(2028, 2, 29)


class TestIsExplicitFlag:
    """
    resolve_deadline returns a CommitmentDeadline with is_explicit flag.
    is_explicit=True means an exact date was stated; False means relative.
    """

    def test_exact_date_is_explicit(self):
        from parler.extraction.deadline_resolver import resolve_deadline_full
        result = resolve_deadline_full("April 14th", ANCHOR, "en")
        assert result.is_explicit is True
        assert result.resolved_date == date(2026, 4, 14)

    def test_relative_date_not_explicit(self):
        from parler.extraction.deadline_resolver import resolve_deadline_full
        result = resolve_deadline_full("next Friday", ANCHOR, "en")
        assert result.is_explicit is False

    def test_unresolvable_has_null_resolved_date(self):
        from parler.extraction.deadline_resolver import resolve_deadline_full
        result = resolve_deadline_full("sometime soon", ANCHOR, "en")
        assert result.resolved_date is None
        assert result.raw == "sometime soon"
