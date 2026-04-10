"""Command-line interface for parler."""

from __future__ import annotations

import json
from datetime import date
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

import click

from .audio.ingester import AudioIngester, prune_managed_audio_files
from .config import CacheConfig, ParlerConfig, load_config
from .doctor import format_doctor_report, run_doctor
from .errors import ParlerError, ProcessingError, exit_code_for
from .extraction.extractor import DecisionExtractor
from .local import LOCAL_API_KEY_PLACEHOLDER, default_local_model_name
from .models import DecisionLog, Transcript
from .pipeline import PipelineOrchestrator, PipelineStage, ProcessingState
from .pipeline.orchestrator import estimate_cost
from .pipeline.state import load_processing_state, save_processing_state
from .rendering.renderer import OutputFormat, RenderConfig, ReportRenderer
from .runlog import RunRecorder, iter_run_summaries, load_run_summary, prune_run_summaries
from .transcription.cache import TranscriptCache
from .util.env import DEFAULT_ENV_FILE, apply_api_key_aliases, load_env_file
from .util.serialization import to_jsonable

_STAGE_DESCRIPTIONS = {
    PipelineStage.INGEST: "probe input and normalize audio",
    PipelineStage.TRANSCRIBE: "call Voxtral speech-to-text",
    PipelineStage.ATTRIBUTE: "resolve speaker labels",
    PipelineStage.EXTRACT: "call Mistral extraction",
    PipelineStage.RENDER: "render the decision report",
}
_LOCAL_MODEL_NAME = default_local_model_name()


def _package_version() -> str:
    try:
        return version("parler")
    except PackageNotFoundError:
        return "0.1.0+local"


def _parse_meeting_date(value: str | None) -> date | None:
    if not value:
        return None
    return date.fromisoformat(value)


def _split_participants(values: tuple[str, ...], combined: str | None) -> tuple[str, ...]:
    participants = [value for value in values if value]
    if combined:
        participants.extend(part.strip() for part in combined.split(",") if part.strip())
    return tuple(participants)


def _infer_report_format(
    output_format: str | None,
    output_path: Path | None,
    *,
    default: str,
) -> str:
    if output_format:
        return output_format
    if output_path is None:
        return default
    suffix = output_path.suffix.lower()
    if suffix == ".html":
        return "html"
    if suffix == ".json":
        return "json"
    return default


def _infer_report_format_from_path(output_path: Path | None) -> str | None:
    if output_path is None:
        return None
    suffix = output_path.suffix.lower()
    if suffix == ".html":
        return "html"
    if suffix == ".json":
        return "json"
    return "markdown"


def _default_report_path(input_path: Path, output_format: str) -> Path:
    suffix = {
        "markdown": ".md",
        "html": ".html",
        "json": ".json",
    }.get(output_format, ".txt")
    return Path.cwd() / f"{input_path.stem}-decisions{suffix}"


def _render_transcript_payload(transcript: Transcript, output_format: str) -> str:
    if output_format == "json":
        return json.dumps(to_jsonable(transcript), indent=2, ensure_ascii=False)
    return transcript.text


def _write_or_echo(payload: str, target_path: Path | None) -> None:
    if target_path is not None:
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_text(payload, encoding="utf-8")
        click.echo(str(target_path))
        return
    click.echo(payload)


def _render_decision_log_payload(
    decision_log: DecisionLog,
    *,
    output_format: str,
) -> str:
    if output_format == "json":
        return json.dumps(to_jsonable(decision_log), indent=2, ensure_ascii=False)
    renderer = ReportRenderer()
    return renderer.render(
        decision_log,
        RenderConfig(format=OutputFormat(output_format)),
    )


def _cache_entry_path(cache_dir: Path, key: str) -> Path:
    normalized = key if key.endswith(".json") else f"{key}.json"
    return cache_dir / normalized


def _resolve_cache_settings(config_path: Path | None) -> CacheConfig:
    if config_path is not None:
        return load_config(config_path=config_path).cache
    try:
        return load_config(config_path=None).cache
    except ParlerError:
        return CacheConfig()


def _echo_json(value: object) -> None:
    click.echo(json.dumps(to_jsonable(value), indent=2, ensure_ascii=False))


def _emit_verbose(message: str, *, enabled: bool) -> None:
    if enabled:
        click.echo(f"[verbose] {message}", err=True)


