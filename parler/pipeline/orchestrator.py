"""Minimal orchestrator and checkpoint/state surface."""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum, auto
import time
from pathlib import Path
from typing import Any, Callable

from ..config import ParlerConfig
from ..errors import APIError, ProcessingError
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
from ..rendering.renderer import OutputFormat, RenderConfig, ReportRenderer
from ..util.serialization import read_json, to_jsonable, write_json_atomic


class AudioIngester:
    def ingest(self, input_path: Path) -> AudioFile:  # pragma: no cover - implementation follows later
        raise NotImplementedError("AudioIngester is not implemented yet")


class VoxtralTranscriber:
    def transcribe(self, audio_file: AudioFile) -> Transcript:  # pragma: no cover - implementation follows later
        raise NotImplementedError("VoxtralTranscriber is not implemented yet")


class SpeakerAttributor:
    def attribute(
        self,
        transcript: Transcript,
        *,
        participants: list[str] | None = None,
        anonymize: bool = False,
    ) -> Transcript:  # pragma: no cover - implementation follows later
        raise NotImplementedError("SpeakerAttributor is not implemented yet")


class DecisionExtractor:
    def extract(
        self,
        transcript: Transcript,
        *,
        meeting_date=None,
        participants: list[str] | None = None,
    ) -> DecisionLog:  # pragma: no cover - implementation follows later
        raise NotImplementedError("DecisionExtractor is not implemented yet")


def estimate_cost(audio_file: AudioFile, config: ParlerConfig) -> float:
    del audio_file, config
    return 0.0


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

    def _with_stage(self, stage: PipelineStage, **changes: Any) -> "ProcessingState":
        return replace(self, completed_stages=self.completed_stages | {stage}, **changes)

    def with_audio_file(self, audio_file: AudioFile) -> "ProcessingState":
        return replace(self, audio_file=audio_file)

    def with_transcript(self, transcript: Transcript) -> "ProcessingState":
        return self._with_stage(PipelineStage.TRANSCRIBE, transcript=transcript)

    def with_attributed_transcript(self, transcript: Transcript) -> "ProcessingState":
        return self._with_stage(PipelineStage.ATTRIBUTE, attributed_transcript=transcript)

    def with_decision_log(self, decision_log: DecisionLog) -> "ProcessingState":
        return self._with_stage(PipelineStage.EXTRACT, decision_log=decision_log)

    def with_report(self, report: str) -> "ProcessingState":
        return self._with_stage(PipelineStage.RENDER, report=report)


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


def _transcript_from_dict(data: dict[str, Any]) -> Transcript:
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
    return ExtractionMetadata(
        model=data.get("model", ""),
        prompt_version=data.get("prompt_version", ""),
        meeting_date=None,
        extracted_at=data.get("extracted_at", ""),
        input_tokens=data.get("input_tokens", 0),
        output_tokens=data.get("output_tokens", 0),
        pass_count=data.get("pass_count", 1),
        parse_warnings=tuple(data.get("parse_warnings", ())),
    )


def _decision_log_from_dict(data: dict[str, Any]) -> DecisionLog:
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
                        resolved_date=None,
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


