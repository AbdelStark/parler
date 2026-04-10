"""Voxtral transcription adapter with cache and retry support."""

from __future__ import annotations

import math
import mimetypes
import re
import sys
from collections.abc import Iterable
from contextlib import suppress
from dataclasses import dataclass
from inspect import Parameter, signature
from types import ModuleType, SimpleNamespace
from typing import Any, cast

from ..errors import APIError
from ..local import LocalVoxtralRuntime, is_local_model, local_repo_id
from ..models import AudioFile, RawVoxtralChunkResponse, Transcript, TranscriptSegment
from ..util.language import detect_language_with_codeswitch, normalize_language_code
from ..util.retry import RetryConfig, RetryExhaustedError, is_retriable_http_status, with_retry
from .assembler import assemble_chunks
from .cache import TranscriptCache
from .quality import TranscriptQualityChecker, TranscriptQualityReport

_SdkMistralClient: Any
MistralFile: Any
httpx: Any | None = None
_LOCAL_SEGMENT_BOUNDARY = re.compile(r"(?<=[.!?])\s+")

try:
    from mistralai.client import Mistral as _SdkMistralClient
    from mistralai.client.models.file import File as MistralFile
except ImportError:  # pragma: no cover - older SDKs
    _SdkMistralClient = None
    MistralFile = None

with suppress(ImportError):  # pragma: no cover - optional dependency detail
    import httpx