def _describe_checkpoint_target(checkpoint_path: Path | None, *, resume: bool) -> str:
    if checkpoint_path is not None:
        return str(checkpoint_path)
    if resume:
        return "auto (.parler-state.json)"
    return "-"


def _stage_start_message(
    stage: PipelineStage,
    *,
    input_path: Path,
    config: ParlerConfig,
    output_format: str,
) -> str:
    if stage == PipelineStage.INGEST:
        return f"{stage.name.lower()}: {input_path} ({_STAGE_DESCRIPTIONS[stage]})"
    if stage == PipelineStage.TRANSCRIBE:
        languages = ",".join(config.transcription.languages) or "auto"
        cache_state = "on" if config.cache.enabled else "off"
        return (
            f"{stage.name.lower()}: model={config.transcription.model} "
            f"languages={languages} cache={cache_state}"
        )
    if stage == PipelineStage.ATTRIBUTE:
        return (
            f"{stage.name.lower()}: participants={len(config.participants)} "
            f"anonymize={'yes' if config.output.anonymize_speakers else 'no'}"
        )
    if stage == PipelineStage.EXTRACT:
        meeting_date = config.meeting_date.isoformat() if config.meeting_date else "unspecified"
        cache_state = "on" if config.cache.enabled else "off"
        return (
            f"{stage.name.lower()}: model={config.extraction.model} "
            f"prompt={config.extraction.prompt_version} meeting_date={meeting_date} "
            f"cache={cache_state}"
        )
    return f"{stage.name.lower()}: format={output_format}"


def _describe_state(state: ProcessingState, *, transcribe_only: bool) -> tuple[str, ...]:
    details: list[str] = []
    if state.audio_file is not None:
        details.append(
            "audio="
            f"{state.audio_file.format} duration={state.audio_file.duration_s:.1f}s "
            f"hash={state.audio_file.content_hash}"
        )
    if state.transcript is not None:
        languages = (
            ",".join(state.transcript.detected_languages) or state.transcript.language or "-"
        )
        details.append(
            "transcript="
            f"segments={len(state.transcript.segments)} language={state.transcript.language or '-'} "
            f"detected={languages} model={state.transcript.model or '-'}"
        )
    if not transcribe_only and state.decision_log is not None:
        details.append(
            "decision_log="
            f"decisions={len(state.decision_log.decisions)} "
            f"commitments={len(state.decision_log.commitments)} "
            f"questions={len(state.decision_log.open_questions)} "
            f"rejected={len(state.decision_log.rejected)} "
            f"model={state.decision_log.metadata.model}"
        )
    if not transcribe_only and state.report is not None:
        details.append(f"report_bytes={len(state.report.encode('utf-8'))}")
    return tuple(details)


def _format_run_summary(summary: dict[str, object]) -> str:
    input_path = summary.get("input_path")
    source = Path(str(input_path)).name if input_path else "-"
    stages = summary.get("stages")
    stage_count = len(stages) if isinstance(stages, dict) else 0
    return "\t".join(
        (
            str(summary.get("trace_id", "-")),
            str(summary.get("command", "-")),
            str(summary.get("status", "-")),
            str(summary.get("started_at", "-")),
            source,
            str(stage_count),
        )
    )


def _build_overrides(
    *,
    languages: tuple[str, ...],
    output_format: str | None,
    output_path: Path | None,
    participants: tuple[str, ...],
    meeting_date_value: str | None,
    anonymize_speakers: bool,
    local: bool,
) -> dict[str, object]:
    overrides: dict[str, object] = {}
    if languages:
        overrides["transcription.languages"] = list(languages)
    if output_format is not None:
        overrides["output.format"] = output_format
    if output_path is not None:
        overrides["output.output_path"] = str(output_path)
    if participants:
        overrides["participants"] = list(participants)
    parsed_meeting_date = _parse_meeting_date(meeting_date_value)
    if parsed_meeting_date is not None:
        overrides["meeting_date"] = parsed_meeting_date.isoformat()
    if anonymize_speakers:
        overrides["output.anonymize_speakers"] = True
    if local:
        overrides["api_key"] = LOCAL_API_KEY_PLACEHOLDER
        overrides["transcription.model"] = _LOCAL_MODEL_NAME
        overrides["extraction.model"] = _LOCAL_MODEL_NAME
    return overrides


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(version=_package_version(), prog_name="parler")
def cli() -> None:
    """Multilingual meeting intelligence built on Voxtral."""


