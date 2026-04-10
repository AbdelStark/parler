"""Mistral-backed decision extraction adapter."""

from __future__ import annotations

import json
import re
from dataclasses import replace
from datetime import UTC, date, datetime
from types import SimpleNamespace
from typing import Any, TypeVar

from ..errors import APIError
from ..local import LocalVoxtralRuntime, is_local_model, local_repo_id
from ..models import Commitment, Decision, DecisionLog, OpenQuestion, Rejection, Transcript
from ..prompts.extraction import DEFAULT_EXTRACTION_PROMPT_VERSION, get_extraction_prompt
from ..util.hashing import stable_fingerprint
from .cache import ExtractionCache
from .parser import parse_extraction_response, validate_decision_log

_SdkMistralClient: Any
T = TypeVar("T")
_LOCAL_JSON_SCHEMA_PROMPT = (
    "Local mode instructions:\n"
    "- Return JSON only. Prefer raw JSON without markdown fences.\n"
    "- Use exactly this shape:\n"
    '  {"decisions":[{"summary":str,"quote":str,"confidence":"high"|"medium","language":str|null}],'
    '"commitments":[{"owner":str|null,"action":str,"quote":str,"confidence":"high"|"medium","language":str|null}],'
    '"rejected":[{"summary":str,"quote":str,"confidence":"high"|"medium","language":str|null}],'
    '"open_questions":[{"question":str,"quote":str,"confidence":"high"|"medium","language":str|null}]}\n'
    "- Include every top-level key even when empty.\n"
    "- Extract explicit items only."
)
_LOCAL_ACKNOWLEDGEMENT_PREFIX_RE = re.compile(
    r"^(?:oui|yes|ok|okay|d'accord|sure|bien sûr|bien sur)[,\s:;-]+",
    re.IGNORECASE,
)
_LOCAL_ADDRESS_CANDIDATE_RE = re.compile(r"\b([A-ZÀ-ÖØ-Þ][\wÀ-ÖØ-öø-ÿ'-]+)\s*,")
_LOCAL_REJECTION_RE = re.compile(
    r"\b(?:reject|rejected|rejection|rejeter|rejetons|rejeté|rejetee|rejetée)\b",
    re.IGNORECASE,
)
_LOCAL_GROUP_DECISION_RE = re.compile(
    r"\b(?:we will|we'll|we are going to|nous allons|on va)\b",
    re.IGNORECASE,
)
_LOCAL_DECISION_CONTEXT_RE = re.compile(
    r"\b(?:decision|décision|board|council|conseil)\b",
    re.IGNORECASE,
)
_LOCAL_COMMITMENT_RE = re.compile(
    r"\b(?:je vais|i will|i'll|je m'en charge|je vais m'en charger|i can take it|i'll take it|i'll handle it)\b",
    re.IGNORECASE,
)
_LOCAL_QUESTION_PREFIX_RE = re.compile(
    r"^(?:et\s+la\s+\w+\s+question,\s*|and\s+the\s+\w+\s+question,\s*)",
    re.IGNORECASE,
)
_LOCAL_ADDRESS_STOPWORDS = {
    "alors",
    "bonjour",
    "du",
    "d",
    "et",
    "hey",
    "ok",
    "okay",
    "oui",
    "salut",
}

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


def _normalize_json_response(raw_content: str) -> str:
    stripped = raw_content.strip()
    if not stripped:
        return ""
    fenced_match = re.match(r"^```(?:json)?\s*(.*?)\s*```$", stripped, re.DOTALL | re.IGNORECASE)
    if fenced_match:
        stripped = fenced_match.group(1).strip()

    if stripped.startswith("{") and stripped.endswith("}"):
        return stripped

    first_brace = stripped.find("{")
    last_brace = stripped.rfind("}")
    if first_brace != -1 and last_brace != -1 and first_brace < last_brace:
        candidate = stripped[first_brace : last_brace + 1].strip()
        try:
            json.loads(candidate)
        except json.JSONDecodeError:
            return stripped
        return candidate
    return stripped


def _texts_overlap(left: str, right: str) -> bool:
    normalized_left = _normalize_text(left)
    normalized_right = _normalize_text(right)
    if not normalized_left or not normalized_right:
        return False
    return normalized_left in normalized_right or normalized_right in normalized_left