class APIStatusError(Exception):
    """Compatibility error for older `mistralai.exceptions.APIStatusError` tests."""

    def __init__(self, message: str, *, status_code: int, body: object | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.body = body


def _install_exception_compat() -> None:
    try:
        __import__("mistralai.exceptions")
    except ModuleNotFoundError:
        module = ModuleType("mistralai.exceptions")
        module.__dict__["APIStatusError"] = APIStatusError
        sys.modules["mistralai.exceptions"] = module


_install_exception_compat()


class MistralClient:
    """Compatibility wrapper around the installed Mistral SDK."""

    def __init__(self, *, api_key: str):
        if _SdkMistralClient is None:  # pragma: no cover - dependency issue
            raise RuntimeError("mistralai SDK is not installed")
        client = _SdkMistralClient(api_key=api_key)
        create = getattr(client.audio.transcriptions, "create", None)
        if create is None:
            create = client.audio.transcriptions.complete
        self.audio = SimpleNamespace(transcriptions=SimpleNamespace(create=create))


def _filter_supported_kwargs(method: object, kwargs: dict[str, object]) -> dict[str, object]:
    try:
        parameters = signature(cast(Any, method)).parameters.values()
    except (TypeError, ValueError):
        return kwargs

    if any(parameter.kind == Parameter.VAR_KEYWORD for parameter in parameters):
        return kwargs

    accepted = {
        parameter.name
        for parameter in parameters
        if parameter.kind in {Parameter.POSITIONAL_OR_KEYWORD, Parameter.KEYWORD_ONLY}
    }
    return {key: value for key, value in kwargs.items() if key in accepted}


@dataclass(frozen=True)
class _ChunkSpec:
    index: int
    start_s: float
    duration_s: float


def _context_bias_fingerprint(context_bias: Iterable[str] | None) -> str:
    if not context_bias:
        return ""
    return "|".join(sorted(set(context_bias)))


def _language_fingerprint(languages: Iterable[str] | None) -> str | tuple[str, ...]:
    if not languages:
        return "auto"
    return tuple(languages)


def _logprob_to_confidence(avg_logprob: float | None) -> float:
    if avg_logprob is None:
        return 0.0
    clipped = max(-1.5, min(0.0, avg_logprob))
    return (clipped + 1.5) / 1.5


def _required_value(raw: object, name: str) -> Any:
    if isinstance(raw, dict):
        return raw[name]
    return getattr(raw, name)


def _normalize_optional_value(value: Any) -> Any | None:
    if value is None:
        return None
    if value.__class__.__name__ == "Unset":
        return None
    return value


def _optional_value(raw: object, *names: str) -> Any | None:
    if isinstance(raw, dict):
        for name in names:
            if name in raw:
                return _normalize_optional_value(raw[name])
        return None
    raw_dict = getattr(raw, "__dict__", {})
    for name in names:
        if name in raw_dict:
            return _normalize_optional_value(raw_dict[name])
    return None


def _requested_languages(languages: Iterable[str] | None) -> tuple[str, ...]:
    normalized: list[str] = []
    for language in languages or ():
        code = normalize_language_code(language)
        if code and code not in normalized:
            normalized.append(code)
    return tuple(normalized)


def _split_local_segments(text: str, *, duration_s: float) -> tuple[tuple[float, float, str], ...]:
    normalized = " ".join(text.split()).strip()
    if not normalized:
        return ()
    parts = [part.strip() for part in _LOCAL_SEGMENT_BOUNDARY.split(normalized) if part.strip()]
    if not parts:
        parts = [normalized]
    total_weight = sum(max(len(part), 1) for part in parts)
    cursor = 0.0
    segments: list[tuple[float, float, str]] = []
    for index, part in enumerate(parts):
        if index == len(parts) - 1:
            end = duration_s
        else:
            span = duration_s * (max(len(part), 1) / total_weight)
            end = min(duration_s, cursor + span)
        segments.append((cursor, end, part))
        cursor = end
    return tuple(segments)


def _normalize_chunk_response(
    raw_response: object,
    *,
    chunk_start_s: float,
    chunk_duration_s: float,
    requested_languages: tuple[str, ...] = (),
) -> RawVoxtralChunkResponse:
    raw_segments = cast(Iterable[object], _required_value(raw_response, "segments"))
    segments: list[TranscriptSegment] = []
    default_language = normalize_language_code(_optional_value(raw_response, "language"))
    if default_language is None and len(requested_languages) == 1:
        default_language = requested_languages[0]

    for index, raw_segment in enumerate(raw_segments):
        speaker_id = _optional_value(raw_segment, "speaker_id", "speaker", "speaker_label")
        raw_segment_id = _optional_value(raw_segment, "id")
        avg_logprob = _optional_value(raw_segment, "avg_logprob")
        score = _optional_value(raw_segment, "score")
        confidence = float(score) if score is not None else _logprob_to_confidence(avg_logprob)
        segment_text = str(_required_value(raw_segment, "text"))
        segment_language = normalize_language_code(_optional_value(raw_segment, "language"))
        if segment_language is None:
            segment_language, code_switch = detect_language_with_codeswitch(
                segment_text,
                candidates=requested_languages,
                default=default_language,
            )
        else:
            _, code_switch = detect_language_with_codeswitch(
                segment_text,
                candidates=requested_languages,
                default=segment_language,
            )
        segments.append(
            TranscriptSegment(
                id=int(raw_segment_id) if raw_segment_id is not None else index,
                start_s=float(_required_value(raw_segment, "start")) + chunk_start_s,
                end_s=float(_required_value(raw_segment, "end")) + chunk_start_s,
                text=segment_text,
                language=segment_language or "",
                speaker_id=str(speaker_id) if speaker_id else None,
                speaker_confidence="high" if speaker_id else None,
                confidence=confidence,
                no_speech_prob=float(_optional_value(raw_segment, "no_speech_prob") or 0.0),
                code_switch=code_switch,
                words=None,
            )
        )

        if default_language is None and segment_language:
            default_language = segment_language

    return RawVoxtralChunkResponse(
        text=str(_required_value(raw_response, "text")),
        language=default_language or "",
        duration=float(_optional_value(raw_response, "duration") or chunk_duration_s)
        + chunk_start_s,
        segments=tuple(segments),
    )


class VoxtralTranscriber:
    """Transcribe audio through Voxtral with retry, cache, and quality evaluation."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str = "voxtral-mini-latest",
        max_chunk_s: int = 600,
        max_retries: int = 3,
        cache: TranscriptCache | None = None,
        request_mode: str = "timestamp_first",
        diarize: bool = True,
        timestamp_granularity_mode: str = "segment",
        preprocessing_fingerprint: str = "raw",
        context_bias: Iterable[str] | None = None,
        timeout_ms: int | None = None,
    ):
        self.api_key = api_key
        self.model = model
        self.max_chunk_s = max_chunk_s
        self.max_retries = max_retries
        self.cache = cache
        self.request_mode = request_mode
        self.diarize = diarize
        self.timestamp_granularity_mode = timestamp_granularity_mode
        self.preprocessing_fingerprint = preprocessing_fingerprint
        self.context_bias = tuple(context_bias or ())
        self.timeout_ms = timeout_ms
        self.quality_checker = TranscriptQualityChecker()
        self.last_quality_report: TranscriptQualityReport | None = None
        self._local_runtime = (
            LocalVoxtralRuntime(local_repo_id(model)) if is_local_model(model) else None
        )
        self._client = None if self._local_runtime is not None else MistralClient(api_key=api_key)

    def _transcribe_local(
        self, audio_file: AudioFile, languages: Iterable[str] | None
    ) -> Transcript:
        assert self._local_runtime is not None
        requested_languages = _requested_languages(languages)
        requested_language = requested_languages[0] if len(requested_languages) == 1 else None
        transcript_text = self._local_runtime.transcribe_file(
            audio_file.path,
            language=requested_language,
        )
        normalized_text = " ".join(transcript_text.split()).strip()
        default_language = requested_language
        detected_language, _ = detect_language_with_codeswitch(
            normalized_text,
            candidates=requested_languages,
            default=default_language,
        )
        segment_specs = _split_local_segments(normalized_text, duration_s=audio_file.duration_s)
        segments = tuple(
            TranscriptSegment(
                id=index,
                start_s=start_s,
                end_s=end_s,
                text=segment_text,
                language=detect_language_with_codeswitch(
                    segment_text,
                    candidates=requested_languages,
                    default=detected_language,
                )[0]
                or detected_language
                or "",
                speaker_id=None,
                speaker_confidence=None,
                confidence=0.0,
                no_speech_prob=0.0,
                code_switch=detect_language_with_codeswitch(
                    segment_text,
                    candidates=requested_languages,
                    default=detected_language,
                )[1],
                words=None,
            )
            for index, (start_s, end_s, segment_text) in enumerate(segment_specs)
        )
        return Transcript(
            text=normalized_text,
            language=detected_language or requested_language or "",
            duration_s=audio_file.duration_s,
            segments=segments,
            model=self.model,
            content_hash=audio_file.content_hash,
        )

    def _chunk_specs(self, audio_file: AudioFile) -> list[_ChunkSpec]:
        chunk_count = max(1, math.ceil(audio_file.duration_s / self.max_chunk_s))
        specs: list[_ChunkSpec] = []
        for index in range(chunk_count):
            start = index * self.max_chunk_s
            duration = min(self.max_chunk_s, max(audio_file.duration_s - start, 0.0))
            specs.append(
                _ChunkSpec(
                    index=index, start_s=start, duration_s=duration or float(self.max_chunk_s)
                )
            )
        return specs

    def _file_argument(self, audio_file: AudioFile) -> object:
        content_type = mimetypes.guess_type(audio_file.path.name)[0] or "application/octet-stream"
        if audio_file.path.exists():
            return MistralFile(
                file_name=audio_file.path.name,
                content=audio_file.path.open("rb"),
                content_type=content_type,
            )
        return {
            "file_name": audio_file.path.name,
            "content": b"",
            "content_type": content_type,
        }

    def _request_kwargs(
        self,
        audio_file: AudioFile,
        *,
        languages: Iterable[str] | None,
        chunk: _ChunkSpec,
    ) -> dict[str, object]:
        requested_languages = _requested_languages(languages)
        kwargs: dict[str, object] = {
            "model": self.model,
            "file": self._file_argument(audio_file),
            "diarize": self.diarize,
            "context_bias": list(self.context_bias) or None,
            "timeout_ms": self.timeout_ms,
        }
        if self.request_mode == "timestamp_first":
            kwargs["timestamp_granularities"] = [self.timestamp_granularity_mode]
        if len(requested_languages) == 1:
            kwargs["language"] = requested_languages[0]

        # Logical chunk metadata for mocks and future chunk-file generation.
        kwargs["start_time"] = chunk.start_s
        kwargs["chunk_duration_s"] = chunk.duration_s
        return kwargs

    def _translate_api_error(self, exc: BaseException) -> APIError:
        status_code = getattr(exc, "status_code", None)
        if status_code == 401:
            return APIError("API authentication failed")
        if status_code == 403:
            return APIError("API authorization failed")
        if status_code == 429:
            return APIError(f"Rate limit exceeded after {self.max_retries} retries")
        return APIError(str(exc))

    def _transcribe_chunk(
        self,
        audio_file: AudioFile,
        *,
        languages: Iterable[str] | None,
        chunk: _ChunkSpec,
    ) -> RawVoxtralChunkResponse:
        def request() -> object:
            kwargs = self._request_kwargs(audio_file, languages=languages, chunk=chunk)
            assert self._client is not None
            create = self._client.audio.transcriptions.create
            file_arg = kwargs.get("file")
            content = getattr(file_arg, "content", None)
            try:
                return create(**_filter_supported_kwargs(create, kwargs))
            except APIStatusError as exc:
                if is_retriable_http_status(exc.status_code):
                    raise
                raise self._translate_api_error(exc) from exc
            finally:
                if hasattr(content, "close"):
                    cast(Any, content).close()

        retriable_exceptions: list[type[BaseException]] = [
            APIStatusError,
            TimeoutError,
            ConnectionError,
        ]
        if httpx is not None:
            retriable_exceptions.append(httpx.TimeoutException)
            retriable_exceptions.append(httpx.NetworkError)

        try:
            raw_response = with_retry(
                request,
                config=RetryConfig(
                    max_retries=self.max_retries,
                    retriable_exceptions=tuple(retriable_exceptions),
                ),
            )
        except RetryExhaustedError as exc:
            raise self._translate_api_error(exc.last_exception) from exc
        except TimeoutError as exc:
            raise APIError("Timeout during transcription") from exc

        return _normalize_chunk_response(
            raw_response,
            chunk_start_s=chunk.start_s,
            chunk_duration_s=chunk.duration_s,
            requested_languages=_requested_languages(languages),
        )

    def transcribe(
        self, audio_file: AudioFile, languages: Iterable[str] | None = None
    ) -> Transcript:
        cache_kwargs = {
            "request_mode": self.request_mode,
            "diarize": self.diarize,
            "timestamp_granularity_mode": self.timestamp_granularity_mode,
            "preprocessing_fingerprint": self.preprocessing_fingerprint,
            "context_bias_fingerprint": _context_bias_fingerprint(self.context_bias),
            "language_fingerprint": _language_fingerprint(languages),
            "normalization_version": "v2",
        }
        if self.cache is not None:
            cached = self.cache.get(audio_file.content_hash, self.model, **cache_kwargs)
            if cached is not None:
                self.last_quality_report = self.quality_checker.evaluate(cached)
                return cached

        if self._local_runtime is not None:
            transcript = self._transcribe_local(audio_file, languages)
        else:
            chunk_responses = [
                self._transcribe_chunk(audio_file, languages=languages, chunk=chunk)
                for chunk in self._chunk_specs(audio_file)
            ]
            transcript = assemble_chunks(
                chunk_responses,
                content_hash=audio_file.content_hash,
                model=self.model,
            )
        self.last_quality_report = self.quality_checker.evaluate(transcript)

        if self.cache is not None:
            self.cache.store(audio_file.content_hash, self.model, transcript, **cache_kwargs)

        return transcript


__all__ = ["APIStatusError", "MistralClient", "VoxtralTranscriber"]