@cli.command()
@click.argument("input_path", type=click.Path(path_type=Path, dir_okay=False))
@click.option("--config", "config_path", type=click.Path(path_type=Path, dir_okay=False))
@click.option("--lang", "languages", multiple=True, help="Repeat for each expected language.")
@click.option(
    "--format",
    "output_format",
    type=click.Choice(["markdown", "html", "json"], case_sensitive=False),
)
@click.option("--output", "output_path", type=click.Path(path_type=Path, dir_okay=False))
@click.option("--participant", "participants", multiple=True, help="Known participant name.")
@click.option(
    "--participants",
    "participants_csv",
    help="Comma-separated participant names.",
)
@click.option(
    "--meeting-date", "meeting_date_value", help="Meeting date in ISO format (YYYY-MM-DD)."
)
@click.option("--checkpoint", "checkpoint_path", type=click.Path(path_type=Path, dir_okay=False))
@click.option("--resume", is_flag=True, help="Resume from an existing checkpoint if available.")
@click.option("--transcribe-only", is_flag=True, help="Stop after transcription.")
@click.option("--no-diarize", is_flag=True, help="Skip speaker attribution.")
@click.option("--anonymize-speakers", is_flag=True, help="Replace speaker names in outputs.")
@click.option("--cost-estimate", is_flag=True, help="Print estimated cost without API calls.")
@click.option("--yes", "assume_yes", is_flag=True, help="Auto-confirm cost prompts.")
@click.option(
    "--local", is_flag=True, help="Run transcription and extraction with a local Voxtral model."
)
@click.option("-v", "--verbose", is_flag=True, help="Log pipeline stages and runtime details.")
def process(
    input_path: Path,
    config_path: Path | None,
    languages: tuple[str, ...],
    output_format: str | None,
    output_path: Path | None,
    participants: tuple[str, ...],
    participants_csv: str | None,
    meeting_date_value: str | None,
    checkpoint_path: Path | None,
    resume: bool,
    transcribe_only: bool,
    no_diarize: bool,
    anonymize_speakers: bool,
    cost_estimate: bool,
    assume_yes: bool,
    local: bool,
    verbose: bool,
) -> None:
    """Process an audio file into a transcript or decision report."""

    project_root = Path.cwd().resolve()
    resolved_participants = _split_participants(participants, participants_csv)
    requested_format = (
        output_format.lower()
        if output_format is not None
        else _infer_report_format_from_path(output_path)
    )

    if cost_estimate:
        overrides = _build_overrides(
            languages=languages,
            output_format=requested_format,
            output_path=output_path,
            participants=resolved_participants,
            meeting_date_value=meeting_date_value,
            anonymize_speakers=anonymize_speakers,
            local=local,
        )
        config = load_config(config_path=config_path, overrides=overrides)
        if local:
            click.echo("Estimated total cost: $0.00 (local inference)")
            return
        audio_file = AudioIngester().ingest(input_path)
        click.echo(f"Estimated total cost: ${estimate_cost(audio_file, config):.2f}")
        return

    recorder = RunRecorder(
        command="process",
        project_root=project_root,
        input_path=input_path,
        config_path=config_path,
        output_path=output_path,
        checkpoint_path=checkpoint_path,
    )
    try:
        overrides = _build_overrides(
            languages=languages,
            output_format=requested_format,
            output_path=output_path,
            participants=resolved_participants,
            meeting_date_value=meeting_date_value,
            anonymize_speakers=anonymize_speakers,
            local=local,
        )
        config = load_config(config_path=config_path, overrides=overrides)
        orchestrator = PipelineOrchestrator(config)
        resolved_format = requested_format or config.output.format
        started_stages: list[PipelineStage] = []

        _emit_verbose(
            "command=process "
            f"input={input_path} format={resolved_format} "
            f"checkpoint={_describe_checkpoint_target(checkpoint_path, resume=resume)} "
            f"resume={'yes' if resume else 'no'} "
            f"execution={'local' if local else 'remote'}",
            enabled=verbose,
        )
        _emit_verbose(
            "models "
            f"transcription={config.transcription.model} "
            f"extraction={config.extraction.model}",
            enabled=verbose,
        )
        _emit_verbose(
            "context "
            f"languages={','.join(config.transcription.languages) or 'auto'} "
            f"participants={','.join(config.participants) or '-'} "
            f"meeting_date={config.meeting_date.isoformat() if config.meeting_date else '-'} "
            f"cache_dir={config.cache.directory}",
            enabled=verbose,
        )
        _emit_verbose(
            f"trace_id={recorder.trace_id} run_dir={recorder.run_dir}",
            enabled=verbose,
        )

        def confirm_cost(cost: float) -> bool:
            _emit_verbose(f"estimated_cost=${cost:.2f}", enabled=verbose)
            if assume_yes:
                return True
            return click.confirm(f"Estimated API cost is ${cost:.2f}. Continue?", default=False)

        def on_stage_start(stage: PipelineStage) -> None:
            recorder.stage_started(stage)
            started_stages.append(stage)
            _emit_verbose(
                _stage_start_message(
                    stage,
                    input_path=input_path,
                    config=config,
                    output_format=resolved_format,
                ),
                enabled=verbose,
            )

        def on_stage_complete(stage: PipelineStage, duration_s: float) -> None:
            recorder.stage_completed(stage, duration_s)
            _emit_verbose(
                f"{stage.name.lower()}: complete in {duration_s:.2f}s",
                enabled=verbose,
            )

        state = orchestrator.run(
            input_path,
            transcribe_only=transcribe_only,
            no_diarize=no_diarize,
            checkpoint_path=checkpoint_path,
            resume=resume,
            on_cost_confirm=confirm_cost,
            on_stage_start=on_stage_start,
            on_stage_complete=on_stage_complete,
        )
        if state is None:
            recorder.finish_cancelled()
            _emit_verbose("pipeline cancelled before the first billable stage", enabled=verbose)
            click.echo("Processing cancelled before the first billable stage.", err=True)
            return

        rendered_output: str | None
        if transcribe_only:
            rendered_output = (
                _render_transcript_payload(
                    state.transcript, "json" if resolved_format == "json" else "text"
                )
                if state.transcript is not None
                else None
            )
        else:
            rendered_output = state.report

        if rendered_output is None:
            recorder.finish_failure(ProcessingError("No output produced."))
            _emit_verbose("pipeline returned no output", enabled=verbose)
            click.echo("No output produced.", err=True)
            return

        target_path = output_path or config.output.output_path
        if target_path is None and output_format is None and not transcribe_only:
            target_path = _default_report_path(input_path, resolved_format)
        recorder.set_output_path(target_path)
        recorder.set_checkpoint_path(state.checkpoint_path)
        if resume:
            resumed_stages = [
                stage.name.lower()
                for stage in sorted(state.completed_stages, key=lambda item: item.value)
                if stage not in started_stages
            ]
            if resumed_stages:
                _emit_verbose(
                    f"reused completed stages from checkpoint: {', '.join(resumed_stages)}",
                    enabled=verbose,
                )
        if transcribe_only:
            _emit_verbose(
                "mode=transcribe-only; later pipeline stages were skipped", enabled=verbose
            )
        elif no_diarize:
            _emit_verbose("attribute stage skipped because --no-diarize was set", enabled=verbose)
        for detail in _describe_state(state, transcribe_only=transcribe_only):
            _emit_verbose(detail, enabled=verbose)
        _write_or_echo(rendered_output, target_path)
        recorder.finish_success(state)
        _emit_verbose(
            f"wrote_output={target_path if target_path is not None else 'stdout'}",
            enabled=verbose,
        )
    except Exception as exc:
        recorder.finish_failure(exc)
        _emit_verbose(f"pipeline failed: {type(exc).__name__}: {exc}", enabled=verbose)
        raise


