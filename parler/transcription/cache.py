"""Transcript cache implementation."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from ..models import Transcript, TranscriptSegment
from ..util.hashing import stable_fingerprint
from ..util.serialization import read_json, to_jsonable, write_json_atomic


def build_transcript_cache_key(
    content_hash: str,
    model: str,
    *,
    request_mode: str = "timestamp_first",
    diarize: bool = True,
    timestamp_granularity_mode: str = "segment",
    preprocessing_fingerprint: str = "raw",
    context_bias_fingerprint: str = "",
    language_fingerprint: str | tuple[str, ...] = "auto",
    normalization_version: str = "v2",
) -> str:
    return stable_fingerprint(
        "transcript",
        content_hash,
        model,
        request_mode,
        diarize,
        timestamp_granularity_mode,
        preprocessing_fingerprint,
        context_bias_fingerprint,
        language_fingerprint,
        normalization_version,
    )


def _segment_from_dict(data: dict[str, Any]) -> TranscriptSegment:
    return TranscriptSegment(
        id=int(data["id"]),
        start_s=float(data["start_s"]),
        end_s=float(data["end_s"]),
        text=str(data["text"]),
        language=str(data["language"]),
        speaker_id=data.get("speaker_id"),
        speaker_confidence=data.get("speaker_confidence"),
        confidence=float(data.get("confidence", 1.0)),
        no_speech_prob=float(data.get("no_speech_prob", 0.0)),
        code_switch=bool(data.get("code_switch", False)),
        words=None,
    )


def _transcript_from_dict(data: dict[str, Any]) -> Transcript:
    return Transcript(
        text=str(data.get("text", "")),
        language=str(data.get("language", "")),
        duration_s=float(data.get("duration_s", 0.0)),
        segments=tuple(_segment_from_dict(item) for item in data.get("segments", [])),
        detected_languages=tuple(str(item) for item in data.get("detected_languages", [])),
        model=str(data.get("model", "")),
        content_hash=str(data.get("content_hash", "")),
    )


class TranscriptCache:
    """JSON-backed transcript cache keyed by semantic request fingerprint."""

    def __init__(self, *, cache_dir: Path, ttl_days: int = 30):
        self.cache_dir = Path(cache_dir)
        self.ttl_days = ttl_days
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _path_for(self, content_hash: str, model: str, **fingerprint_kwargs: Any) -> Path:
        key = build_transcript_cache_key(content_hash, model, **fingerprint_kwargs)
        return self.cache_dir / f"{key}.json"

    def get(self, content_hash: str, model: str, **fingerprint_kwargs: Any) -> Transcript | None:
        path = self._path_for(content_hash, model, **fingerprint_kwargs)
        if not path.exists():
            return None
        if self.ttl_days >= 0:
            age = datetime.now() - datetime.fromtimestamp(path.stat().st_mtime)
            if age > timedelta(days=self.ttl_days):
                return None
        raw = read_json(path)
        return _transcript_from_dict(raw["transcript"])

    def store(
        self, content_hash: str, model: str, transcript: Transcript, **fingerprint_kwargs: Any
    ) -> Path:
        path = self._path_for(content_hash, model, **fingerprint_kwargs)
        write_json_atomic(
            path,
            {
                "content_hash": content_hash,
                "model": model,
                "transcript": to_jsonable(transcript),
            },
        )
        return path

    def clear(
        self, content_hash: str | None = None, model: str | None = None, **fingerprint_kwargs: Any
    ) -> None:
        if content_hash is not None and model is not None:
            path = self._path_for(content_hash, model, **fingerprint_kwargs)
            if path.exists():
                path.unlink()
            return
        for entry in self.cache_dir.glob("*.json"):
            entry.unlink()

    def entry_count(self) -> int:
        return len(list(self.cache_dir.glob("*.json")))


__all__ = ["TranscriptCache", "build_transcript_cache_key"]
