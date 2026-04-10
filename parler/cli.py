"""Command-line interface for parler."""

from __future__ import annotations

import json
from datetime import date
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

import click

from .audio.ingester import AudioIngester
from .config import CacheConfig, load_config
from .errors import ParlerError, ProcessingError, exit_code_for
from .extraction.extractor import DecisionExtractor
from .models import DecisionLog, Transcript
from .pipeline import PipelineOrchestrator
from .pipeline.orchestrator import estimate_cost
from .pipeline.state import load_processing_state, save_processing_state
from .rendering.renderer import OutputFormat, RenderConfig, ReportRenderer
from .transcription.cache import TranscriptCache
from .util.serialization import to_jsonable


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


def _build_overrides(
    *,
    languages: tuple[str, ...],
    output_format: str | None,
    output_path: Path | None,
    participants: tuple[str, ...],
    meeting_date_value: str | None,
    anonymize_speakers: bool,
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
) -> None:
    """Process an audio file into a transcript or decision report."""

    resolved_participants = _split_participants(participants, participants_csv)
    requested_format = (
        output_format.lower()
        if output_format is not None
        else _infer_report_format_from_path(output_path)
    )
    overrides = _build_overrides(
        languages=languages,
        output_format=requested_format,
        output_path=output_path,
        participants=resolved_participants,
        meeting_date_value=meeting_date_value,
        anonymize_speakers=anonymize_speakers,
    )
    config = load_config(config_path=config_path, overrides=overrides)

    if cost_estimate:
        audio_file = AudioIngester().ingest(input_path)
        click.echo(f"Estimated total cost: ${estimate_cost(audio_file, config):.2f}")
        return

    orchestrator = PipelineOrchestrator(config)
    resolved_format = requested_format or config.output.format

    def confirm_cost(cost: float) -> bool:
        if assume_yes:
            return True
        return click.confirm(f"Estimated API cost is ${cost:.2f}. Continue?", default=False)

    state = orchestrator.run(
        input_path,
        transcribe_only=transcribe_only,
        no_diarize=no_diarize,
        checkpoint_path=checkpoint_path,
        resume=resume,
        on_cost_confirm=confirm_cost,
    )
    if state is None:
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
        click.echo("No output produced.", err=True)
        return

    target_path = output_path or config.output.output_path
    if target_path is None and output_format is None and not transcribe_only:
        target_path = _default_report_path(input_path, resolved_format)
    _write_or_echo(rendered_output, target_path)


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
) -> None:
    """Run only the transcription stage."""

    overrides = _build_overrides(
        languages=languages,
        output_format=None,
        output_path=None,
        participants=(),
        meeting_date_value=None,
        anonymize_speakers=False,
    )
    config = load_config(config_path=config_path, overrides=overrides)

    if cost_estimate:
        audio_file = AudioIngester().ingest(input_path)
        click.echo(f"Estimated total cost: ${estimate_cost(audio_file, config):.2f}")
        return

    orchestrator = PipelineOrchestrator(config)

    def confirm_cost(cost: float) -> bool:
        if assume_yes:
            return True
        return click.confirm(f"Estimated API cost is ${cost:.2f}. Continue?", default=False)

    state = orchestrator.run(
        input_path,
        transcribe_only=True,
        checkpoint_path=checkpoint_path,
        resume=resume,
        on_cost_confirm=confirm_cost,
    )
    if state is None or state.transcript is None:
        click.echo("No transcript produced.", err=True)
        return

    resolved_format = (
        output_format.lower()
        if output_format is not None
        else ("json" if output_path and output_path.suffix.lower() == ".json" else "text")
    )
    payload = _render_transcript_payload(state.transcript, resolved_format)
    _write_or_echo(payload, output_path)


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
def extract(
    state_path: Path,
    config_path: Path | None,
    output_format: str | None,
    output_path: Path | None,
    participants: tuple[str, ...],
    participants_csv: str | None,
    meeting_date_value: str | None,
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