@cli.command()
@click.argument("input_path", type=click.Path(path_type=Path, dir_okay=False))
@click.option("--config", "config_path", type=click.Path(path_type=Path, dir_okay=False))
@click.option("--lang", "languages", multiple=True, help="Repeat for each expected language.")
@click.option(
    "--format",
    "output_format",
    type=click.Choice(["text", "json"], case_sensitive=False),
)
@click.option("--output", "output_path", type=click.Path(path_type=Path, dir_okay=False))
@click.option("--checkpoint", "checkpoint_path", type=click.Path(path_type=Path, dir_okay=False))
@click.option("--resume", is_flag=True, help="Resume from an existing checkpoint if available.")
@click.option("--cost-estimate", is_flag=True, help="Print estimated cost without API calls.")
@click.option("--yes", "assume_yes", is_flag=True, help="Auto-confirm cost prompts.")
@click.option("--local", is_flag=True, help="Run transcription locally with a Voxtral model.")
@click.option("-v", "--verbose", is_flag=True, help="Log pipeline stages and runtime details.")
def transcribe(
    input_path: Path,
    config_path: Path | None,
    languages: tuple[str, ...],
    output_format: str | None,
    output_path: Path | None,
    checkpoint_path: Path | None,
    resume: bool,
    cost_estimate: bool,
    assume_yes: bool,
    local: bool,
    verbose: bool,
) -> None:
    """Run only the transcription stage."""

    if cost_estimate:
        overrides = _build_overrides(
            languages=languages,
            output_format=None,
            output_path=None,
            participants=(),
            meeting_date_value=None,
            anonymize_speakers=False,
            local=local,
        )
        config = load_config(config_path=config_path, overrides=overrides)
        if local:
            click.echo("Estimated total cost: $0.00 (local inference)")
            return
        audio_file = AudioIngester().ingest(input_path)
        click.echo(f"Estimated total cost: ${estimate_cost(audio_file, config):.2f}")
        return

    recorder = RunRecorder(
        command="transcribe",
        project_root=Path.cwd().resolve(),
        input_path=input_path,
        config_path=config_path,
        output_path=output_path,
        checkpoint_path=checkpoint_path,
    )
    try:
        overrides = _build_overrides(
            languages=languages,
            output_format=None,
            output_path=None,
            participants=(),
            meeting_date_value=None,
            anonymize_speakers=False,
            local=local,
        )
        config = load_config(config_path=config_path, overrides=overrides)
        orchestrator = PipelineOrchestrator(config)
        resolved_format = (
            output_format.lower()
            if output_format is not None
            else ("json" if output_path and output_path.suffix.lower() == ".json" else "text")
        )
        started_stages: list[PipelineStage] = []

        _emit_verbose(
            "command=transcribe "
            f"input={input_path} format={resolved_format} "
            f"checkpoint={_describe_checkpoint_target(checkpoint_path, resume=resume)} "
            f"resume={'yes' if resume else 'no'} "
            f"execution={'local' if local else 'remote'}",
            enabled=verbose,
        )
        _emit_verbose(
            "model "
            f"transcription={config.transcription.model} "
            f"languages={','.join(config.transcription.languages) or 'auto'} "
            f"cache_dir={config.cache.directory}",
            enabled=verbose,
        )
        _emit_verbose(
            f"trace_id={recorder.trace_id} run_dir={recorder.run_dir}",
            enabled=verbose,
        )

        def confirm_cost(cost: float) -> bool:
            _emit_verbose(f"estimated_cost=${cost:.2f}", enabled=verbose)
            if assume_yes:
                return True
            return click.confirm(f"Estimated API cost is ${cost:.2f}. Continue?", default=False)

        def on_stage_start(stage: PipelineStage) -> None:
            recorder.stage_started(stage)
            started_stages.append(stage)
            _emit_verbose(
                _stage_start_message(
                    stage,
                    input_path=input_path,
                    config=config,
                    output_format=resolved_format,
                ),
                enabled=verbose,
            )

        def on_stage_complete(stage: PipelineStage, duration_s: float) -> None:
            recorder.stage_completed(stage, duration_s)
            _emit_verbose(
                f"{stage.name.lower()}: complete in {duration_s:.2f}s",
                enabled=verbose,
            )

        state = orchestrator.run(
            input_path,
            transcribe_only=True,
            checkpoint_path=checkpoint_path,
            resume=resume,
            on_cost_confirm=confirm_cost,
            on_stage_start=on_stage_start,
            on_stage_complete=on_stage_complete,
        )
        if state is None:
            recorder.finish_cancelled()
            _emit_verbose(
                "transcription cancelled before the first billable stage", enabled=verbose
            )
            click.echo("Transcription cancelled before the first billable stage.", err=True)
            return
        if state.transcript is None:
            recorder.finish_failure(ProcessingError("No transcript produced."))
            _emit_verbose("transcription produced no transcript", enabled=verbose)
            click.echo("No transcript produced.", err=True)
            return

        payload = _render_transcript_payload(state.transcript, resolved_format)
        recorder.set_output_path(output_path)
        recorder.set_checkpoint_path(state.checkpoint_path)
        if resume:
            resumed_stages = [
                stage.name.lower()
                for stage in sorted(state.completed_stages, key=lambda item: item.value)
                if stage not in started_stages
            ]
            if resumed_stages:
                _emit_verbose(
                    f"reused completed stages from checkpoint: {', '.join(resumed_stages)}",
                    enabled=verbose,
                )
        for detail in _describe_state(state, transcribe_only=True):
            _emit_verbose(detail, enabled=verbose)
        _write_or_echo(payload, output_path)
        recorder.finish_success(state)
        _emit_verbose(
            f"wrote_output={output_path if output_path is not None else 'stdout'}",
            enabled=verbose,
        )
    except Exception as exc:
        recorder.finish_failure(exc)
        _emit_verbose(f"transcription failed: {type(exc).__name__}: {exc}", enabled=verbose)
        raise


