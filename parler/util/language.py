"""Language normalization and lightweight FR/EN heuristics."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable

_LANGUAGE_ALIASES = {
    "arabic": "ar",
    "ar": "ar",
    "de": "de",
    "english": "en",
    "en": "en",
    "es": "es",
    "french": "fr",
    "fr": "fr",
    "german": "de",
    "it": "it",
    "italian": "it",
    "ja": "ja",
    "japanese": "ja",
    "ko": "ko",
    "korean": "ko",
    "nl": "nl",
    "polish": "pl",
    "pl": "pl",
    "portuguese": "pt",
    "pt": "pt",
    "spanish": "es",
    "und": None,
    "unknown": None,
    "zh": "zh",
    "chinese": "zh",
}

_NULL_LANGUAGE_TOKENS = {"", "n/a", "none", "null", "unset", "undefined", "unknown"}

_ENGLISH_MARKERS = (
    "agreed",
    "analyst",
    "api",
    "approach",
    "call",
    "cleaner",
    "direct",
    "earnings",
    "faq",
    "friday",
    "going with",
    "gross margin",
    "guidance",
    "investor",
    "margin",
    "migration guide",
    "python sdk",
    "quarter",
    "quarterly",
    "ready",
    "regarding",
    "revenue",
    "review it",
    "sdk",
    "so",
    "sure",
    "welcome back",
)

_FRENCH_MARKERS = (
    "bonjour",
    "capacite",
    "c est decide",
    "d accord",
    "deploiement",
    "donnees",
    "equipe",
    "faisable",
    "je pense",
    "lancement",
    "mai",
    "nous",
    "on a decide",
    "on commence",
    "on devrait",
    "peux tu",
    "pret",
    "prochain",
    "proprietaire",
    "reunion",
    "revoir",
    "tres bien",
    "vendredi",
    "oui",
)


def _normalized_text(text: str) -> str:
    ascii_text = (
        unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii").lower()
    )
    return re.sub(r"[^a-z0-9']+", " ", ascii_text).strip()


def normalize_language_code(value: object, *, default: str | None = None) -> str | None:
    if value is None:
        return default
    normalized = str(value).strip().lower()
    if normalized in _NULL_LANGUAGE_TOKENS:
        return default
    if normalized in _LANGUAGE_ALIASES:
        return _LANGUAGE_ALIASES[normalized] or default
    if len(normalized) == 2 and normalized.isalpha():
        return normalized
    return default


def _language_candidates(candidates: Iterable[str] | None) -> tuple[str, ...]:
    normalized: list[str] = []
    for item in candidates or ():
        code = normalize_language_code(item)
        if code and code not in normalized:
            normalized.append(code)
    return tuple(normalized)


def _marker_score(text: str, markers: tuple[str, ...]) -> int:
    return sum(
        1
        for marker in markers
        if re.search(rf"(?<![a-z0-9']){re.escape(marker)}(?![a-z0-9'])", text) is not None
    )


def detect_language(
    text: str,
    *,
    candidates: Iterable[str] | None = None,
    default: str | None = None,
) -> str | None:
    guess, _ = detect_language_with_codeswitch(text, candidates=candidates, default=default)
    return guess


def detect_language_with_codeswitch(
    text: str,
    *,
    candidates: Iterable[str] | None = None,
    default: str | None = None,
) -> tuple[str | None, bool]:
    allowed = _language_candidates(candidates)
    if len(allowed) == 1:
        return allowed[0], False

    normalized = _normalized_text(text)
    if not normalized:
        if default is not None:
            return default, False
        if allowed:
            return allowed[0], False
        return None, False

    english_score = _marker_score(normalized, _ENGLISH_MARKERS)
    french_score = _marker_score(normalized, _FRENCH_MARKERS)
    code_switch = english_score > 0 and french_score > 0

    if allowed and "en" not in allowed:
        english_score = 0
    if allowed and "fr" not in allowed:
        french_score = 0

    if english_score > french_score and english_score > 0:
        return "en", code_switch
    if french_score > english_score and french_score > 0:
        return "fr", code_switch

    if code_switch:
        if default in {"fr", "en"}:
            return default, True
        if "fr" in allowed:
            return "fr", True
        if "en" in allowed:
            return "en", True

    if default is not None:
        return default, False
    if allowed:
        return allowed[0], False
    return None, False


__all__ = [
    "detect_language",
    "detect_language_with_codeswitch",
    "normalize_language_code",
]
