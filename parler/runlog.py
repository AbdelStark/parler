"""Local run-artifact recording for operator-driven parler workflows."""

from __future__ import annotations

import json
import shutil
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

from .models import DecisionLog, Transcript
from .pipeline.state import PipelineStage, ProcessingState
from .util.serialization import read_json, write_json_atomic

DEFAULT_RUN_DIRECTORY = Path(".parler-runs")


def _timestamp() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _to_iso(value: datetime) -> str:
    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def default_run_directory(project_root: Path | None = None) -> Path:
    return (project_root or Path.cwd()) / DEFAULT_RUN_DIRECTORY


def _summarize_transcript(transcript: Transcript | None) -> dict[str, Any] | None:
    if transcript is None:
        return None
    return {
        "language": transcript.language,
        "detected_languages": list(transcript.detected_languages),
        "duration_s": transcript.duration_s,
        "segment_count": len(transcript.segments),
        "text_length": len(transcript.text),
        "model": transcript.model,
    }


def _summarize_decision_log(decision_log: DecisionLog | None) -> dict[str, Any] | None:
    if decision_log is None:
        return None
    return {
        "decision_count": len(decision_log.decisions),
        "commitment_count": len(decision_log.commitments),
        "rejected_count": len(decision_log.rejected),
        "open_question_count": len(decision_log.open_questions),
        "model": decision_log.metadata.model,
        "prompt_version": decision_log.metadata.prompt_version,
        "pass_count": decision_log.metadata.pass_count,
    }


class RunRecorder:
    """Persist per-run summaries and event streams under `.parler-runs/`."""

    def __init__(
        self,
        *,
        command: str,
        project_root: Path,
        input_path: Path | None = None,
        config_path: Path | None = None,
        output_path: Path | None = None,
        checkpoint_path: Path | None = None,
        trace_id: str | None = None,
    ) -> None:
        self.command = command
        self.project_root = project_root.resolve()
        self.trace_id = trace_id or uuid4().hex[:12]
        self.run_dir = default_run_directory(self.project_root) / self.trace_id
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.summary_path = self.run_dir / "run.json"
        self.events_path = self.run_dir / "events.jsonl"
        self._current_stage: str | None = None
        self._summary: dict[str, Any] = {
            "trace_id": self.trace_id,
            "command": command,
            "status": "running",
            "started_at": _timestamp(),
            "finished_at": None,
            "project_root": str(self.project_root),
            "input_path": str(input_path.resolve()) if input_path is not None else None,
            "config_path": str(config_path.resolve()) if config_path is not None else None,
            "output_path": str(output_path.resolve()) if output_path is not None else None,
            "checkpoint_path": (
                str(checkpoint_path.resolve()) if checkpoint_path is not None else None
            ),
            "run_dir": str(self.run_dir),
            "summary_path": str(self.summary_path),
            "events_path": str(self.events_path),
            "stages": {},
            "result": None,
            "error": None,
        }
        self._write_summary()
        self._append_event("run_started")

    def stage_started(self, stage: PipelineStage) -> None:
        name = stage.name
        self._current_stage = name
        self._summary["stages"][name] = {
            "status": "running",
            "started_at": _timestamp(),
            "completed_at": None,
            "duration_s": None,
        }
        self._write_summary()
        self._append_event("stage_started", stage=name)

    def stage_completed(self, stage: PipelineStage, duration_s: float) -> None:
        name = stage.name
        stage_summary = self._summary["stages"].setdefault(name, {})
        stage_summary.update(
            {
                "status": "completed",
                "completed_at": _timestamp(),
                "duration_s": round(duration_s, 6),
            }
        )
        self._current_stage = None
        self._write_summary()
        self._append_event("stage_completed", stage=name, duration_s=round(duration_s, 6))

    def set_output_path(self, output_path: Path | None) -> None:
        self._summary["output_path"] = (
            str(output_path.resolve()) if output_path is not None else None
        )
        self._write_summary()

    def set_checkpoint_path(self, checkpoint_path: Path | None) -> None:
        self._summary["checkpoint_path"] = (
            str(checkpoint_path.resolve()) if checkpoint_path is not None else None
        )
        self._write_summary()

    def finish_success(self, state: ProcessingState) -> None:
        audio_summary = None
        if state.audio_file is not None:
            audio_summary = {
                "path": str(state.audio_file.path),
                "original_path": (
                    str(state.audio_file.original_path) if state.audio_file.original_path else None
                ),
                "format": state.audio_file.format,
                "duration_s": state.audio_file.duration_s,
            }
        self._summary["status"] = "completed"
        self._summary["finished_at"] = _timestamp()
        self._summary["result"] = {
            "audio": audio_summary,
            "transcript": _summarize_transcript(state.attributed_transcript or state.transcript),
            "decision_log": _summarize_decision_log(state.decision_log),
            "report_present": state.report is not None,
        }
        self._write_summary()
        self._append_event("run_completed")

    def finish_cancelled(self) -> None:
        self._summary["status"] = "cancelled"
        self._summary["finished_at"] = _timestamp()
        self._write_summary()
        self._append_event("run_cancelled")

    def finish_failure(self, error: BaseException) -> None:
        stage = self._current_stage
        self._summary["status"] = "failed"
        self._summary["finished_at"] = _timestamp()
        self._summary["error"] = {
            "type": error.__class__.__name__,
            "message": str(error),
            "stage": stage,
        }
        if stage is not None:
            stage_summary = self._summary["stages"].setdefault(stage, {})
            stage_summary["status"] = "failed"
            stage_summary["completed_at"] = _timestamp()
        self._write_summary()
        self._append_event(
            "run_failed",
            stage=stage,
            error_type=error.__class__.__name__,
            error_message=str(error),
        )

    def _append_event(self, event_type: str, **payload: Any) -> None:
        record = {"timestamp": _timestamp(), "event": event_type, **payload}
        with self.events_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        with suppress(OSError):
            self.events_path.chmod(0o600)

    def _write_summary(self) -> None:
        write_json_atomic(self.summary_path, self._summary)


def iter_run_summaries(project_root: Path | None = None) -> list[dict[str, Any]]:
    base_dir = default_run_directory(project_root)
    if not base_dir.exists():
        return []
    summaries: list[dict[str, Any]] = []
    for candidate in sorted(
        base_dir.iterdir(), key=lambda item: item.stat().st_mtime, reverse=True
    ):
        summary_path = candidate / "run.json"
        if not summary_path.exists():
            continue
        with suppress(Exception):
            summaries.append(read_json(summary_path))
    return summaries


def load_run_summary(trace_id: str, project_root: Path | None = None) -> dict[str, Any]:
    summary_path = default_run_directory(project_root) / trace_id / "run.json"
    return cast(dict[str, Any], read_json(summary_path))


def prune_run_summaries(*, older_than_days: float, project_root: Path | None = None) -> int:
    base_dir = default_run_directory(project_root)
    if not base_dir.exists():
        return 0
    threshold = datetime.now(UTC) - timedelta(days=older_than_days)
    removed = 0
    for candidate in base_dir.iterdir():
        if not candidate.is_dir():
            continue
        modified = datetime.fromtimestamp(candidate.stat().st_mtime, tz=UTC)
        if modified >= threshold:
            continue
        shutil.rmtree(candidate, ignore_errors=True)
        removed += 1
    return removed


__all__ = [
    "DEFAULT_RUN_DIRECTORY",
    "RunRecorder",
    "default_run_directory",
    "iter_run_summaries",
    "load_run_summary",
    "prune_run_summaries",
]