@cli.command()
@click.option(
    "--from-state",
    "state_path",
    type=click.Path(path_type=Path, dir_okay=False, exists=True),
    required=True,
)
@click.option("--config", "config_path", type=click.Path(path_type=Path, dir_okay=False))
@click.option(
    "--format",
    "output_format",
    type=click.Choice(["markdown", "html", "json"], case_sensitive=False),
)
@click.option("--output", "output_path", type=click.Path(path_type=Path, dir_okay=False))
@click.option("--participant", "participants", multiple=True, help="Known participant name.")
@click.option("--participants", "participants_csv", help="Comma-separated participant names.")
@click.option(
    "--meeting-date", "meeting_date_value", help="Meeting date in ISO format (YYYY-MM-DD)."
)
@click.option("--local", is_flag=True, help="Run extraction locally with a Voxtral model.")
def extract(
    state_path: Path,
    config_path: Path | None,
    output_format: str | None,
    output_path: Path | None,
    participants: tuple[str, ...],
    participants_csv: str | None,
    meeting_date_value: str | None,
    local: bool,
) -> None:
    """Extract or reuse a decision log from a saved checkpoint."""

    state = load_processing_state(state_path)

    if state.decision_log is None:
        resolved_participants = _split_participants(participants, participants_csv)
        overrides = _build_overrides(
            languages=(),
            output_format=None,
            output_path=None,
            participants=resolved_participants,
            meeting_date_value=meeting_date_value,
            anonymize_speakers=False,
            local=local,
        )
        config = load_config(config_path=config_path, overrides=overrides)
        transcript = state.attributed_transcript or state.transcript
        if transcript is None:
            raise ProcessingError("State file does not contain a transcript")
        extractor = DecisionExtractor(
            api_key=config.api_key,
            model=config.extraction.model,
            prompt_version=config.extraction.prompt_version,
            temperature=config.extraction.temperature,
            max_tokens=config.extraction.max_tokens,
            multi_pass_threshold=config.extraction.multi_pass_threshold,
        )
        state = state.with_decision_log(
            extractor.extract(
                transcript,
                meeting_date=config.meeting_date,
                participants=config.participants,
            )
        )
        save_processing_state(state_path, state)

    assert state.decision_log is not None
    resolved_format = _infer_report_format(
        output_format.lower() if output_format is not None else None,
        output_path,
        default="json",
    )
    payload = _render_decision_log_payload(
        state.decision_log,
        output_format=resolved_format,
    )
    _write_or_echo(payload, output_path)


