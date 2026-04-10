"""Mistral-backed decision extraction adapter."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, date, datetime
from types import SimpleNamespace
from typing import Any, TypeVar

from ..errors import APIError
from ..models import Commitment, Decision, DecisionLog, OpenQuestion, Rejection, Transcript
from ..prompts.extraction import DEFAULT_EXTRACTION_PROMPT_VERSION, get_extraction_prompt
from ..util.hashing import stable_fingerprint
from .cache import ExtractionCache
from .parser import parse_extraction_response, validate_decision_log

_SdkMistralClient: Any
T = TypeVar("T")

try:
    from mistralai.client import Mistral as _SdkMistralClient
except ImportError:  # pragma: no cover - dependency issue
    _SdkMistralClient = None


class MistralClient:
    """Compatibility wrapper around the installed Mistral SDK."""

    def __init__(self, *, api_key: str):
        if _SdkMistralClient is None:  # pragma: no cover - dependency issue
            raise RuntimeError("mistralai SDK is not installed")
        client = _SdkMistralClient(api_key=api_key)
        complete = getattr(client.chat, "complete", None)
        if complete is None:  # pragma: no cover - future SDK drift
            raise RuntimeError("Installed mistralai SDK does not expose chat.complete")
        self.chat = SimpleNamespace(complete=complete)


def _timestamp() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _normalize_text(value: str) -> str:
    return " ".join(value.lower().split())


def _response_content(response: object) -> str:
    choices = getattr(response, "choices", None)
    if not choices:
        return ""
    message = getattr(choices[0], "message", None)
    if message is None:
        return ""
    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                parts.append(str(item.get("text", "")))
            else:
                parts.append(str(getattr(item, "text", "")))
        return "".join(parts)
    return str(content)


def _usage_token(response: object, *names: str) -> int:
    usage = getattr(response, "usage", None)
    if usage is None:
        return 0
    for name in names:
        if hasattr(usage, name):
            value = getattr(usage, name)
            if isinstance(value, int):
                return value
    return 0


class DecisionExtractor:
    """Extract canonical decision logs from transcripts via Mistral Chat."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str = "mistral-medium-latest",
        prompt_version: str = DEFAULT_EXTRACTION_PROMPT_VERSION,
        temperature: float = 0.0,
        max_tokens: int = 4096,
        multi_pass_threshold: int = 25_000,
        cache: ExtractionCache | None = None,
    ):
        self.api_key = api_key
        self.model = model
        self.prompt_version = prompt_version
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.multi_pass_threshold = multi_pass_threshold
        self.cache = cache
        self._client = MistralClient(api_key=api_key)

    def _transcript_hash(self, transcript: Transcript) -> str:
        return stable_fingerprint(
            "transcript",
            transcript.text,
            transcript.language,
            transcript.duration_s,
            transcript.content_hash,
        )

    def _passes(self, transcript: Transcript) -> list[str]:
        text = transcript.text.strip()
        if len(transcript.segments) > 40:
            window_size = 30
            overlap = 5
            windows: list[str] = []
            start = 0
            while start < len(transcript.segments):
                end = min(len(transcript.segments), start + window_size)
                window_text = " ".join(
                    segment.text
                    for segment in transcript.segments[start:end]
                    if segment.text.strip()
                ).strip()
                if window_text:
                    windows.append(window_text)
                if end >= len(transcript.segments):
                    break
                start = max(end - overlap, start + 1)
            if len(windows) > 1:
                return windows
        if len(text) <= self.multi_pass_threshold:
            return [text]
        window = self.multi_pass_threshold
        overlap = max(window // 10, 1)
        passes: list[str] = []
        start = 0
        while start < len(text):
            end = min(len(text), start + window)
            passes.append(text[start:end])
            if end >= len(text):
                break
            start = max(end - overlap, start + 1)
        return passes

    def _messages(
        self,
        *,
        transcript_text: str,
        meeting_date: date | None,
        participants: list[str] | None,
        pass_index: int,
        pass_count: int,
    ) -> list[dict[str, str]]:
        system_prompt = get_extraction_prompt(self.prompt_version)
        participant_block = ", ".join(participants or []) or "(none provided)"
        meeting_date_block = meeting_date.isoformat() if meeting_date is not None else "unknown"
        user_prompt = "\n".join(
            [
                "Extract the canonical decision log from this meeting transcript.",
                f"Meeting date anchor: {meeting_date_block}",
                f"Known participants: {participant_block}",
                f"Pass: {pass_index}/{pass_count}",
                "Return JSON only. Use null for unknown fields.",
                "",
                "TRANSCRIPT:",
                transcript_text,
            ]
        )
        return [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

    def _api_kwargs(self, messages: list[dict[str, str]]) -> dict[str, object]:
        return {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "response_format": {"type": "json_object"},
        }

    def _translate_api_error(self, exc: BaseException) -> APIError:
        status_code = getattr(exc, "status_code", None)
        if status_code == 401:
            return APIError("API authentication failed")
        if status_code == 403:
            return APIError("API authorization failed")
        if status_code == 429:
            return APIError("Rate limit exceeded during extraction")
        return APIError(str(exc))

    def _extract_single_pass(
        self,
        *,
        transcript_text: str,
        transcript: Transcript,
        meeting_date: date | None,
        participants: list[str] | None,
        pass_index: int,
        pass_count: int,
    ) -> DecisionLog:
        messages = self._messages(
            transcript_text=transcript_text,
            meeting_date=meeting_date,
            participants=participants,
            pass_index=pass_index,
            pass_count=pass_count,
        )
        extracted_at = _timestamp()

        for attempt in range(2):
            try:
                response = self._client.chat.complete(**self._api_kwargs(messages))
            except Exception as exc:
                raise self._translate_api_error(exc) from exc

            raw_content = _response_content(response)
            try:
                payload = json.loads(raw_content)
            except json.JSONDecodeError:
                if attempt == 0:
                    continue
                return parse_extraction_response(
                    None,
                    meeting_date=meeting_date,
                    model=self.model,
                    prompt_version=self.prompt_version,
                    extracted_at=extracted_at,
                    input_tokens=_usage_token(response, "input_tokens", "prompt_tokens"),
                    output_tokens=_usage_token(response, "output_tokens", "completion_tokens"),
                    pass_count=1,
                    default_language=transcript.language or "en",
                    allowed_languages=transcript.detected_languages,
                )

            return parse_extraction_response(
                payload,
                meeting_date=meeting_date,
                model=self.model,
                prompt_version=self.prompt_version,
                extracted_at=extracted_at,
                input_tokens=_usage_token(response, "input_tokens", "prompt_tokens"),
                output_tokens=_usage_token(response, "output_tokens", "completion_tokens"),
                pass_count=1,
                default_language=transcript.language or "en",
                allowed_languages=transcript.detected_languages,
            )

        return parse_extraction_response(
            None,
            meeting_date=meeting_date,
            model=self.model,
            prompt_version=self.prompt_version,
            extracted_at=extracted_at,
            pass_count=1,
            default_language=transcript.language or "en",
            allowed_languages=transcript.detected_languages,
        )

    def _decision_key(self, item: Decision) -> tuple[str, str, float | None]:
        return (_normalize_text(item.summary), item.speaker or "", item.timestamp_s)

    def _commitment_key(self, item: Commitment) -> tuple[str, str, str | None]:
        deadline = (
            item.deadline.resolved_date.isoformat()
            if item.deadline and item.deadline.resolved_date
            else None
        )
        return (_normalize_text(item.owner), _normalize_text(item.action), deadline)

    def _rejection_key(self, item: Rejection) -> tuple[str, str]:
        return (_normalize_text(item.summary), _normalize_text(item.reason or ""))

    def _question_key(self, item: OpenQuestion) -> tuple[str, str]:
        return (_normalize_text(item.question), _normalize_text(item.asked_by or ""))

    def _dedupe_collection(
        self,
        items: tuple[T, ...],
        *,
        key_fn: Any,
    ) -> tuple[T, ...]:
        seen: set[object] = set()
        deduped: list[T] = []
        for item in items:
            key = key_fn(item)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(item)
        return tuple(deduped)

    def _merge_logs(self, logs: list[DecisionLog], *, meeting_date: date | None) -> DecisionLog:
        decisions = self._dedupe_collection(
            tuple(item for log in logs for item in log.decisions),
            key_fn=self._decision_key,
        )
        commitments = self._dedupe_collection(
            tuple(item for log in logs for item in log.commitments),
            key_fn=self._commitment_key,
        )
        rejected = self._dedupe_collection(
            tuple(item for log in logs for item in log.rejected),
            key_fn=self._rejection_key,
        )
        open_questions = self._dedupe_collection(
            tuple(item for log in logs for item in log.open_questions),
            key_fn=self._question_key,
        )
        parse_warnings = tuple(warning for log in logs for warning in log.metadata.parse_warnings)
        merged = DecisionLog(
            decisions=decisions,
            commitments=commitments,
            rejected=rejected,
            open_questions=open_questions,
            metadata=replace(
                logs[-1].metadata,
                meeting_date=meeting_date,
                input_tokens=sum(log.metadata.input_tokens for log in logs),
                output_tokens=sum(log.metadata.output_tokens for log in logs),
                pass_count=len(logs),
                parse_warnings=parse_warnings,
            ),
        )
        return validate_decision_log(merged)

    def extract(
        self,
        transcript: Transcript,
        *,
        meeting_date: date | None = None,
        participants: list[str] | None = None,
    ) -> DecisionLog:
        transcript_hash = self._transcript_hash(transcript)
        cache_kwargs = {
            "model": self.model,
            "meeting_date_anchor": meeting_date.isoformat() if meeting_date else "",
        }
        if self.cache is not None:
            cached = self.cache.get(transcript_hash, self.prompt_version, **cache_kwargs)
            if cached is not None:
                return cached

        pass_texts = self._passes(transcript)
        logs = [
            self._extract_single_pass(
                transcript_text=text,
                transcript=transcript,
                meeting_date=meeting_date,
                participants=participants,
                pass_index=index,
                pass_count=len(pass_texts),
            )
            for index, text in enumerate(pass_texts, start=1)
        ]
        merged = self._merge_logs(logs, meeting_date=meeting_date)

        if self.cache is not None:
            self.cache.store(transcript_hash, self.prompt_version, merged, **cache_kwargs)

        return merged


__all__ = ["DecisionExtractor", "MistralClient"]