def _contains_rejection_language(text: str) -> bool:
    return bool(_LOCAL_REJECTION_RE.search(text))


def _normalize_question_text(text: str) -> str:
    return _LOCAL_QUESTION_PREFIX_RE.sub("", text.strip())


def _strip_acknowledgement_prefix(text: str) -> str:
    return _LOCAL_ACKNOWLEDGEMENT_PREFIX_RE.sub("", text.strip()).strip()


def _infer_addressed_name(text: str) -> str | None:
    matches = [str(candidate) for candidate in _LOCAL_ADDRESS_CANDIDATE_RE.findall(text)]
    for candidate in reversed(matches):
        if candidate.lower() not in _LOCAL_ADDRESS_STOPWORDS:
            return candidate
    return None


def _extract_commitment_action(text: str) -> str | None:
    stripped = _strip_acknowledgement_prefix(text)
    if not stripped:
        return None
    if not _LOCAL_COMMITMENT_RE.search(stripped):
        return None
    return stripped


def _extract_decision_summary(text: str, *, previous_text: str = "") -> str | None:
    sentences = [sentence.strip() for sentence in re.split(r"(?<=[.!?])\s+", text.strip())]
    for sentence in sentences:
        if not sentence or "?" in sentence:
            continue
        if not _LOCAL_GROUP_DECISION_RE.search(sentence):
            continue
        normalized = _normalize_text(sentence)
        if "commencer la réunion" in normalized or "start the meeting" in normalized:
            continue
        if (
            _LOCAL_DECISION_CONTEXT_RE.search(sentence)
            or _contains_rejection_language(previous_text)
            or normalized.startswith("nous allons donc")
            or normalized.startswith("we will continue")
            or normalized.startswith("we'll continue")
        ):
            return sentence
    return None


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
        self._local_runtime = (
            LocalVoxtralRuntime(local_repo_id(model)) if is_local_model(model) else None
        )
        self._client = None if self._local_runtime is not None else MistralClient(api_key=api_key)

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
        if self._local_runtime is not None:
            system_prompt = f"{system_prompt}\n\n{_LOCAL_JSON_SCHEMA_PROMPT}"
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
            if self._local_runtime is not None:
                response = None
                try:
                    raw_content = self._local_runtime.generate_text(
                        messages,
                        max_new_tokens=self.max_tokens,
                        temperature=self.temperature,
                    )
                except Exception as exc:
                    raise self._translate_api_error(exc) from exc
            else:
                try:
                    assert self._client is not None
                    response = self._client.chat.complete(**self._api_kwargs(messages))
                except Exception as exc:
                    raise self._translate_api_error(exc) from exc
                raw_content = _response_content(response)
            normalized_content = _normalize_json_response(raw_content)
            try:
                payload = json.loads(normalized_content)
            except json.JSONDecodeError:
                if attempt == 0:
                    continue
                return parse_extraction_response(
                    None,
                    meeting_date=meeting_date,
                    model=self.model,
                    prompt_version=self.prompt_version,
                    extracted_at=extracted_at,
                    input_tokens=(
                        _usage_token(response, "input_tokens", "prompt_tokens")
                        if response is not None
                        else 0
                    ),
                    output_tokens=(
                        _usage_token(response, "output_tokens", "completion_tokens")
                        if response is not None
                        else 0
                    ),
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
                input_tokens=(
                    _usage_token(response, "input_tokens", "prompt_tokens")
                    if response is not None
                    else 0
                ),
                output_tokens=(
                    _usage_token(response, "output_tokens", "completion_tokens")
                    if response is not None
                    else 0
                ),
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

    def _postprocess_local_log(
        self,
        log: DecisionLog,
        *,
        transcript: Transcript,
    ) -> DecisionLog:
        decisions: list[Decision] = []
        for decision_item in log.decisions:
            recovered_summary = _extract_decision_summary(decision_item.quote)
            if recovered_summary is not None and _contains_rejection_language(
                decision_item.summary
            ):
                decision_item = replace(
                    decision_item,
                    summary=recovered_summary,
                    quote=recovered_summary,
                )
            decisions.append(decision_item)

        open_questions: list[OpenQuestion] = []
        for question_item in log.open_questions:
            if "?" in question_item.quote:
                normalized_question = _normalize_question_text(question_item.quote)
                if normalized_question:
                    question_item = replace(question_item, question=normalized_question)
            open_questions.append(question_item)

        recovered_decisions = list(decisions)
        recovered_commitments = list(log.commitments)
        recovered_rejections = list(log.rejected)
        recovered_questions = list(open_questions)
        default_language = transcript.language or "en"

        for index, segment in enumerate(transcript.segments):
            previous_text = transcript.segments[index - 1].text if index > 0 else ""
            next_text = (
                transcript.segments[index + 1].text if index + 1 < len(transcript.segments) else ""
            )
            language = segment.language or default_language

            decision_summary = _extract_decision_summary(segment.text, previous_text=previous_text)
            if decision_summary is not None and not any(
                _texts_overlap(decision_summary, item.summary)
                or _texts_overlap(decision_summary, item.quote)
                for item in recovered_decisions
            ):
                recovered_decisions.append(
                    Decision(
                        id="D0",
                        summary=decision_summary,
                        timestamp_s=segment.start_s,
                        speaker=segment.speaker_id,
                        quote=segment.text.strip(),
                        confidence="high",
                        language=language,
                    )
                )

            commitment_action = _extract_commitment_action(segment.text)
            if commitment_action is not None and "?" in previous_text:
                owner = _infer_addressed_name(previous_text) or "Unknown"
                if not any(
                    _texts_overlap(commitment_action, item.action)
                    or _texts_overlap(commitment_action, item.quote)
                    for item in recovered_commitments
                ):
                    recovered_commitments.append(
                        Commitment(
                            id="C0",
                            owner=owner,
                            action=commitment_action,
                            deadline=None,
                            timestamp_s=segment.start_s,
                            quote=segment.text.strip(),
                            confidence="high",
                            language=language,
                        )
                    )

            normalized_question = _normalize_question_text(segment.text)
            if (
                "?" in segment.text
                and _extract_commitment_action(next_text) is None
                and not any(
                    _texts_overlap(normalized_question, item.question)
                    or _texts_overlap(normalized_question, item.quote)
                    for item in recovered_questions
                )
            ):
                recovered_questions.append(
                    OpenQuestion(
                        id="Q0",
                        question=normalized_question,
                        asked_by=None,
                        timestamp_s=segment.start_s,
                        quote=segment.text.strip(),
                        language=language,
                        stakes=None,
                        confidence="high",
                    )
                )

            normalized_segment = _normalize_text(segment.text)
            if (
                _contains_rejection_language(segment.text)
                and "cette décision" in normalized_segment
                and recovered_rejections
            ):
                continue
            if _contains_rejection_language(segment.text) and not any(
                _texts_overlap(segment.text, item.summary)
                or _texts_overlap(segment.text, item.quote)
                for item in recovered_rejections
            ):
                recovered_rejections.append(
                    Rejection(
                        id="R0",
                        summary=segment.text.strip(),
                        timestamp_s=segment.start_s,
                        quote=segment.text.strip(),
                        confidence="high",
                        language=language,
                        reason=None,
                    )
                )

        return validate_decision_log(
            replace(
                log,
                decisions=self._dedupe_collection(
                    tuple(recovered_decisions),
                    key_fn=self._decision_key,
                ),
                commitments=self._dedupe_collection(
                    tuple(recovered_commitments),
                    key_fn=self._commitment_key,
                ),
                rejected=self._dedupe_collection(
                    tuple(recovered_rejections),
                    key_fn=self._rejection_key,
                ),
                open_questions=self._dedupe_collection(
                    tuple(recovered_questions),
                    key_fn=self._question_key,
                ),
            )
        )

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
        if self._local_runtime is not None:
            merged = self._postprocess_local_log(merged, transcript=transcript)

        if self.cache is not None:
            self.cache.store(transcript_hash, self.prompt_version, merged, **cache_kwargs)

        return merged


__all__ = ["DecisionExtractor", "MistralClient"]