@cli.command()
@click.option(
    "--from-state",
    "state_path",
    type=click.Path(path_type=Path, dir_okay=False, exists=True),
    required=True,
)
@click.option(
    "--format",
    "output_format",
    type=click.Choice(["markdown", "html", "json"], case_sensitive=False),
)
@click.option("--output", "output_path", type=click.Path(path_type=Path, dir_okay=False))
def report(
    state_path: Path,
    output_format: str | None,
    output_path: Path | None,
) -> None:
    """Render a report from a saved checkpoint without re-running APIs."""

    state = load_processing_state(state_path)
    if state.decision_log is None:
        raise ProcessingError("State file does not contain a decision log")

    resolved_format = _infer_report_format(
        output_format.lower() if output_format is not None else None,
        output_path,
        default="markdown",
    )
    report_text = _render_decision_log_payload(
        state.decision_log,
        output_format=resolved_format,
    )
    state = state.with_report(report_text)
    save_processing_state(state_path, state)
    _write_or_echo(report_text, output_path)


@cli.group()
def config() -> None:
    """Inspect and validate parler configuration."""


@config.command("validate")
@click.option("--config", "config_path", type=click.Path(path_type=Path, dir_okay=False))
def validate_config(config_path: Path | None) -> None:
    """Load config and exit non-zero if invalid."""

    load_config(config_path=config_path)
    click.echo("Configuration is valid.")


