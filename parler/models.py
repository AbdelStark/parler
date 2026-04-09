"""Canonical domain models used throughout the project."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Literal


def _as_tuple(value: tuple[object, ...] | list[object] | None) -> tuple[object, ...]:
    if value is None:
        return ()
    if isinstance(value, tuple):
        return value
    return tuple(value)


@dataclass(frozen=True)
class AudioFile:
    path: Path
    original_path: Path | None
    format: str
    duration_s: float
    sample_rate: int
    channels: int
    size_bytes: int
    content_hash: str


@dataclass(frozen=True)
class AudioChunk:
    path: Path
    start_s: float
    end_s: float
    index: int = 0


@dataclass(frozen=True)
class ChunkPlan:
    chunks: tuple[AudioChunk, ...] = ()
    overlap_s: float = 0.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "chunks", _as_tuple(self.chunks))


@dataclass(frozen=True)
class TranscriptWord:
    word: str
    start_s: float
    end_s: float
    probability: float


@dataclass(frozen=True)
class TranscriptSegment:
    id: int
    start_s: float
    end_s: float
    text: str
    language: str
    speaker_id: str | None = None
    speaker_confidence: Literal["high", "medium", "low", "unknown"] | None = None
    confidence: float = 1.0
    no_speech_prob: float = 0.0
    code_switch: bool = False
    words: tuple[TranscriptWord, ...] | None = None

    def __post_init__(self) -> None:
        if self.words is not None:
            object.__setattr__(self, "words", _as_tuple(self.words))


@dataclass(frozen=True)
class RawVoxtralChunkResponse:
    text: str
    language: str
    duration: float
    segments: tuple[TranscriptSegment, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "segments", _as_tuple(self.segments))


@dataclass(frozen=True)
class Transcript:
    text: str
    language: str
    duration_s: float
    segments: tuple[TranscriptSegment, ...]
    detected_languages: tuple[str, ...] = ()
    model: str = ""
    content_hash: str = ""

    def __post_init__(self) -> None:
        segments = _as_tuple(self.segments)
        object.__setattr__(self, "segments", segments)
        if not self.detected_languages:
            seen: list[str] = []
            for segment in segments:
                if segment.language and segment.language not in seen:
                    seen.append(segment.language)
            if not seen and self.language:
                seen.append(self.language)
            object.__setattr__(self, "detected_languages", tuple(seen))

    @property
    def primary_language(self) -> str:
        return self.language


@dataclass(frozen=True)
class CommitmentDeadline:
    raw: str
    resolved_date: date | None
    is_explicit: bool


@dataclass(frozen=True)
class Decision:
    id: str
    summary: str
    timestamp_s: float | None
    speaker: str | None
    confirmed_by: tuple[str, ...] = ()
    quote: str = ""
    confidence: Literal["high", "medium"] = "medium"
    language: str = "en"

    def __post_init__(self) -> None:
        object.__setattr__(self, "confirmed_by", _as_tuple(self.confirmed_by))


@dataclass(frozen=True)
class Commitment:
    id: str
    owner: str
    action: str
    deadline: CommitmentDeadline | None
    timestamp_s: float | None
    quote: str = ""
    confidence: Literal["high", "medium"] = "medium"
    language: str = "en"


@dataclass(frozen=True)
class Rejection:
    id: str
    summary: str
    timestamp_s: float | None
    quote: str = ""
    confidence: Literal["high", "medium"] = "medium"
    language: str = "en"
    reason: str | None = None


@dataclass(frozen=True)
class OpenQuestion:
    id: str
    question: str
    asked_by: str | None = None
    timestamp_s: float | None = None
    quote: str = ""
    language: str = "en"
    stakes: str | None = None
    confidence: Literal["high", "medium"] = "medium"


@dataclass(frozen=True)
class ExtractionMetadata:
    model: str
    prompt_version: str
    meeting_date: date | None
    extracted_at: str
    input_tokens: int
    output_tokens: int
    pass_count: int = 1
    parse_warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "parse_warnings", _as_tuple(self.parse_warnings))


@dataclass(frozen=True)
class DecisionLog:
    decisions: tuple[Decision, ...]
    commitments: tuple[Commitment, ...]
    rejected: tuple[Rejection, ...]
    open_questions: tuple[OpenQuestion, ...]
    metadata: ExtractionMetadata

    def __post_init__(self) -> None:
        object.__setattr__(self, "decisions", _as_tuple(self.decisions))
        object.__setattr__(self, "commitments", _as_tuple(self.commitments))
        object.__setattr__(self, "rejected", _as_tuple(self.rejected))
        object.__setattr__(self, "open_questions", _as_tuple(self.open_questions))

    @property
    def total_items(self) -> int:
        return (
            len(self.decisions)
            + len(self.commitments)
            + len(self.rejected)
            + len(self.open_questions)
        )

    @property
    def is_empty(self) -> bool:
        return self.total_items == 0
