"""Extraction cache implementation."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

from ..models import (
    Commitment,
    CommitmentDeadline,
    Decision,
    DecisionLog,
    ExtractionMetadata,
    OpenQuestion,
    Rejection,
)
from ..util.hashing import stable_fingerprint
from ..util.serialization import read_json, to_jsonable, write_json_atomic


def build_extraction_cache_key(
    transcript_hash: str,
    prompt_version: str,
    *,
    model: str = "mistral-large-latest",
    schema_version: str = "v1",
    meeting_date_anchor: str = "",
    extraction_policy_version: str = "v1",
    normalization_policy_version: str = "v4",
) -> str:
    return stable_fingerprint(
        "extraction",
        transcript_hash,
        prompt_version,
        model,
        schema_version,
        meeting_date_anchor,
        extraction_policy_version,
        normalization_policy_version,
    )


def _metadata_from_dict(data: dict[str, Any]) -> ExtractionMetadata:
    meeting_date_raw = data.get("meeting_date")
    meeting_date = date.fromisoformat(meeting_date_raw) if meeting_date_raw else None
    return ExtractionMetadata(
        model=str(data.get("model", "")),
        prompt_version=str(data.get("prompt_version", "")),
        meeting_date=meeting_date,
        extracted_at=str(data.get("extracted_at", "")),
        input_tokens=int(data.get("input_tokens", 0)),
        output_tokens=int(data.get("output_tokens", 0)),
        pass_count=int(data.get("pass_count", 1)),
        parse_warnings=tuple(str(item) for item in data.get("parse_warnings", [])),
    )


def _log_from_dict(data: dict[str, Any]) -> DecisionLog:
    return DecisionLog(
        decisions=tuple(
            Decision(
                id=str(item["id"]),
                summary=str(item["summary"]),
                timestamp_s=float(item["timestamp_s"])
                if item.get("timestamp_s") is not None
                else None,
                speaker=item.get("speaker"),
                confirmed_by=tuple(str(name) for name in item.get("confirmed_by", [])),
                quote=str(item.get("quote", "")),
                confidence=str(item.get("confidence", "medium")),  # type: ignore[arg-type]
                language=str(item.get("language", "en")),
            )
            for item in data.get("decisions", [])
        ),
        commitments=tuple(
            Commitment(
                id=str(item["id"]),
                owner=str(item["owner"]),
                action=str(item["action"]),
                deadline=(
                    CommitmentDeadline(
                        raw=str(item["deadline"]["raw"]),
                        resolved_date=(
                            date.fromisoformat(item["deadline"]["resolved_date"])
                            if item["deadline"].get("resolved_date")
                            else None
                        ),
                        is_explicit=bool(item["deadline"]["is_explicit"]),
                    )
                    if item.get("deadline")
                    else None
                ),
                timestamp_s=float(item["timestamp_s"])
                if item.get("timestamp_s") is not None
                else None,
                quote=str(item.get("quote", "")),
                confidence=str(item.get("confidence", "medium")),  # type: ignore[arg-type]
                language=str(item.get("language", "en")),
            )
            for item in data.get("commitments", [])
        ),
        rejected=tuple(
            Rejection(
                id=str(item["id"]),
                summary=str(item["summary"]),
                timestamp_s=float(item["timestamp_s"])
                if item.get("timestamp_s") is not None
                else None,
                quote=str(item.get("quote", "")),
                confidence=str(item.get("confidence", "medium")),  # type: ignore[arg-type]
                language=str(item.get("language", "en")),
                reason=item.get("reason"),
            )
            for item in data.get("rejected", [])
        ),
        open_questions=tuple(
            OpenQuestion(
                id=str(item["id"]),
                question=str(item["question"]),
                asked_by=item.get("asked_by"),
                timestamp_s=float(item["timestamp_s"])
                if item.get("timestamp_s") is not None
                else None,
                quote=str(item.get("quote", "")),
                language=str(item.get("language", "en")),
                stakes=item.get("stakes"),
                confidence=str(item.get("confidence", "medium")),  # type: ignore[arg-type]
            )
            for item in data.get("open_questions", [])
        ),
        metadata=_metadata_from_dict(data["metadata"]),
    )


class ExtractionCache:
    """JSON-backed extraction cache."""

    def __init__(self, *, cache_dir: Path):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _path_for(
        self, transcript_hash: str, prompt_version: str, **fingerprint_kwargs: Any
    ) -> Path:
        key = build_extraction_cache_key(transcript_hash, prompt_version, **fingerprint_kwargs)
        return self.cache_dir / f"{key}.json"

    def get(
        self, transcript_hash: str, prompt_version: str, **fingerprint_kwargs: Any
    ) -> DecisionLog | None:
        path = self._path_for(transcript_hash, prompt_version, **fingerprint_kwargs)
        if not path.exists():
            return None
        raw = read_json(path)
        return _log_from_dict(raw["decision_log"])

    def store(
        self, transcript_hash: str, prompt_version: str, log: DecisionLog, **fingerprint_kwargs: Any
    ) -> Path:
        path = self._path_for(transcript_hash, prompt_version, **fingerprint_kwargs)
        write_json_atomic(
            path,
            {
                "transcript_hash": transcript_hash,
                "prompt_version": prompt_version,
                "decision_log": to_jsonable(log),
            },
        )
        return path

    def clear(
        self,
        transcript_hash: str | None = None,
        prompt_version: str | None = None,
        **fingerprint_kwargs: Any,
    ) -> None:
        if transcript_hash is not None and prompt_version is not None:
            path = self._path_for(transcript_hash, prompt_version, **fingerprint_kwargs)
            if path.exists():
                path.unlink()
            return
        for entry in self.cache_dir.glob("*.json"):
            entry.unlink()

    def entry_count(self) -> int:
        return len(list(self.cache_dir.glob("*.json")))


__all__ = ["ExtractionCache", "build_extraction_cache_key"]