@cli.group()
def cache() -> None:
    """Inspect and manage local cache entries."""


@cache.command("list")
@click.option("--config", "config_path", type=click.Path(path_type=Path, dir_okay=False))
def list_cache(config_path: Path | None) -> None:
    """List cache entries with size and modified time."""

    cache_settings = _resolve_cache_settings(config_path)
    cache_dir = cache_settings.directory
    cache_dir.mkdir(parents=True, exist_ok=True)
    entries = sorted(cache_dir.glob("*.json"))
    if not entries:
        click.echo("No cache entries found.")
        return
    for entry in entries:
        stat = entry.stat()
        click.echo(f"{entry.stem}\t{stat.st_size}\t{date.fromtimestamp(stat.st_mtime).isoformat()}")


@cache.command("show")
@click.argument("key")
@click.option("--config", "config_path", type=click.Path(path_type=Path, dir_okay=False))
def show_cache(key: str, config_path: Path | None) -> None:
    """Show one cache entry by key."""

    cache_settings = _resolve_cache_settings(config_path)
    path = _cache_entry_path(cache_settings.directory, key)
    if not path.exists():
        raise ProcessingError(f"Cache entry not found: {key}")
    click.echo(path.read_text(encoding="utf-8"))


@cache.command("clear")
@click.option("--config", "config_path", type=click.Path(path_type=Path, dir_okay=False))
@click.option("--yes", "assume_yes", is_flag=True, help="Skip confirmation.")
def clear_cache(config_path: Path | None, assume_yes: bool) -> None:
    """Remove all cache entries."""

    cache_settings = _resolve_cache_settings(config_path)
    cache = TranscriptCache(
        cache_dir=cache_settings.directory,
        ttl_days=cache_settings.ttl_days,
    )
    if not assume_yes and not click.confirm("Clear all cache entries?", default=False):
        click.echo("Cache clear cancelled.", err=True)
        return
    cache.clear()
    click.echo("Cache cleared.")


@cli.command()
@click.option("--config", "config_path", type=click.Path(path_type=Path, dir_okay=False))
@click.option(
    "--project-root",
    type=click.Path(path_type=Path, file_okay=False, dir_okay=True),
    default=None,
)
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON.")
def doctor(config_path: Path | None, project_root: Path | None, as_json: bool) -> None:
    """Check local readiness for operator-driven parler runs."""

    report = run_doctor((project_root or Path.cwd()).resolve(), config_path=config_path)
    if as_json:
        _echo_json(
            {
                "ready": report.ready,
                "project_root": report.project_root,
                "env_file": report.env_file,
                "config_path": report.config_path,
                "cache_directory": report.cache_directory,
                "run_directory": report.run_directory,
                "temp_audio_directory": report.temp_audio_directory,
                "checks": report.checks,
            }
        )
    else:
        click.echo(format_doctor_report(report))
    if not report.ready:
        raise SystemExit(1)


@cli.group()
def runs() -> None:
    """Inspect local `.parler-runs` artifacts."""


@runs.command("list")
@click.option(
    "--project-root",
    type=click.Path(path_type=Path, file_okay=False, dir_okay=True),
    default=None,
)
@click.option("--limit", type=click.IntRange(min=1), default=20, show_default=True)
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON.")
def list_runs(project_root: Path | None, limit: int, as_json: bool) -> None:
    """List recent run artifact bundles."""

    summaries = iter_run_summaries((project_root or Path.cwd()).resolve())[:limit]
    if as_json:
        _echo_json(summaries)
        return
    if not summaries:
        click.echo("No recorded runs found.")
        return
    click.echo("trace_id\tcommand\tstatus\tstarted_at\tinput\tstages")
    for summary in summaries:
        click.echo(_format_run_summary(summary))


