"""Pipeline state objects and checkpoint serialization helpers."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date
from enum import Enum, auto
from pathlib import Path
from typing import Any

from ..errors import ProcessingError
from ..models import (
    AudioFile,
    Commitment,
    CommitmentDeadline,
    Decision,
    DecisionLog,
    ExtractionMetadata,
    OpenQuestion,
    Rejection,
    Transcript,
    TranscriptSegment,
)
from ..util.serialization import read_json, to_jsonable, write_json_atomic


class PipelineStage(Enum):
    INGEST = auto()
    TRANSCRIBE = auto()
    ATTRIBUTE = auto()
    EXTRACT = auto()
    RENDER = auto()


@dataclass(frozen=True)
class ProcessingState:
    audio_file: AudioFile | None
    transcript: Transcript | None
    attributed_transcript: Transcript | None
    decision_log: DecisionLog | None
    report: str | None
    completed_stages: frozenset[PipelineStage]
    checkpoint_path: Path | None

    def _with_stage(self, stage: PipelineStage, **changes: Any) -> ProcessingState:
        return replace(self, completed_stages=self.completed_stages | {stage}, **changes)

    def with_audio_file(self, audio_file: AudioFile) -> ProcessingState:
        return replace(self, audio_file=audio_file)

    def with_transcript(self, transcript: Transcript) -> ProcessingState:
        return self._with_stage(PipelineStage.TRANSCRIBE, transcript=transcript)

    def with_attributed_transcript(self, transcript: Transcript) -> ProcessingState:
        return self._with_stage(PipelineStage.ATTRIBUTE, attributed_transcript=transcript)

    def with_decision_log(self, decision_log: DecisionLog) -> ProcessingState:
        return self._with_stage(PipelineStage.EXTRACT, decision_log=decision_log)

    def with_report(self, report: str) -> ProcessingState:
        return self._with_stage(PipelineStage.RENDER, report=report)


def _audio_file_from_dict(data: dict[str, Any]) -> AudioFile:
    return AudioFile(
        path=Path(str(data["path"])),
        original_path=(
            Path(str(data["original_path"])) if data.get("original_path") is not None else None
        ),
        format=str(data["format"]),
        duration_s=float(data["duration_s"]),
        sample_rate=int(data["sample_rate"]),
        channels=int(data["channels"]),
        size_bytes=int(data["size_bytes"]),
        content_hash=str(data["content_hash"]),
    )


def _segment_from_dict(data: dict[str, Any]) -> TranscriptSegment:
    return TranscriptSegment(
        id=data["id"],
        start_s=data["start_s"],
        end_s=data["end_s"],
        text=data["text"],
        language=data["language"],
        speaker_id=data.get("speaker_id"),
        speaker_confidence=data.get("speaker_confidence"),
        confidence=data.get("confidence", 1.0),
        no_speech_prob=data.get("no_speech_prob", 0.0),
        code_switch=data.get("code_switch", False),
        words=None,
    )


def transcript_from_dict(data: dict[str, Any]) -> Transcript:
    return Transcript(
        text=data.get("text", ""),
        language=data.get("language", ""),
        duration_s=data.get("duration_s", 0.0),
        segments=tuple(_segment_from_dict(item) for item in data.get("segments", [])),
        detected_languages=tuple(data.get("detected_languages", ())),
        model=data.get("model", ""),
        content_hash=data.get("content_hash", ""),
    )


def _metadata_from_dict(data: dict[str, Any]) -> ExtractionMetadata:
    meeting_date_raw = data.get("meeting_date")
    meeting_date = date.fromisoformat(meeting_date_raw) if meeting_date_raw else None
    return ExtractionMetadata(
        model=data.get("model", ""),
        prompt_version=data.get("prompt_version", ""),
        meeting_date=meeting_date,
        extracted_at=data.get("extracted_at", ""),
        input_tokens=data.get("input_tokens", 0),
        output_tokens=data.get("output_tokens", 0),
        pass_count=data.get("pass_count", 1),
        parse_warnings=tuple(data.get("parse_warnings", ())),
    )


def decision_log_from_dict(data: dict[str, Any]) -> DecisionLog:
    return DecisionLog(
        decisions=tuple(
            Decision(
                id=item["id"],
                summary=item["summary"],
                timestamp_s=item.get("timestamp_s"),
                speaker=item.get("speaker"),
                confirmed_by=tuple(item.get("confirmed_by", ())),
                quote=item.get("quote", ""),
                confidence=item.get("confidence", "medium"),
                language=item.get("language", "en"),
            )
            for item in data.get("decisions", [])
        ),
        commitments=tuple(
            Commitment(
                id=item["id"],
                owner=item["owner"],
                action=item["action"],
                deadline=(
                    CommitmentDeadline(
                        raw=item["deadline"]["raw"],
                        resolved_date=(
                            date.fromisoformat(item["deadline"]["resolved_date"])
                            if item["deadline"].get("resolved_date")
                            else None
                        ),
                        is_explicit=item["deadline"]["is_explicit"],
                    )
                    if item.get("deadline")
                    else None
                ),
                timestamp_s=item.get("timestamp_s"),
                quote=item.get("quote", ""),
                confidence=item.get("confidence", "medium"),
                language=item.get("language", "en"),
            )
            for item in data.get("commitments", [])
        ),
        rejected=tuple(
            Rejection(
                id=item["id"],
                summary=item["summary"],
                timestamp_s=item.get("timestamp_s"),
                quote=item.get("quote", ""),
                confidence=item.get("confidence", "medium"),
                language=item.get("language", "en"),
                reason=item.get("reason"),
            )
            for item in data.get("rejected", [])
        ),
        open_questions=tuple(
            OpenQuestion(
                id=item["id"],
                question=item["question"],
                asked_by=item.get("asked_by"),
                timestamp_s=item.get("timestamp_s"),
                quote=item.get("quote", ""),
                language=item.get("language", "en"),
                stakes=item.get("stakes"),
                confidence=item.get("confidence", "medium"),
            )
            for item in data.get("open_questions", [])
        ),
        metadata=_metadata_from_dict(data.get("metadata", {})),
    )


def checkpoint_payload(state: ProcessingState) -> dict[str, Any]:
    payload = {
        "audio_hash": state.audio_file.content_hash if state.audio_file else None,
        "completed_stages": [
            stage.name for stage in sorted(state.completed_stages, key=lambda item: item.value)
        ],
    }
    if state.audio_file is not None:
        payload["audio_file"] = to_jsonable(state.audio_file)
    if state.transcript is not None:
        payload["transcript"] = to_jsonable(state.transcript)
    if state.attributed_transcript is not None:
        payload["attributed_transcript"] = to_jsonable(state.attributed_transcript)
    if state.decision_log is not None:
        payload["decision_log"] = to_jsonable(state.decision_log)
    if state.report is not None:
        payload["report"] = state.report
    return payload


def processing_state_from_dict(
    data: dict[str, Any],
    *,
    audio_file: AudioFile | None = None,
    checkpoint_path: Path | None = None,
) -> ProcessingState:
    completed = frozenset(PipelineStage[name] for name in data.get("completed_stages", []))
    payload_audio = data.get("audio_file")
    return ProcessingState(
        audio_file=audio_file
        or (_audio_file_from_dict(payload_audio) if isinstance(payload_audio, dict) else None),
        transcript=transcript_from_dict(data["transcript"]) if data.get("transcript") else None,
        attributed_transcript=(
            transcript_from_dict(data["attributed_transcript"])
            if data.get("attributed_transcript")
            else None
        ),
        decision_log=decision_log_from_dict(data["decision_log"])
        if data.get("decision_log")
        else None,
        report=data.get("report"),
        completed_stages=completed,
        checkpoint_path=checkpoint_path,
    )


def _validate_resumable_state(state: ProcessingState) -> None:
    completed = state.completed_stages

    if PipelineStage.ATTRIBUTE in completed and PipelineStage.TRANSCRIBE not in completed:
        raise ProcessingError("checkpoint is inconsistent: ATTRIBUTE completed without TRANSCRIBE")
    if PipelineStage.EXTRACT in completed and PipelineStage.TRANSCRIBE not in completed:
        raise ProcessingError("checkpoint is inconsistent: EXTRACT completed without TRANSCRIBE")
    if PipelineStage.RENDER in completed and PipelineStage.EXTRACT not in completed:
        raise ProcessingError("checkpoint is inconsistent: RENDER completed without EXTRACT")

    if PipelineStage.TRANSCRIBE in completed and state.transcript is None:
        raise ProcessingError(
            "checkpoint is inconsistent: TRANSCRIBE completed but transcript is missing"
        )
    if PipelineStage.ATTRIBUTE in completed and state.attributed_transcript is None:
        raise ProcessingError(
            "checkpoint is inconsistent: ATTRIBUTE completed but attributed transcript is missing"
        )
    if PipelineStage.EXTRACT in completed and state.decision_log is None:
        raise ProcessingError(
            "checkpoint is inconsistent: EXTRACT completed but decision log is missing"
        )
    if PipelineStage.RENDER in completed and state.report is None:
        raise ProcessingError("checkpoint is inconsistent: RENDER completed but report is missing")


def load_processing_state(
    checkpoint_path: Path,
    *,
    audio_file: AudioFile | None = None,
    expected_audio_hash: str | None = None,
) -> ProcessingState:
    raw = read_json(checkpoint_path)
    checkpoint_hash = raw.get("audio_hash")
    if expected_audio_hash is not None:
        if not checkpoint_hash:
            raise ProcessingError(
                "checkpoint is missing its audio hash and cannot be resumed safely"
            )
        if checkpoint_hash != expected_audio_hash:
            raise ProcessingError("checkpoint audio changed or mismatch detected")

    state = processing_state_from_dict(raw, audio_file=audio_file, checkpoint_path=checkpoint_path)
    _validate_resumable_state(state)
    return state


def save_processing_state(checkpoint_path: Path, state: ProcessingState) -> None:
    write_json_atomic(checkpoint_path, checkpoint_payload(state))


__all__ = [
    "PipelineStage",
    "ProcessingState",
    "checkpoint_payload",
    "decision_log_from_dict",
    "load_processing_state",
    "processing_state_from_dict",
    "save_processing_state",
    "transcript_from_dict",
]