class PipelineOrchestrator:
    def __init__(self, config: ParlerConfig):
        self.config = config

    def _run_stage(
        self,
        stage: PipelineStage,
        callback: Callable[[], Any],
        *,
        on_stage_start: Callable[[PipelineStage], None] | None = None,
        on_stage_complete: Callable[[PipelineStage, float], None] | None = None,
    ) -> Any:
        if on_stage_start is not None:
            on_stage_start(stage)
        start = time.perf_counter()
        result = callback()
        duration = time.perf_counter() - start
        if on_stage_complete is not None:
            on_stage_complete(stage, duration)
        return result

    def _checkpoint_data(self, state: ProcessingState) -> dict[str, Any]:
        payload = {
            "audio_hash": state.audio_file.content_hash if state.audio_file else None,
            "completed_stages": [stage.name for stage in sorted(state.completed_stages, key=lambda item: item.value)],
        }
        if state.transcript is not None:
            payload["transcript"] = to_jsonable(state.transcript)
        if state.attributed_transcript is not None:
            payload["attributed_transcript"] = to_jsonable(state.attributed_transcript)
        if state.decision_log is not None:
            payload["decision_log"] = to_jsonable(state.decision_log)
        if state.report is not None:
            payload["report"] = state.report
        return payload

    def _save_checkpoint(self, state: ProcessingState) -> None:
        if state.checkpoint_path is None:
            return
        write_json_atomic(state.checkpoint_path, self._checkpoint_data(state))

    def _load_checkpoint(self, checkpoint_path: Path, audio_file: AudioFile) -> ProcessingState:
        raw = read_json(checkpoint_path)
        checkpoint_hash = raw.get("audio_hash")
        if checkpoint_hash and checkpoint_hash != audio_file.content_hash:
            raise ProcessingError("checkpoint audio changed or mismatch detected")
        completed = frozenset(PipelineStage[name] for name in raw.get("completed_stages", []))
        return ProcessingState(
            audio_file=audio_file,
            transcript=_transcript_from_dict(raw["transcript"]) if raw.get("transcript") else None,
            attributed_transcript=(
                _transcript_from_dict(raw["attributed_transcript"])
                if raw.get("attributed_transcript")
                else None
            ),
            decision_log=(
                _decision_log_from_dict(raw["decision_log"]) if raw.get("decision_log") else None
            ),
            report=raw.get("report"),
            completed_stages=completed,
            checkpoint_path=checkpoint_path,
        )

    def run(
        self,
        input_path: str | Path,
        *,
        transcribe_only: bool = False,
        no_diarize: bool = False,
        checkpoint_path: Path | None = None,
        resume: bool = False,
        on_cost_confirm: Callable[[float], bool] | None = None,
        on_stage_start: Callable[[PipelineStage], None] | None = None,
        on_stage_complete: Callable[[PipelineStage, float], None] | None = None,
    ) -> ProcessingState | None:
        input_file = Path(input_path)
        checkpoint = checkpoint_path
        if checkpoint is None and resume:
            checkpoint = Path(".parler-state.json")

        ingester = AudioIngester()
        audio_file = self._run_stage(
            PipelineStage.INGEST,
            lambda: ingester.ingest(input_file),
            on_stage_start=on_stage_start,
            on_stage_complete=on_stage_complete,
        )
        state = ProcessingState(
            audio_file=audio_file,
            transcript=None,
            attributed_transcript=None,
            decision_log=None,
            report=None,
            completed_stages=frozenset(),
            checkpoint_path=checkpoint,
        ).with_audio_file(audio_file)

        if resume and checkpoint is not None and checkpoint.exists():
            state = self._load_checkpoint(checkpoint, audio_file)

        if PipelineStage.TRANSCRIBE not in state.completed_stages:
            estimated_cost = estimate_cost(audio_file, self.config)
            if (
                estimated_cost > self.config.cost.confirm_above_usd
                and on_cost_confirm is not None
                and not on_cost_confirm(estimated_cost)
            ):
                return None

            transcriber = VoxtralTranscriber()
            transcript = self._run_stage(
                PipelineStage.TRANSCRIBE,
                lambda: transcriber.transcribe(audio_file),
                on_stage_start=on_stage_start,
                on_stage_complete=on_stage_complete,
            )
            state = state.with_transcript(transcript)
            self._save_checkpoint(state)

        if transcribe_only:
            return state

        working_transcript = state.attributed_transcript or state.transcript
        if not no_diarize and PipelineStage.ATTRIBUTE not in state.completed_stages:
            attributor = SpeakerAttributor()
            try:
                attributed = self._run_stage(
                    PipelineStage.ATTRIBUTE,
                    lambda: attributor.attribute(
                        state.transcript,
                        participants=self.config.participants,
                        anonymize=self.config.output.anonymize_speakers,
                    ),
                    on_stage_start=on_stage_start,
                    on_stage_complete=on_stage_complete,
                )
                state = state.with_attributed_transcript(attributed)
                working_transcript = attributed
            except ProcessingError:
                working_transcript = state.transcript

        if PipelineStage.EXTRACT not in state.completed_stages:
            extractor = DecisionExtractor()
            decision_log = self._run_stage(
                PipelineStage.EXTRACT,
                lambda: extractor.extract(
                    working_transcript,
                    meeting_date=self.config.meeting_date,
                    participants=self.config.participants,
                ),
                on_stage_start=on_stage_start,
                on_stage_complete=on_stage_complete,
            )
            state = state.with_decision_log(decision_log)
            self._save_checkpoint(state)

        if PipelineStage.RENDER not in state.completed_stages:
            renderer = ReportRenderer()
            report = self._run_stage(
                PipelineStage.RENDER,
                lambda: renderer.render(
                    state.decision_log,
                    RenderConfig(format=OutputFormat(self.config.output.format)),
                ),
                on_stage_start=on_stage_start,
                on_stage_complete=on_stage_complete,
            )
            state = state.with_report(report)

        return state
