"""Defensive normalization for extraction responses."""

from __future__ import annotations

import json
import logging
from dataclasses import replace
from datetime import UTC, date, datetime
from typing import Any, Literal, cast

from ..models import (
    Commitment,
    CommitmentDeadline,
    Decision,
    DecisionLog,
    ExtractionMetadata,
    OpenQuestion,
    Rejection,
)
from ..util.language import detect_language, normalize_language_code
from .deadline_resolver import resolve_deadline_full

logger = logging.getLogger(__name__)

Confidence = Literal["high", "medium"]


def _timestamp() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _empty_log(
    *,
    meeting_date: date | None,
    model: str,
    prompt_version: str,
    extracted_at: str | None = None,
    input_tokens: int = 0,
    output_tokens: int = 0,
    pass_count: int = 1,
    parse_warnings: tuple[str, ...] = (),
) -> DecisionLog:
    return DecisionLog(
        decisions=(),
        commitments=(),
        rejected=(),
        open_questions=(),
        metadata=ExtractionMetadata(
            model=model,
            prompt_version=prompt_version,
            meeting_date=meeting_date,
            extracted_at=extracted_at or _timestamp(),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            pass_count=pass_count,
            parse_warnings=parse_warnings,
        ),
    )


def _coerce_payload(response: object) -> dict[str, Any]:
    if response is None:
        return {}
    if isinstance(response, dict):
        return cast(dict[str, Any], response)
    if isinstance(response, str):
        try:
            decoded = json.loads(response)
        except json.JSONDecodeError:
            return {}
        if isinstance(decoded, dict):
            return cast(dict[str, Any], decoded)
        return {}
    return {}


def _coerce_items(raw: object) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    items: list[dict[str, Any]] = []
    for item in raw:
        if isinstance(item, dict):
            items.append(cast(dict[str, Any], item))
    return items


def _clean_text(value: object) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _first_text(item: dict[str, Any], *keys: str) -> str:
    for key in keys:
        candidate = _clean_text(item.get(key))
        if candidate:
            return candidate
    return ""


def _meaningful_quote(item: dict[str, Any]) -> str:
    quote = _clean_text(item.get("quote"))
    if any(character.isalnum() for character in quote):
        return quote
    return ""


def _normalize_confidence(value: object) -> Confidence | None:
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized == "high":
            return "high"
        if normalized == "low":
            return None
        if normalized == "medium":
            return "medium"
    return "medium"


def _normalize_language(
    value: object,
    *,
    default: str = "en",
    fallback_text: str = "",
    allowed_languages: tuple[str, ...] = (),
) -> str:
    normalized = normalize_language_code(value)
    if normalized is not None:
        return normalized
    inferred = detect_language(
        fallback_text,
        candidates=allowed_languages,
        default=default,
    )
    return inferred or default


def _normalize_quote(value: object, *, warnings: list[str], item_label: str) -> str:
    quote = _clean_text(value)
    if not quote:
        warning = f"empty quote retained for {item_label}"
        logger.warning(warning)
        warnings.append(warning)
        return ""
    if len(quote) > 500:
        return f"{quote[:500]}..."
    return quote


def _normalize_timestamp(value: object) -> float | None:
    if value is None:
        return None
    try:
        normalized = float(cast(Any, value))
    except (TypeError, ValueError):
        return None
    if normalized < 0:
        return None
    return normalized


