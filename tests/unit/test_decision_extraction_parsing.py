"""
TDD specification: DecisionExtractor._validate_output() and related parsing

The extraction parsing layer converts raw LLM JSON responses into typed
DecisionLog objects. It must:
  - Accept valid JSON and produce correct typed objects
  - Handle partial/malformed responses gracefully (never raise)
  - Validate all required fields and silently drop invalid items
  - Normalize language codes, confidence levels, and IDs
  - Never hallucinate data not present in the raw response
"""

import pytest
from datetime import date
from parler.extraction.parser import parse_extraction_response, validate_decision_log


# ─── Fixtures: sample raw LLM responses ─────────────────────────────────────

VALID_FULL_RESPONSE = {
    "decisions": [
        {
            "id": "D1",
            "summary": "Launch date set to May 15",
            "timestamp_s": 842.0,
            "speaker": "Pierre",
            "confirmed_by": ["Sophie"],
            "quote": "On part sur le 15 mai, c'est décidé.",
            "confidence": "high",
            "language": "fr",
        }
    ],
    "commitments": [
        {
            "id": "C1",
            "owner": "Sophie",
            "action": "Review the deployment checklist",
            "deadline": {"raw": "vendredi prochain", "resolved_date": None, "is_explicit": False},
            "timestamp_s": 848.0,
            "quote": "Je vais revoir le checklist avant vendredi prochain.",
            "confidence": "high",
            "language": "fr",
        }
    ],
    "rejected": [],
    "open_questions": [],
}

EMPTY_RESPONSE = {"decisions": [], "commitments": [], "rejected": [], "open_questions": []}

LIVE_VARIANT_RESPONSE = {
    "decisions": [
        {
            "outcome": "On part sur le 15 mai pour le lancement. C'est décidé.",
            "date": "2026-05-15",
            "owner": "Pierre",
            "confidence": "high",
        }
    ],
    "commitments": [
        {
            "outcome": "Je vais revoir la checklist avant vendredi prochain.",
            "date": "2026-04-17",
            "owner": "Sophie",
            "confidence": "high",
        }
    ],
    "rejections": [
        {
            "outcome": "Nous ne pouvons pas viser un lancement en mars.",
            "owner": "Pierre",
            "confidence": "high",
        }
    ],
    "open_questions": [
        {
            "outcome": "Qui prend la migration de la base de données ?",
            "owner": None,
            "confidence": "high",
        }
    ],
}


class TestValidFullResponse:
    def test_parses_valid_full_response(self):
        log = parse_extraction_response(VALID_FULL_RESPONSE, meeting_date=date(2026, 4, 9))
        assert len(log.decisions) == 1
        assert len(log.commitments) == 1
        assert len(log.rejected) == 0
        assert len(log.open_questions) == 0

    def test_decision_fields_populated_correctly(self):
        log = parse_extraction_response(VALID_FULL_RESPONSE, meeting_date=date(2026, 4, 9))
        d = log.decisions[0]
        assert d.id == "D1"
        assert "May 15" in d.summary or "15 mai" in d.summary or "15" in d.summary
        assert d.timestamp_s == pytest.approx(842.0)
        assert d.speaker == "Pierre"
        assert "Sophie" in d.confirmed_by
        assert d.confidence == "high"
        assert d.language == "fr"

    def test_commitment_deadline_resolved(self):
        log = parse_extraction_response(VALID_FULL_RESPONSE, meeting_date=date(2026, 4, 9))
        c = log.commitments[0]
        assert c.deadline is not None
        assert c.deadline.raw == "vendredi prochain"
        assert c.deadline.resolved_date == date(2026, 4, 17)
        assert c.deadline.is_explicit is False

    def test_empty_response_produces_empty_log(self):
        log = parse_extraction_response(EMPTY_RESPONSE, meeting_date=date(2026, 4, 9))
        assert log.is_empty
        assert log.total_items == 0