@runs.command("show")
@click.argument("trace_id")
@click.option(
    "--project-root",
    type=click.Path(path_type=Path, file_okay=False, dir_okay=True),
    default=None,
)
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON.")
def show_runs(trace_id: str, project_root: Path | None, as_json: bool) -> None:
    """Show one recorded run summary."""

    try:
        summary = load_run_summary(trace_id, (project_root or Path.cwd()).resolve())
    except FileNotFoundError as exc:
        raise ProcessingError(f"Run not found: {trace_id}") from exc
    if as_json:
        _echo_json(summary)
        return
    click.echo(f"Trace ID: {summary.get('trace_id', trace_id)}")
    click.echo(f"Command: {summary.get('command', '-')}")
    click.echo(f"Status: {summary.get('status', '-')}")
    click.echo(f"Started: {summary.get('started_at', '-')}")
    click.echo(f"Finished: {summary.get('finished_at', '-')}")
    click.echo(f"Input: {summary.get('input_path', '-')}")
    click.echo(f"Output: {summary.get('output_path', '-')}")
    click.echo(f"Checkpoint: {summary.get('checkpoint_path', '-')}")
    click.echo(f"Events: {summary.get('events_path', '-')}")
    stages = summary.get("stages", {})
    if isinstance(stages, dict) and stages:
        click.echo("Stages:")
        for stage_name, stage_data in stages.items():
            if isinstance(stage_data, dict):
                click.echo(
                    f"  {stage_name}: {stage_data.get('status', '-')} "
                    f"({stage_data.get('duration_s', '-')})"
                )


@cli.command()
@click.option("--runs/--no-runs", default=True, help="Prune stale `.parler-runs` bundles.")
@click.option(
    "--temp-audio/--no-temp-audio",
    default=True,
    help="Prune stale normalized temp audio files.",
)
@click.option(
    "--older-than-days",
    type=click.FloatRange(min=0.0),
    default=7.0,
    show_default=True,
    help="Delete artifacts older than this many days.",
)
@click.option(
    "--project-root",
    type=click.Path(path_type=Path, file_okay=False, dir_okay=True),
    default=None,
)
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON.")
def cleanup(
    runs: bool,
    temp_audio: bool,
    older_than_days: float,
    project_root: Path | None,
    as_json: bool,
) -> None:
    """Prune stale local run artifacts and normalized temp audio."""

    resolved_project_root = (project_root or Path.cwd()).resolve()
    removed_runs = (
        prune_run_summaries(older_than_days=older_than_days, project_root=resolved_project_root)
        if runs
        else 0
    )
    removed_temp_audio = (
        prune_managed_audio_files(older_than_days=older_than_days) if temp_audio else 0
    )
    payload = {
        "older_than_days": older_than_days,
        "removed_runs": removed_runs,
        "removed_temp_audio": removed_temp_audio,
    }
    if as_json:
        _echo_json(payload)
        return
    click.echo(f"Removed {removed_runs} run bundle(s) and {removed_temp_audio} temp audio file(s).")


@cli.command()
@click.option(
    "--project-root",
    type=click.Path(path_type=Path, file_okay=False, dir_okay=True),
    help="Open the TUI against a specific project root.",
)
def tui(project_root: Path | None) -> None:
    """Launch the Textual cockpit for parler."""

    from .tui import main as tui_main

    tui_main(project_root=project_root.resolve() if project_root is not None else None)


def main() -> None:
    try:
        load_env_file(Path.cwd() / DEFAULT_ENV_FILE)
        apply_api_key_aliases()
        cli.main(prog_name="parler", standalone_mode=False)
    except click.ClickException as exc:
        exc.show()
        raise SystemExit(exc.exit_code) from exc
    except ParlerError as exc:
        click.echo(f"Error: {exc}", err=True)
        raise SystemExit(exit_code_for(exc)) from exc
    except NotImplementedError as exc:
        click.echo(f"Not implemented yet: {exc}", err=True)
        raise SystemExit(1) from exc


__all__ = ["cli", "main"]