def _normalize_names(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    names: list[str] = []
    for item in value:
        candidate = _clean_text(item)
        if candidate and candidate not in names:
            names.append(candidate)
    return tuple(names)


def _coerce_deadline_dict(item: dict[str, Any]) -> dict[str, Any] | None:
    deadline = item.get("deadline")
    if isinstance(deadline, dict):
        return deadline

    raw_date = _first_text(item, "date", "due_date", "dueDate")
    if not raw_date:
        return None
    return {
        "raw": raw_date,
        "resolved_date": raw_date,
        "is_explicit": True,
    }


def _normalize_deadline(
    value: object,
    *,
    meeting_date: date | None,
    language: str,
) -> CommitmentDeadline | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        return None
    raw_value = _clean_text(value.get("raw"))
    if not raw_value:
        return None
    if meeting_date is None:
        resolved_date = None
        is_explicit = bool(value.get("is_explicit"))
        return CommitmentDeadline(
            raw=raw_value, resolved_date=resolved_date, is_explicit=is_explicit
        )
    resolved = resolve_deadline_full(raw_value, meeting_date, language)
    explicit_override = value.get("is_explicit")
    if isinstance(explicit_override, bool):
        return CommitmentDeadline(
            raw=raw_value,
            resolved_date=resolved.resolved_date,
            is_explicit=explicit_override
            if resolved.resolved_date is None
            else resolved.is_explicit,
        )
    return resolved


def _parse_decision(
    item: dict[str, Any],
    *,
    warnings: list[str],
    default_language: str,
    allowed_languages: tuple[str, ...],
) -> Decision | None:
    summary = _first_text(item, "summary", "outcome", "decision", "title")
    if not summary:
        return None
    confidence = _normalize_confidence(item.get("confidence"))
    if confidence is None:
        return None
    quote = _first_text(item, "quote", "excerpt", "outcome")
    language = _normalize_language(
        item.get("language"),
        default=default_language,
        fallback_text=quote or summary,
        allowed_languages=allowed_languages,
    )
    return Decision(
        id="D0",
        summary=summary,
        timestamp_s=_normalize_timestamp(
            item.get("timestamp_s")
            if item.get("timestamp_s") is not None
            else item.get("timestamp")
        ),
        speaker=_first_text(item, "speaker", "owner") or None,
        confirmed_by=_normalize_names(item.get("confirmed_by") or item.get("approvers")),
        quote=_normalize_quote(quote, warnings=warnings, item_label="decision"),
        confidence=confidence,
        language=language,
    )


def _parse_commitment(
    item: dict[str, Any],
    *,
    warnings: list[str],
    meeting_date: date | None,
    default_language: str,
    allowed_languages: tuple[str, ...],
) -> Commitment | None:
    action = _first_text(item, "action", "outcome", "summary", "task") or _meaningful_quote(item)
    if not action:
        return None
    confidence = _normalize_confidence(item.get("confidence"))
    if confidence is None:
        return None
    quote = _first_text(item, "quote", "excerpt", "outcome")
    language = _normalize_language(
        item.get("language"),
        default=default_language,
        fallback_text=quote or action,
        allowed_languages=allowed_languages,
    )
    owner = _first_text(item, "owner", "speaker", "assignee") or "Unknown"
    return Commitment(
        id="C0",
        owner=owner,
        action=action,
        deadline=_normalize_deadline(
            _coerce_deadline_dict(item),
            meeting_date=meeting_date,
            language=language,
        ),
        timestamp_s=_normalize_timestamp(
            item.get("timestamp_s")
            if item.get("timestamp_s") is not None
            else item.get("timestamp")
        ),
        quote=_normalize_quote(quote, warnings=warnings, item_label="commitment"),
        confidence=confidence,
        language=language,
    )


def _parse_rejection(
    item: dict[str, Any],
    *,
    warnings: list[str],
    default_language: str,
    allowed_languages: tuple[str, ...],
) -> Rejection | None:
    summary = _first_text(item, "summary", "proposal", "outcome") or _meaningful_quote(item)
    if not summary:
        return None
    confidence = _normalize_confidence(item.get("confidence"))
    if confidence is None:
        return None
    quote = _first_text(item, "quote", "excerpt", "outcome")
    language = _normalize_language(
        item.get("language"),
        default=default_language,
        fallback_text=quote or summary,
        allowed_languages=allowed_languages,
    )
    return Rejection(
        id="R0",
        summary=summary,
        timestamp_s=_normalize_timestamp(
            item.get("timestamp_s")
            if item.get("timestamp_s") is not None
            else item.get("timestamp")
        ),
        quote=_normalize_quote(quote, warnings=warnings, item_label="rejection"),
        confidence=confidence,
        language=language,
        reason=_first_text(item, "reason") or None,
    )


def _parse_open_question(
    item: dict[str, Any],
    *,
    warnings: list[str],
    default_language: str,
    allowed_languages: tuple[str, ...],
) -> OpenQuestion | None:
    question = _first_text(item, "question", "outcome", "summary") or _meaningful_quote(item)
    if not question:
        return None
    confidence = _normalize_confidence(item.get("confidence"))
    if confidence is None:
        return None
    quote = _first_text(item, "quote", "excerpt", "outcome")
    language = _normalize_language(
        item.get("language"),
        default=default_language,
        fallback_text=quote or question,
        allowed_languages=allowed_languages,
    )
    return OpenQuestion(
        id="Q0",
        question=question,
        asked_by=_first_text(item, "asked_by", "owner", "speaker") or None,
        timestamp_s=_normalize_timestamp(
            item.get("timestamp_s")
            if item.get("timestamp_s") is not None
            else item.get("timestamp")
        ),
        quote=_normalize_quote(quote, warnings=warnings, item_label="open question"),
        language=language,
        stakes=_clean_text(item.get("stakes")) or None,
        confidence=confidence,
    )


def validate_decision_log(log: DecisionLog) -> DecisionLog:
    decisions = tuple(
        replace(item, id=f"D{index}") for index, item in enumerate(log.decisions, start=1)
    )
    commitments = tuple(
        replace(item, id=f"C{index}") for index, item in enumerate(log.commitments, start=1)
    )
    rejected = tuple(
        replace(item, id=f"R{index}") for index, item in enumerate(log.rejected, start=1)
    )
    open_questions = tuple(
        replace(item, id=f"Q{index}") for index, item in enumerate(log.open_questions, start=1)
    )
    return replace(
        log,
        decisions=decisions,
        commitments=commitments,
        rejected=rejected,
        open_questions=open_questions,
    )


def parse_extraction_response(
    response: object,
    *,
    meeting_date: date | None,
    model: str = "mistral-large-latest",
    prompt_version: str = "v1.0",
    extracted_at: str | None = None,
    input_tokens: int = 0,
    output_tokens: int = 0,
    pass_count: int = 1,
    default_language: str = "en",
    allowed_languages: tuple[str, ...] = (),
) -> DecisionLog:
    try:
        payload = _coerce_payload(response)
        if "decision_log" in payload and isinstance(payload["decision_log"], dict):
            payload = cast(dict[str, Any], payload["decision_log"])
        warnings: list[str] = []

        decision_items: list[Decision] = []
        for raw_item in _coerce_items(payload.get("decisions")):
            parsed_decision = _parse_decision(
                raw_item,
                warnings=warnings,
                default_language=default_language,
                allowed_languages=allowed_languages,
            )
            if parsed_decision is not None:
                decision_items.append(parsed_decision)

        commitment_items: list[Commitment] = []
        for raw_item in _coerce_items(payload.get("commitments")):
            parsed_commitment = _parse_commitment(
                raw_item,
                warnings=warnings,
                meeting_date=meeting_date,
                default_language=default_language,
                allowed_languages=allowed_languages,
            )
            if parsed_commitment is not None:
                commitment_items.append(parsed_commitment)

        rejection_items: list[Rejection] = []
        rejected_payload = payload.get("rejected")
        if rejected_payload is None:
            rejected_payload = payload.get("rejections")
        for raw_item in _coerce_items(rejected_payload):
            parsed_rejection = _parse_rejection(
                raw_item,
                warnings=warnings,
                default_language=default_language,
                allowed_languages=allowed_languages,
            )
            if parsed_rejection is not None:
                rejection_items.append(parsed_rejection)

        question_items: list[OpenQuestion] = []
        question_payload = payload.get("open_questions")
        if question_payload is None:
            question_payload = payload.get("unresolved_open_questions")
        for raw_item in _coerce_items(question_payload):
            parsed_question = _parse_open_question(
                raw_item,
                warnings=warnings,
                default_language=default_language,
                allowed_languages=allowed_languages,
            )
            if parsed_question is not None:
                question_items.append(parsed_question)

        return validate_decision_log(
            DecisionLog(
                decisions=tuple(decision_items),
                commitments=tuple(commitment_items),
                rejected=tuple(rejection_items),
                open_questions=tuple(question_items),
                metadata=ExtractionMetadata(
                    model=model,
                    prompt_version=prompt_version,
                    meeting_date=meeting_date,
                    extracted_at=extracted_at or _timestamp(),
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    pass_count=pass_count,
                    parse_warnings=tuple(warnings),
                ),
            )
        )
    except Exception:
        return _empty_log(
            meeting_date=meeting_date,
            model=model,
            prompt_version=prompt_version,
            extracted_at=extracted_at,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            pass_count=pass_count,
        )


__all__ = ["parse_extraction_response", "validate_decision_log"]