class TestMalformedResponses:
    def test_missing_top_level_key_handled_gracefully(self):
        """Response missing 'rejected' key — should default to empty list."""
        partial = {"decisions": [], "commitments": [], "open_questions": []}
        log = parse_extraction_response(partial, meeting_date=date(2026, 4, 9))
        assert log.rejected == ()

    def test_extra_unknown_fields_ignored(self):
        """LLM may add unexpected fields — they should be silently dropped."""
        response = {
            **VALID_FULL_RESPONSE,
            "extra_field": "unexpected",
            "debug_info": {"tokens": 500},
        }
        log = parse_extraction_response(response, meeting_date=date(2026, 4, 9))
        assert len(log.decisions) == 1  # parsing succeeded

    def test_decision_missing_required_summary_dropped(self):
        """A decision without a summary is incomplete and should be dropped."""
        response = {
            "decisions": [
                {
                    "id": "D1",
                    "confidence": "high",
                    "language": "fr",
                    "quote": "...",
                    "timestamp_s": None,
                    "speaker": None,
                    "confirmed_by": [],
                }
                # missing "summary"
            ],
            "commitments": [],
            "rejected": [],
            "open_questions": [],
        }
        log = parse_extraction_response(response, meeting_date=date(2026, 4, 9))
        assert len(log.decisions) == 0

    def test_commitment_missing_owner_defaults_to_unknown(self):
        """A commitment without an owner should still be included with owner='Unknown'."""
        response = {
            "decisions": [],
            "commitments": [
                {
                    "id": "C1",
                    "action": "Send the report",
                    "confidence": "medium",
                    "language": "en",
                    "quote": "I'll send the report.",
                    "timestamp_s": None,
                    "deadline": None,
                    # missing "owner"
                }
            ],
            "rejected": [],
            "open_questions": [],
        }
        log = parse_extraction_response(response, meeting_date=date(2026, 4, 9))
        assert len(log.commitments) == 1
        assert log.commitments[0].owner == "Unknown"

    def test_invalid_confidence_value_normalized(self):
        """Confidence values outside {"high", "medium"} should be normalized to "medium"."""
        response = {
            "decisions": [
                {
                    "id": "D1",
                    "summary": "Launch decided",
                    "confidence": "very_high",
                    "language": "en",
                    "quote": "...",
                    "timestamp_s": None,
                    "speaker": None,
                    "confirmed_by": [],
                }
            ],
            "commitments": [],
            "rejected": [],
            "open_questions": [],
        }
        log = parse_extraction_response(response, meeting_date=date(2026, 4, 9))
        # "very_high" is not a valid confidence level; normalize to "medium"
        assert log.decisions[0].confidence == "medium"

    def test_low_confidence_items_excluded(self):
        """Items with confidence 'low' should be excluded (only high/medium included)."""
        response = {
            "decisions": [
                {
                    "id": "D1",
                    "summary": "Maybe launch on May 15",
                    "confidence": "low",
                    "language": "en",
                    "quote": "...",
                    "timestamp_s": None,
                    "speaker": None,
                    "confirmed_by": [],
                }
            ],
            "commitments": [],
            "rejected": [],
            "open_questions": [],
        }
        log = parse_extraction_response(response, meeting_date=date(2026, 4, 9))
        assert len(log.decisions) == 0

    def test_completely_invalid_json_string_returns_empty_log(self):
        """If the LLM returns something that can't be parsed at all, return empty log."""
        log = parse_extraction_response("this is not json", meeting_date=date(2026, 4, 9))
        assert log.is_empty

    def test_null_response_returns_empty_log(self):
        log = parse_extraction_response(None, meeting_date=date(2026, 4, 9))
        assert log.is_empty

    def test_live_variant_schema_is_parsed(self):
        log = parse_extraction_response(
            LIVE_VARIANT_RESPONSE,
            meeting_date=date(2026, 4, 9),
            default_language="fr",
            allowed_languages=("fr", "en"),
        )
        assert len(log.decisions) == 1
        assert len(log.commitments) == 1
        assert len(log.rejected) == 1
        assert len(log.open_questions) == 1
        assert log.decisions[0].speaker == "Pierre"
        assert log.commitments[0].owner == "Sophie"
        assert log.commitments[0].deadline is not None
        assert log.commitments[0].deadline.resolved_date == date(2026, 4, 17)
        assert log.decisions[0].language == "fr"


class TestIDNormalization:
    def test_missing_ids_auto_assigned(self):
        """Items without IDs should get auto-assigned IDs in order."""
        response = {
            "decisions": [
                {
                    "summary": "Decision one",
                    "confidence": "high",
                    "language": "en",
                    "quote": "...",
                    "timestamp_s": None,
                    "speaker": None,
                    "confirmed_by": [],
                },
                {
                    "summary": "Decision two",
                    "confidence": "high",
                    "language": "en",
                    "quote": "...",
                    "timestamp_s": None,
                    "speaker": None,
                    "confirmed_by": [],
                },
            ],
            "commitments": [],
            "rejected": [],
            "open_questions": [],
        }
        log = parse_extraction_response(response, meeting_date=date(2026, 4, 9))
        assert log.decisions[0].id == "D1"
        assert log.decisions[1].id == "D2"

    def test_duplicate_ids_deduplicated(self):
        """If the LLM returns two items with the same ID, renumber them."""
        response = {
            "decisions": [
                {
                    "id": "D1",
                    "summary": "First",
                    "confidence": "high",
                    "language": "en",
                    "quote": ".",
                    "timestamp_s": None,
                    "speaker": None,
                    "confirmed_by": [],
                },
                {
                    "id": "D1",
                    "summary": "Second (duplicate ID)",
                    "confidence": "high",
                    "language": "en",
                    "quote": ".",
                    "timestamp_s": None,
                    "speaker": None,
                    "confirmed_by": [],
                },
            ],
            "commitments": [],
            "rejected": [],
            "open_questions": [],
        }
        log = parse_extraction_response(response, meeting_date=date(2026, 4, 9))
        ids = [d.id for d in log.decisions]
        assert len(set(ids)) == len(ids), f"Duplicate IDs in output: {ids}"


class TestQuoteValidation:
    def test_empty_quote_accepted_with_warning(self, caplog):
        """Empty quote is technically valid — item retained but warning logged."""
        import logging

        response = {
            "decisions": [
                {
                    "id": "D1",
                    "summary": "Decision",
                    "confidence": "high",
                    "language": "en",
                    "quote": "",
                    "timestamp_s": None,
                    "speaker": None,
                    "confirmed_by": [],
                }
            ],
            "commitments": [],
            "rejected": [],
            "open_questions": [],
        }
        with caplog.at_level(logging.WARNING):
            log = parse_extraction_response(response, meeting_date=date(2026, 4, 9))
        assert len(log.decisions) == 1
        assert any("empty quote" in record.message.lower() for record in caplog.records)

    def test_quote_over_500_chars_truncated(self):
        """Excessively long quotes should be truncated."""
        long_quote = "A" * 600
        response = {
            "decisions": [
                {
                    "id": "D1",
                    "summary": "Decision",
                    "confidence": "high",
                    "language": "en",
                    "quote": long_quote,
                    "timestamp_s": None,
                    "speaker": None,
                    "confirmed_by": [],
                }
            ],
            "commitments": [],
            "rejected": [],
            "open_questions": [],
        }
        log = parse_extraction_response(response, meeting_date=date(2026, 4, 9))
        assert len(log.decisions[0].quote) <= 503  # 500 + "..."
