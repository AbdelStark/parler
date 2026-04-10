"""Textual application for the parler workflow."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import ClassVar

from textual import on
from textual.app import App, ComposeResult, SystemCommand
from textual.binding import Binding
from textual.containers import Grid, Horizontal, Vertical, VerticalScroll
from textual.events import Resize
from textual.screen import Screen
from textual.widgets import (
    Button,
    DataTable,
    DirectoryTree,
    Footer,
    Input,
    Label,
    Markdown,
    ProgressBar,
    RichLog,
    Select,
    Static,
    Switch,
    TabbedContent,
    TabPane,
)
from textual.worker import Worker, WorkerState

from ..audio.ingester import AudioIngester
from ..config import CacheConfig, ParlerConfig, load_config
from ..errors import InputError, ParlerError
from ..models import Commitment, Decision, DecisionLog, OpenQuestion, Rejection, Transcript
from ..pipeline import PipelineOrchestrator
from ..pipeline.state import (
    PipelineStage,
    ProcessingState,
    checkpoint_payload,
    load_processing_state,
)
from ..rendering.renderer import OutputFormat, RenderConfig, ReportRenderer
from ..util.env import DEFAULT_ENV_FILE, apply_api_key_aliases, load_env_file
from ..util.serialization import to_jsonable

FORMAT_OPTIONS: tuple[tuple[str, str], ...] = (
    ("Markdown", "markdown"),
    ("HTML", "html"),
    ("JSON", "json"),
)
FORMAT_VALUES = {value for _, value in FORMAT_OPTIONS}

MODEL_PRESETS: tuple[tuple[str, str], ...] = (
    ("voxtral-mini-latest", "voxtral-mini-latest"),
    ("voxtral-small-latest", "voxtral-small-latest"),
    ("mistral-medium-latest", "mistral-medium-latest"),
    ("mistral-large-latest", "mistral-large-latest"),
)
TRANSCRIPTION_MODEL_VALUES = {value for _, value in MODEL_PRESETS[:2]}
EXTRACTION_MODEL_VALUES = {value for _, value in MODEL_PRESETS[2:]}

STAGE_LABELS: dict[PipelineStage, str] = {
    PipelineStage.INGEST: "Ingest",
    PipelineStage.TRANSCRIBE: "Transcribe",
    PipelineStage.ATTRIBUTE: "Attribute",
    PipelineStage.EXTRACT: "Extract",
    PipelineStage.RENDER: "Render",
}

AUDIO_EXTENSIONS = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg"}
TEXT_PREVIEW_EXTENSIONS = {
    ".json",
    ".md",
    ".py",
    ".txt",
    ".toml",
    ".yaml",
    ".yml",
    ".css",
    ".tcss",
}
PREVIEW_LIMIT = 4_000
FORM_STACK_WIDTH = 150
STUDIO_STACK_WIDTH = 175
SHELL_STACK_WIDTH = 126


@dataclass(frozen=True)
class FixturePreset:
    name: str
    path: Path
    languages: tuple[str, ...]
    participants: tuple[str, ...]
    meeting_date: date
    output_format: str = "markdown"


FIXTURE_PRESETS: dict[str, FixturePreset] = {
    "fr": FixturePreset(
        name="French launch demo",
        path=Path("tests/fixtures/audio/fr_meeting_5min.mp3"),
        languages=("fr",),
        participants=("Pierre", "Sophie"),
        meeting_date=date(2026, 4, 9),
    ),
    "bilingual": FixturePreset(
        name="Bilingual startup demo",
        path=Path("tests/fixtures/audio/bilingual_meeting_5min.mp3"),
        languages=("fr", "en"),
        participants=("Pierre", "Alice"),
        meeting_date=date(2026, 4, 9),
    ),
    "earnings": FixturePreset(
        name="Earnings-call stress demo",
        path=Path("tests/fixtures/audio/earnings_call_45min.mp3"),
        languages=("en", "fr"),
        participants=("Pierre", "Sophie", "Analyst"),
        meeting_date=date(2026, 4, 9),
    ),
}


def _split_csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _optional_path(project_root: Path, raw: str) -> Path | None:
    normalized = raw.strip()
    if not normalized:
        return None
    path = Path(normalized)
    if not path.is_absolute():
        path = project_root / path
    return path


def _optional_date(raw: str) -> date | None:
    normalized = raw.strip()
    if not normalized:
        return None
    return date.fromisoformat(normalized)


def _display_path(path: Path, project_root: Path) -> str:
    try:
        return str(path.relative_to(project_root))
    except ValueError:
        return str(path)


def _safe_defaults() -> ParlerConfig:
    api_key = os.environ.get("MISTRAL_API_KEY") or os.environ.get("PARLER_API_KEY") or ""
    return ParlerConfig(api_key=api_key, cache=CacheConfig())


def _preview_text(path: Path) -> str:
    if path.suffix.lower() in AUDIO_EXTENSIONS:
        try:
            audio = AudioIngester().ingest(path)
        except Exception as exc:  # pragma: no cover - host dependent
            stat = path.stat()
            return f"Audio asset\nPath: {path}\nSize: {stat.st_size:,} bytes\nPreview error: {exc}"
        return (
            "Audio asset\n"
            f"Path: {audio.path}\n"
            f"Format: {audio.format}\n"
            f"Duration: {audio.duration_s:.1f}s\n"
            f"Sample rate: {audio.sample_rate} Hz\n"
            f"Channels: {audio.channels}\n"
            f"Hash: {audio.content_hash}"
        )

    if path.suffix.lower() not in TEXT_PREVIEW_EXTENSIONS:
        stat = path.stat()
        return f"Binary or unsupported preview\nPath: {path}\nSize: {stat.st_size:,} bytes"

    content = path.read_text(encoding="utf-8", errors="replace")
    if len(content) <= PREVIEW_LIMIT:
        return content
    return content[:PREVIEW_LIMIT] + "\n\n… preview truncated …"


@dataclass(frozen=True)
class PipelineRequest:
    input_path: Path
    config_path: Path | None
    output_path: Path | None
    checkpoint_path: Path | None
    meeting_date: date | None
    languages: tuple[str, ...]
    participants: tuple[str, ...]
    output_format: str
    cache_dir: Path | None
    transcription_model: str | None
    extraction_model: str | None
    transcribe_only: bool
    no_diarize: bool
    anonymize_speakers: bool
    resume: bool

    def expected_stages(self) -> tuple[PipelineStage, ...]:
        stages = [PipelineStage.INGEST, PipelineStage.TRANSCRIBE]
        if self.transcribe_only:
            return tuple(stages)
        if not self.no_diarize:
            stages.append(PipelineStage.ATTRIBUTE)
        stages.extend((PipelineStage.EXTRACT, PipelineStage.RENDER))
        return tuple(stages)


def build_tui_config(request: PipelineRequest) -> ParlerConfig:
    overrides: dict[str, object] = {
        "transcription.languages": list(request.languages),
        "output.format": request.output_format,
        "participants": list(request.participants),
        "output.anonymize_speakers": request.anonymize_speakers,
    }
    if request.output_path is not None:
        overrides["output.output_path"] = request.output_path
    if request.meeting_date is not None:
        overrides["meeting_date"] = request.meeting_date.isoformat()
    if request.cache_dir is not None:
        overrides["cache.directory"] = request.cache_dir
    if request.transcription_model:
        overrides["transcription.model"] = request.transcription_model
    if request.extraction_model:
        overrides["extraction.model"] = request.extraction_model
    return load_config(config_path=request.config_path, overrides=overrides)


class ParlerTUIApp(App[None]):
    """A polished Textual cockpit for running parler end-to-end."""

    CSS_PATH = "app.tcss"
    TITLE = "parler 🇫🇷"
    SUB_TITLE = "Decision intelligence cockpit"
    ENABLE_COMMAND_PALETTE = True
    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("ctrl+r", "run_pipeline", "Run"),
        Binding("ctrl+o", "load_state", "Load state"),
        Binding("ctrl+g", "focus_tree", "Files"),
        Binding("f5", "refresh_cache", "Refresh cache"),
        Binding("ctrl+1", "show_studio", "Studio"),
        Binding("ctrl+2", "show_results", "Results"),
        Binding("ctrl+3", "show_artifacts", "Artifacts"),
        Binding("ctrl+4", "show_about", "About"),
        Binding("ctrl+f", "load_french_demo", "FR demo"),
        Binding("ctrl+b", "load_bilingual_demo", "Bilingual"),
        Binding("ctrl+e", "load_earnings_demo", "Earnings"),
    ]

    def __init__(self, project_root: Path | None = None) -> None:
        super().__init__()
        self.project_root = (project_root or Path.cwd()).resolve()
        self._pipeline_worker: Worker[ProcessingState | None] | None = None
        self.current_request: PipelineRequest | None = None
        self.current_state: ProcessingState | None = None
        self._last_output_payload = ""

    def compose(self) -> ComposeResult:
        with Horizontal(id="topbar"):
            yield Static("parler 🇫🇷 FR", id="topbar-brand")
            yield Static("Decision intelligence cockpit", id="topbar-title")
            with Horizontal(id="topbar-pills"):
                yield Static("Textual studio", classes="topbar-pill accent")
                yield Static("", id="topbar-api", classes="topbar-pill")
                yield Static("", id="topbar-mode", classes="topbar-pill")
                yield Static("Ctrl+P palette", id="topbar-hint", classes="topbar-pill quiet")
        with Horizontal(id="shell"):
            with VerticalScroll(id="sidebar"):
                yield Static(
                    "parler 🇫🇷 FR\nFrench-built meeting intelligence\n\n"
                    "Turn audio into explicit decisions, commitments, and polished reports.",
                    id="brand-card",
                )
                with Vertical(id="action-cluster"):
                    yield Label("Operate", classes="cluster-title")
                    yield Button("Run pipeline", id="run-button", variant="primary")
                    yield Button("Load checkpoint", id="load-state-button", variant="default")
                    yield Button("Refresh cache", id="refresh-cache-button", variant="success")
                    yield Button("Clear form", id="clear-form-button", variant="warning")
                with Vertical(id="fixture-cluster"):
                    yield Label("Showcase", classes="cluster-title")
                    yield Button("French launch", id="fixture-fr-button", classes="fixture-button")
                    yield Button(
                        "Bilingual startup",
                        id="fixture-bilingual-button",
                        classes="fixture-button",
                    )
                    yield Button(
                        "Earnings stress",
                        id="fixture-earnings-button",
                        classes="fixture-button",
                    )
                with Vertical(id="telemetry-cluster"):
                    yield Label("Telemetry", classes="cluster-title")
                    yield Static("", id="metric-mode", classes="metric-card")
                    yield Static("", id="metric-api", classes="metric-card")
                    yield Static("", id="metric-audio", classes="metric-card")
                    yield Static("", id="metric-decisions", classes="metric-card")
                    yield Static("", id="metric-commitments", classes="metric-card")
                    yield Static("", id="metric-languages", classes="metric-card")
                    yield Static("", id="metric-cache", classes="metric-card")
            with TabbedContent(initial="studio-tab", id="main-tabs"):
                with TabPane("Studio", id="studio-tab"), Horizontal(id="studio-layout"):
                    with VerticalScroll(id="form-scroll"):
                        yield Static("Run configuration", classes="section-title")
                        with Horizontal(classes="field-row"):
                            with Vertical(classes="field"):
                                yield Label("Audio input", classes="field-label")
                                yield Input(
                                    placeholder="meeting.mp3",
                                    id="input-path",
                                )
                            with Vertical(classes="field"):
                                yield Label("Config file", classes="field-label")
                                yield Input(
                                    placeholder="parler.toml",
                                    id="config-path",
                                )
                        with Horizontal(classes="field-row"):
                            with Vertical(classes="field"):
                                yield Label("Output path", classes="field-label")
                                yield Input(
                                    placeholder="meeting-decisions.md",
                                    id="output-path",
                                )
                            with Vertical(classes="field"):
                                yield Label("Checkpoint path", classes="field-label")
                                yield Input(
                                    value=".parler-state.json",
                                    placeholder=".parler-state.json",
                                    id="checkpoint-path",
                                )
                        with Horizontal(classes="field-row"):
                            with Vertical(classes="field"):
                                yield Label("Meeting date", classes="field-label")
                                yield Input(
                                    placeholder="2026-04-09",
                                    id="meeting-date",
                                )
                            with Vertical(classes="field"):
                                yield Label("Languages", classes="field-label")
                                yield Input(
                                    placeholder="fr,en",
                                    id="languages-input",
                                )
                        with Horizontal(classes="field-row"):
                            with Vertical(classes="field"):
                                yield Label("Participants", classes="field-label")
                                yield Input(
                                    placeholder="Pierre, Sophie, Alice",
                                    id="participants-input",
                                )
                            with Vertical(classes="field"):
                                yield Label("Output format", classes="field-label")
                                yield Select(
                                    FORMAT_OPTIONS,
                                    value="markdown",
                                    allow_blank=False,
                                    id="output-format-select",
                                )
                        with Horizontal(classes="field-row"):
                            with Vertical(classes="field"):
                                yield Label("Cache directory", classes="field-label")
                                yield Input(
                                    placeholder=".parler-cache",
                                    id="cache-dir-input",
                                )
                            with Vertical(classes="field"):
                                yield Label("Transcription model", classes="field-label")
                                yield Select(
                                    MODEL_PRESETS[:2],
                                    value="voxtral-mini-latest",
                                    allow_blank=False,
                                    id="transcription-model-select",
                                )
                        with Horizontal(classes="field-row"):
                            with Vertical(classes="field"):
                                yield Label("Extraction model", classes="field-label")
                                yield Select(
                                    MODEL_PRESETS[2:],
                                    value="mistral-medium-latest",
                                    allow_blank=False,
                                    id="extraction-model-select",
                                )
                            with Vertical(classes="field is-help"):
                                yield Label("Pipeline notes", classes="field-label")
                                yield Static(
                                    "Use a config file for advanced knobs like retries, chunking, "
                                    "cost budgets, and export behavior. API keys load automatically "
                                    "from .env via MISTRAL_API_KEY or PARLER_API_KEY; the visible "
                                    "fields here override only the explicit run inputs.",
                                    id="form-help",
                                )
                        yield Static("Run mode", classes="section-title compact")
                        with Horizontal(id="switch-row"):
                            with Vertical(classes="switch-field"):
                                yield Label("Transcribe only", classes="switch-label")
                                yield Switch(id="transcribe-only-switch")
                            with Vertical(classes="switch-field"):
                                yield Label("Skip attribution", classes="switch-label")
                                yield Switch(id="no-diarize-switch")
                            with Vertical(classes="switch-field"):
                                yield Label("Anonymize speakers", classes="switch-label")
                                yield Switch(id="anonymize-switch")
                            with Vertical(classes="switch-field"):
                                yield Label("Resume checkpoint", classes="switch-label")
                                yield Switch(id="resume-switch")
                    with Vertical(id="runtime-column"):
                        yield Static(
                            "Ready for a run.\nLoad a fixture or point parler at your own recording.",
                            id="run-summary",
                            classes="hero-card",
                        )
                        yield ProgressBar(
                            total=len(PipelineStage),
                            show_eta=False,
                            id="stage-progress",
                        )
                        with Grid(id="stage-grid"):
                            for stage in PipelineStage:
                                yield Static(
                                    "",
                                    id=f"stage-{stage.name.lower()}",
                                    classes="stage-card",
                                )
                        yield RichLog(
                            id="run-log",
                            wrap=True,
                            markup=False,
                            highlight=False,
                            auto_scroll=True,
                        )
                with TabPane("Results", id="results-tab"):
                    yield Static(
                        "No pipeline output yet.\nRun parler from the Studio tab or load a checkpoint.",
                        id="results-hero",
                        classes="hero-card compact",
                    )
                    with TabbedContent(initial="report-pane", id="results-tabs"):
                        with TabPane("Report", id="report-pane"):
                            yield Markdown(
                                "No report yet.",
                                id="report-markdown",
                            )
                        with (
                            TabPane("Transcript", id="transcript-pane"),
                            VerticalScroll(classes="text-scroll"),
                        ):
                            yield Static("No transcript yet.", id="transcript-view")
                        with TabPane("Decisions", id="decisions-pane"):
                            yield DataTable(
                                id="decision-table",
                                show_row_labels=False,
                                zebra_stripes=True,
                                cursor_type="row",
                            )
                        with TabPane("Commitments", id="commitments-pane"):
                            yield DataTable(
                                id="commitment-table",
                                show_row_labels=False,
                                zebra_stripes=True,
                                cursor_type="row",
                            )
                        with TabPane("Questions", id="questions-pane"):
                            yield DataTable(
                                id="question-table",
                                show_row_labels=False,
                                zebra_stripes=True,
                                cursor_type="row",
                            )
                        with TabPane("Rejected", id="rejected-pane"):
                            yield DataTable(
                                id="rejection-table",
                                show_row_labels=False,
                                zebra_stripes=True,
                                cursor_type="row",
                            )
                        with (
                            TabPane("Output", id="output-pane"),
                            VerticalScroll(classes="text-scroll"),
                        ):
                            yield Static("No serialized output yet.", id="raw-output-view")
                with TabPane("Artifacts", id="artifacts-tab"), Horizontal(id="artifacts-layout"):
                    yield DirectoryTree(self.project_root, id="file-tree")
                    with Vertical(id="artifacts-column"):
                        yield Static(
                            "Select project files or cache entries to preview them.",
                            classes="section-title compact",
                        )
                        with VerticalScroll(id="preview-scroll"):
                            yield Static("No file selected.", id="artifact-preview")
                        yield DataTable(
                            id="cache-table",
                            show_row_labels=False,
                            zebra_stripes=True,
                            cursor_type="row",
                        )
                with TabPane("About", id="about-tab"):
                    yield Markdown(self._about_markdown(), id="about-markdown")
        yield Footer()

    def on_mount(self) -> None:
        self._configure_tables()
        self._seed_defaults()
        self._reset_runtime()
        self._refresh_metrics()
        self.refresh_cache_table()
        self._apply_responsive_layout(self.size.width)
        self.query_one("#input-path", Input).focus()

    def on_resize(self, event: Resize) -> None:
        self._apply_responsive_layout(event.size.width)

    def get_system_commands(self, screen: Screen[object]) -> Iterator[SystemCommand]:
        yield from super().get_system_commands(screen)
        yield SystemCommand(
            "Run pipeline", "Start parler with the current form", self.action_run_pipeline
        )
        yield SystemCommand(
            "Load checkpoint", "Load state from the checkpoint path", self.action_load_state
        )
        yield SystemCommand(
            "French demo", "Load the French fixture preset", self.action_load_french_demo
        )
        yield SystemCommand(
            "Bilingual demo",
            "Load the bilingual fixture preset",
            self.action_load_bilingual_demo,
        )
        yield SystemCommand(
            "Earnings demo",
            "Load the long earnings-call fixture preset",
            self.action_load_earnings_demo,
        )
        yield SystemCommand(
            "Refresh cache", "Re-scan the cache directory", self.action_refresh_cache
        )
        yield SystemCommand("Results tab", "Jump to the Results tab", self.action_show_results)

    def action_run_pipeline(self) -> None:
        if self._pipeline_worker and self._pipeline_worker.state is WorkerState.RUNNING:
            self.notify("A pipeline run is already in progress.", severity="warning")
            return
        try:
            request = self.build_request()
        except ParlerError as exc:
            self.notify(str(exc), title="Cannot start run", severity="error")
            return
        self.current_request = request
        self._reset_runtime(request)
        self._set_busy(True)
        self._write_log(
            f"Starting pipeline for {request.input_path.name} · "
            f"langs={','.join(request.languages) or 'auto'} · format={request.output_format}"
        )
        self._pipeline_worker = self.run_worker(
            lambda: self._run_pipeline_worker(request),
            thread=True,
            exclusive=True,
            group="pipeline",
            description="Run parler pipeline",
            exit_on_error=False,
        )

    def action_load_state(self) -> None:
        checkpoint_path = _optional_path(
            self.project_root, self.query_one("#checkpoint-path", Input).value
        )
        if checkpoint_path is None:
            self.notify("Set a checkpoint path first.", severity="warning")
            return
        try:
            state = load_processing_state(checkpoint_path)
        except ParlerError as exc:
            self.notify(str(exc), title="Checkpoint error", severity="error")
            return
        self.current_request = None
        self.present_state(state, source=f"checkpoint · {checkpoint_path.name}")
        self._write_log(f"Loaded checkpoint {checkpoint_path}")
        self.notify("Checkpoint loaded.", severity="information")

    def action_focus_tree(self) -> None:
        self.query_one("#file-tree", DirectoryTree).focus()
        self._set_main_tab("artifacts-tab")

    def action_refresh_cache(self) -> None:
        self.refresh_cache_table()
        self.notify("Cache view refreshed.", severity="information", timeout=1.5)

    def action_show_studio(self) -> None:
        self._set_main_tab("studio-tab")

    def action_show_results(self) -> None:
        self._set_main_tab("results-tab")

    def action_show_artifacts(self) -> None:
        self._set_main_tab("artifacts-tab")

    def action_show_about(self) -> None:
        self._set_main_tab("about-tab")

    def action_load_french_demo(self) -> None:
        self.load_fixture("fr")

    def action_load_bilingual_demo(self) -> None:
        self.load_fixture("bilingual")

    def action_load_earnings_demo(self) -> None:
        self.load_fixture("earnings")

    @on(Button.Pressed)
    def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id
        if button_id == "run-button":
            self.action_run_pipeline()
        elif button_id == "load-state-button":
            self.action_load_state()
        elif button_id == "refresh-cache-button":
            self.action_refresh_cache()
        elif button_id == "clear-form-button":
            self.clear_form()
        elif button_id == "fixture-fr-button":
            self.action_load_french_demo()
        elif button_id == "fixture-bilingual-button":
            self.action_load_bilingual_demo()
        elif button_id == "fixture-earnings-button":
            self.action_load_earnings_demo()

    @on(DirectoryTree.FileSelected, "#file-tree")
    def on_file_selected(self, event: DirectoryTree.FileSelected) -> None:
        path = Path(event.path)
        self.preview_path(path)
        suffix = path.suffix.lower()
        display = _display_path(path, self.project_root)
        if suffix in AUDIO_EXTENSIONS:
            self.query_one("#input-path", Input).value = display
            self.notify(f"Audio input set to {display}", severity="information", timeout=1.5)
        elif suffix in {".toml", ".yaml", ".yml"}:
            self.query_one("#config-path", Input).value = display
            self.notify(f"Config path set to {display}", severity="information", timeout=1.5)
        elif suffix == ".json":
            self.query_one("#checkpoint-path", Input).value = display
            self.notify(f"Checkpoint path set to {display}", severity="information", timeout=1.5)

    @on(DataTable.RowSelected, "#cache-table")
    def on_cache_row_selected(self, event: DataTable.RowSelected) -> None:
        if str(event.row_key.value) == "empty":
            return
        path = Path(str(event.row_key.value))
        self.preview_path(path)

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        if event.worker is not self._pipeline_worker:
            return
        if event.state is WorkerState.RUNNING:
            self.sub_title = "Running pipeline…"
            self._set_topbar_pill("#topbar-mode", "Running", tone="accent")
            return
        self._set_busy(False)
        if event.state is WorkerState.SUCCESS:
            state = event.worker.result
            if state is None:
                self._write_log("Pipeline returned no state.")
                self.notify("Pipeline produced no result.", severity="warning")
                self.sub_title = "Idle"
                return
            self.present_state(state, source="live run")
            self._persist_output(state)
            self._write_log("Pipeline completed successfully.")
            self.notify("Pipeline complete.", severity="information")
            self.sub_title = "Run complete"
            self._set_topbar_pill("#topbar-mode", "Complete", tone="good")
            return
        if event.state is WorkerState.ERROR:
            error = event.worker.error
            self._write_log(f"Pipeline failed: {error}")
            self.notify(str(error), title="Pipeline failed", severity="error", timeout=8)
            self.sub_title = "Run failed"
            self._set_topbar_pill("#topbar-mode", "Failed", tone="warn")
            if self.current_request is not None:
                self._mark_remaining_error(self.current_request.expected_stages())

    def build_request(self) -> PipelineRequest:
        input_path = _optional_path(self.project_root, self.query_one("#input-path", Input).value)
        if input_path is None:
            raise InputError("Audio input is required.")
        config_path = _optional_path(self.project_root, self.query_one("#config-path", Input).value)
        output_path = _optional_path(self.project_root, self.query_one("#output-path", Input).value)
        checkpoint_path = _optional_path(
            self.project_root, self.query_one("#checkpoint-path", Input).value
        )
        meeting_date = _optional_date(self.query_one("#meeting-date", Input).value)
        languages = tuple(_split_csv(self.query_one("#languages-input", Input).value))
        participants = tuple(_split_csv(self.query_one("#participants-input", Input).value))
        output_format = str(self.query_one("#output-format-select", Select).value or "markdown")
        cache_dir = _optional_path(
            self.project_root, self.query_one("#cache-dir-input", Input).value
        )
        transcription_model = str(
            self.query_one("#transcription-model-select", Select).value or "voxtral-mini-latest"
        )
        extraction_model = str(
            self.query_one("#extraction-model-select", Select).value or "mistral-medium-latest"
        )
        return PipelineRequest(
            input_path=input_path,
            config_path=config_path,
            output_path=output_path,
            checkpoint_path=checkpoint_path,
            meeting_date=meeting_date,
            languages=languages,
            participants=participants,
            output_format=output_format,
            cache_dir=cache_dir,
            transcription_model=transcription_model,
            extraction_model=extraction_model,
            transcribe_only=self.query_one("#transcribe-only-switch", Switch).value,
            no_diarize=self.query_one("#no-diarize-switch", Switch).value,
            anonymize_speakers=self.query_one("#anonymize-switch", Switch).value,
            resume=self.query_one("#resume-switch", Switch).value,
        )

    def load_fixture(self, fixture_key: str) -> None:
        preset = FIXTURE_PRESETS[fixture_key]
        self.query_one("#input-path", Input).value = _display_path(preset.path, self.project_root)
        self.query_one("#meeting-date", Input).value = preset.meeting_date.isoformat()
        self.query_one("#languages-input", Input).value = ",".join(preset.languages)
        self.query_one("#participants-input", Input).value = ", ".join(preset.participants)
        self.query_one("#output-format-select", Select).value = preset.output_format
        self.query_one("#transcribe-only-switch", Switch).value = False
        self.query_one("#no-diarize-switch", Switch).value = False
        self.query_one("#anonymize-switch", Switch).value = False
        self.query_one("#resume-switch", Switch).value = False
        self.preview_path((self.project_root / preset.path).resolve())
        self._set_main_tab("studio-tab")
        self._write_log(f"Loaded fixture preset: {preset.name}")
        self.notify(f"{preset.name} loaded.", severity="information", timeout=1.5)

    def clear_form(self) -> None:
        for widget_id in (
            "#input-path",
            "#config-path",
            "#output-path",
            "#meeting-date",
            "#languages-input",
            "#participants-input",
        ):
            self.query_one(widget_id, Input).value = ""
        self.query_one("#transcribe-only-switch", Switch).value = False
        self.query_one("#no-diarize-switch", Switch).value = False
        self.query_one("#anonymize-switch", Switch).value = False
        self.query_one("#resume-switch", Switch).value = False
        self._reset_runtime()
        self._refresh_metrics()
        self.notify("Form cleared.", severity="information", timeout=1.5)

    def present_state(self, state: ProcessingState, *, source: str) -> None:
        self.current_state = state
        self._update_results_hero(state, source)
        self._update_metrics_from_state(state)
        self._populate_tables(state.decision_log)
        self._update_transcript_view(state.transcript or state.attributed_transcript)
        self._update_report_view(state.decision_log)
        self._update_raw_output_view(state)
        self._apply_stage_completion(state)
        self.refresh_cache_table()
        self._set_main_tab("results-tab")

    def refresh_cache_table(self) -> None:
        cache_dir = self._resolved_cache_dir()
        table = self.query_one("#cache-table", DataTable)
        table.clear(columns=False)
        entries = sorted(cache_dir.glob("*.json")) if cache_dir.exists() else []
        if not entries:
            table.add_row("No cache entries", "-", "-", "-", key="empty")
            self.preview_path(cache_dir)
            self._set_metric(
                "#metric-cache",
                "Cache",
                "0 entries",
                str(cache_dir),
            )
            return
        for path in entries:
            stat = path.stat()
            kind = self._cache_kind(path)
            modified = datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M")
            table.add_row(
                path.stem[:16],
                kind,
                f"{stat.st_size / 1024:.1f} KB",
                modified,
                key=str(path),
            )
        self._set_metric(
            "#metric-cache",
            "Cache",
            f"{len(entries)} entries",
            str(cache_dir),
        )

    def preview_path(self, path: Path) -> None:
        widget = self.query_one("#artifact-preview", Static)
        if not path.exists():
            widget.update(f"Missing path\n{path}")
            return
        if path.is_dir():
            children = sorted(child.name for child in path.iterdir())[:32]
            preview = "\n".join(children) or "(empty directory)"
            widget.update(f"Directory\n{path}\n\n{preview}")
            return
        widget.update(_preview_text(path))

    def _run_pipeline_worker(self, request: PipelineRequest) -> ProcessingState | None:
        config = build_tui_config(request)
        orchestrator = PipelineOrchestrator(config)
        return orchestrator.run(
            request.input_path,
            transcribe_only=request.transcribe_only,
            no_diarize=request.no_diarize,
            checkpoint_path=request.checkpoint_path,
            resume=request.resume,
            on_cost_confirm=lambda _: True,
            on_stage_start=lambda stage: self.call_from_thread(self._handle_stage_start, stage),
            on_stage_complete=lambda stage, duration: self.call_from_thread(
                self._handle_stage_complete, stage, duration
            ),
        )

    def _configure_tables(self) -> None:
        decision_table = self.query_one("#decision-table", DataTable)
        decision_table.add_columns("ID", "Summary", "Speaker", "Time", "Lang", "Confidence")

        commitment_table = self.query_one("#commitment-table", DataTable)
        commitment_table.add_columns("ID", "Owner", "Action", "Deadline", "Lang", "Confidence")

        question_table = self.query_one("#question-table", DataTable)
        question_table.add_columns("ID", "Question", "Asked by", "Time", "Lang")

        rejection_table = self.query_one("#rejection-table", DataTable)
        rejection_table.add_columns("ID", "Summary", "Reason", "Time", "Lang")

        cache_table = self.query_one("#cache-table", DataTable)
        cache_table.add_columns("Key", "Kind", "Size", "Modified")

    def _seed_defaults(self) -> None:
        try:
            defaults = load_config(config_path=None)
        except ParlerError:
            defaults = _safe_defaults()
        self.query_one("#cache-dir-input", Input).value = _display_path(
            defaults.cache.directory,
            self.project_root,
        )
        self._set_select_value(
            "#transcription-model-select",
            defaults.transcription.model,
            allowed_values=TRANSCRIPTION_MODEL_VALUES,
        )
        self._set_select_value(
            "#extraction-model-select",
            defaults.extraction.model,
            allowed_values=EXTRACTION_MODEL_VALUES,
        )
        self._set_select_value(
            "#output-format-select",
            defaults.output.format,
            allowed_values=FORMAT_VALUES,
        )
        if defaults.participants:
            self.query_one("#participants-input", Input).value = ", ".join(defaults.participants)

    def _about_markdown(self) -> str:
        return (
            "# parler 🇫🇷\n\n"
            "A Textual cockpit for the full parler workflow.\n\n"
            "## What this UI is for\n\n"
            "- run the pipeline against local audio files or showcase fixtures\n"
            "- watch stage-by-stage progress in real time\n"
            "- inspect transcripts, decisions, commitments, questions, and rejected proposals\n"
            "- browse cache artifacts and project files without leaving the app\n"
            "- keep everything keyboard-driven via bindings and the command palette\n\n"
            "## Quick keys\n\n"
            "- `Ctrl+R`: run pipeline\n"
            "- `Ctrl+O`: load checkpoint\n"
            "- `Ctrl+1..4`: switch tabs\n"
            "- `Ctrl+F`: French fixture\n"
            "- `Ctrl+B`: bilingual fixture\n"
            "- `Ctrl+E`: earnings fixture\n"
            "- `Ctrl+G`: focus file tree\n"
            "- `F5`: refresh cache\n"
            "- `Ctrl+P`: command palette\n\n"
            "## Showcase flow\n\n"
            "1. Load a fixture from the left rail.\n"
            "2. Press `Ctrl+R`.\n"
            "3. Watch the stage strip and runtime log.\n"
            "4. Review the report and structured tables in Results.\n"
            "5. Use Artifacts to inspect cache entries and local files.\n"
        )

    def _resolved_cache_dir(self) -> Path:
        raw = self.query_one("#cache-dir-input", Input).value
        cache_dir = _optional_path(self.project_root, raw)
        return cache_dir or (self.project_root / ".parler-cache")

    def _cache_kind(self, path: Path) -> str:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return "json"
        if "transcript" in payload:
            return "transcript"
        if "decision_log" in payload:
            return "decision-log"
        return "json"

    def _handle_stage_start(self, stage: PipelineStage) -> None:
        self._set_stage(stage, "running", "Running")
        self.sub_title = f"{STAGE_LABELS[stage]}…"
        self._write_log(f"{STAGE_LABELS[stage]} started")

    def _handle_stage_complete(self, stage: PipelineStage, duration: float) -> None:
        self._set_stage(stage, "complete", f"{duration:.1f}s")
        progress = self._completed_stage_count()
        self.query_one("#stage-progress", ProgressBar).update(progress=progress)
        self._write_log(f"{STAGE_LABELS[stage]} complete in {duration:.1f}s")

    def _completed_stage_count(self) -> int:
        return sum(
            1
            for stage in PipelineStage
            if self.query_one(f"#stage-{stage.name.lower()}", Static).has_class("is-complete")
        )

    def _mark_remaining_error(self, expected_stages: tuple[PipelineStage, ...]) -> None:
        for stage in expected_stages:
            tile = self.query_one(f"#stage-{stage.name.lower()}", Static)
            if tile.has_class("is-complete"):
                continue
            if tile.has_class("is-running"):
                self._set_stage(stage, "error", "Failed")
                break

    def _apply_stage_completion(self, state: ProcessingState) -> None:
        request = self.current_request
        expected = request.expected_stages() if request is not None else tuple(PipelineStage)
        for stage in expected:
            if stage == PipelineStage.INGEST and state.audio_file is not None:
                self._set_stage(stage, "complete", "Ready")
                continue
            if stage in state.completed_stages:
                self._set_stage(stage, "complete", "Done")
            elif request is not None and stage not in request.expected_stages():
                self._set_stage(stage, "skipped", "Skipped")
            else:
                self._set_stage(stage, "pending", "Pending")
        self.query_one("#stage-progress", ProgressBar).update(
            total=len(expected),
            progress=sum(
                1
                for stage in expected
                if self.query_one(f"#stage-{stage.name.lower()}", Static).has_class("is-complete")
            ),
        )

    def _set_stage(self, stage: PipelineStage, tone: str, detail: str) -> None:
        tile = self.query_one(f"#stage-{stage.name.lower()}", Static)
        tile.update(f"{STAGE_LABELS[stage]}\n{detail}")
        tile.remove_class("is-pending", "is-running", "is-complete", "is-error", "is-skipped")
        tile.add_class(f"is-{tone}")

    def _set_busy(self, busy: bool) -> None:
        self.query_one("#run-button", Button).disabled = busy
        self.query_one("#load-state-button", Button).disabled = busy
        if busy:
            self._set_topbar_pill("#topbar-mode", "Running", tone="accent")
            self.query_one("#run-summary", Static).update(
                "Pipeline in flight.\nThe app stays responsive while workers handle API calls."
            )
        else:
            self.sub_title = "Decision intelligence cockpit"
            if self.current_state is None:
                self._set_topbar_pill("#topbar-mode", "Idle", tone="quiet")

    def _reset_runtime(self, request: PipelineRequest | None = None) -> None:
        self.query_one("#run-log", RichLog).clear()
        expected = request.expected_stages() if request is not None else tuple(PipelineStage)
        for stage in PipelineStage:
            if stage in expected:
                self._set_stage(stage, "pending", "Waiting")
            else:
                self._set_stage(stage, "skipped", "Skipped")
        self.query_one("#stage-progress", ProgressBar).update(total=len(expected), progress=0)
        if request is not None:
            participants = ", ".join(request.participants) or "none"
            languages = ", ".join(request.languages) or "auto"
            self.query_one("#run-summary", Static).update(
                f"{request.input_path.name}\n"
                f"Languages: {languages}\n"
                f"Participants: {participants}\n"
                f"Format: {request.output_format}"
            )
        else:
            self.query_one("#run-summary", Static).update(
                "Ready for a run.\n"
                "Choose a fixture or point at your own audio.\n"
                "If API access is missing, add MISTRAL_API_KEY to .env."
            )

    def _write_log(self, message: str) -> None:
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.query_one("#run-log", RichLog).write(f"[{timestamp}] {message}")

    def _refresh_metrics(self) -> None:
        self._set_metric("#metric-mode", "Mode", "Idle", "Textual showcase")
        api_ready = (
            "Ready"
            if (os.environ.get("MISTRAL_API_KEY") or os.environ.get("PARLER_API_KEY"))
            else "Missing"
        )
        self._set_topbar_pill(
            "#topbar-api",
            f"API {api_ready}",
            tone="good" if api_ready == "Ready" else "warn",
        )
        self._set_topbar_pill("#topbar-mode", "Idle", tone="quiet")
        self._set_metric(
            "#metric-api",
            "API key",
            api_ready,
            ".env -> MISTRAL_API_KEY or parler.toml api_key",
        )
        self._set_metric("#metric-audio", "Audio", "No file", "select or load a fixture")
        self._set_metric("#metric-decisions", "Decisions", "0", "awaiting extraction")
        self._set_metric("#metric-commitments", "Commitments", "0", "awaiting extraction")
        self._set_metric("#metric-languages", "Languages", "-", "no transcript loaded")
        self._set_metric("#metric-cache", "Cache", "-", str(self._resolved_cache_dir()))

    def _update_metrics_from_state(self, state: ProcessingState) -> None:
        audio_label = state.audio_file.path.name if state.audio_file is not None else "No file"
        audio_detail = (
            f"{state.audio_file.duration_s:.1f}s"
            if state.audio_file is not None
            else "no audio metadata"
        )
        self._set_topbar_pill("#topbar-mode", "Loaded", tone="good")
        self._set_metric("#metric-mode", "Mode", "Complete", "results loaded")
        self._set_metric("#metric-audio", "Audio", audio_label, audio_detail)
        decision_count = len(state.decision_log.decisions) if state.decision_log is not None else 0
        commitment_count = (
            len(state.decision_log.commitments) if state.decision_log is not None else 0
        )
        self._set_metric("#metric-decisions", "Decisions", str(decision_count), "structured output")
        self._set_metric(
            "#metric-commitments",
            "Commitments",
            str(commitment_count),
            "actionable follow-up",
        )
        transcript = state.attributed_transcript or state.transcript
        languages = self._language_label(transcript)
        self._set_metric("#metric-languages", "Languages", languages or "-", "detected")

    def _set_metric(self, widget_id: str, label: str, value: str, detail: str) -> None:
        self.query_one(widget_id, Static).update(f"{label}\n{value}\n{detail}")

    def _set_topbar_pill(self, widget_id: str, text: str, *, tone: str) -> None:
        pill = self.query_one(widget_id, Static)
        pill.update(text)
        pill.remove_class("accent", "good", "warn", "quiet")
        pill.add_class(tone)

    def _update_results_hero(self, state: ProcessingState, source: str) -> None:
        transcript = state.attributed_transcript or state.transcript
        language_text = self._language_label(transcript)
        decision_count = len(state.decision_log.decisions) if state.decision_log else 0
        commitment_count = len(state.decision_log.commitments) if state.decision_log else 0
        report_model = state.decision_log.metadata.model if state.decision_log else "n/a"
        self.query_one("#results-hero", Static).update(
            f"{source}\n"
            f"Languages: {language_text}\n"
            f"Decisions: {decision_count} · Commitments: {commitment_count}\n"
            f"Model: {report_model}"
        )

    def _update_transcript_view(self, transcript: Transcript | None) -> None:
        widget = self.query_one("#transcript-view", Static)
        if transcript is None:
            widget.update("No transcript yet.")
            return
        widget.update(f"Languages: {self._language_label(transcript)}\n\n{transcript.text}")

    def _update_report_view(self, decision_log: DecisionLog | None) -> None:
        widget = self.query_one("#report-markdown", Markdown)
        if decision_log is None:
            widget.update("No report yet.")
            return
        preview = ReportRenderer().render(
            decision_log,
            RenderConfig(format=OutputFormat.MARKDOWN, include_quotes=True),
        )
        widget.update(preview)

    def _update_raw_output_view(self, state: ProcessingState) -> None:
        payload = self._serialize_output_payload(state)
        self._last_output_payload = payload
        self.query_one("#raw-output-view", Static).update(payload or "No serialized output yet.")

    def _serialize_output_payload(self, state: ProcessingState) -> str:
        if self.current_request is None:
            return json.dumps(checkpoint_payload(state), indent=2, ensure_ascii=False)
        request = self.current_request
        if request.transcribe_only:
            transcript = state.transcript
            if transcript is None:
                return ""
            if request.output_format == "json":
                return json.dumps(to_jsonable(transcript), indent=2, ensure_ascii=False)
            return transcript.text
        if state.decision_log is None:
            return ""
        renderer = ReportRenderer()
        return renderer.render(
            state.decision_log,
            RenderConfig(format=OutputFormat(request.output_format)),
        )

    def _persist_output(self, state: ProcessingState) -> None:
        request = self.current_request
        if request is None:
            return
        target_path = request.output_path
        if target_path is None:
            target_path = build_tui_config(request).output.output_path
        if target_path is None:
            return
        payload = self._serialize_output_payload(state)
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_text(payload, encoding="utf-8")
        self._write_log(f"Wrote output to {target_path}")

    def _populate_tables(self, decision_log: DecisionLog | None) -> None:
        self._populate_decision_table(decision_log.decisions if decision_log else ())
        self._populate_commitment_table(decision_log.commitments if decision_log else ())
        self._populate_question_table(decision_log.open_questions if decision_log else ())
        self._populate_rejection_table(decision_log.rejected if decision_log else ())

    def _populate_decision_table(self, decisions: tuple[Decision, ...]) -> None:
        table = self.query_one("#decision-table", DataTable)
        table.clear(columns=False)
        if not decisions:
            table.add_row("—", "No decisions yet", "—", "—", "—", "—", key="empty")
            return
        for decision in decisions:
            table.add_row(
                decision.id,
                decision.summary,
                decision.speaker or "—",
                self._timestamp(decision.timestamp_s),
                decision.language,
                decision.confidence,
                key=decision.id,
            )

    def _populate_commitment_table(self, commitments: tuple[Commitment, ...]) -> None:
        table = self.query_one("#commitment-table", DataTable)
        table.clear(columns=False)
        if not commitments:
            table.add_row("—", "—", "No commitments yet", "—", "—", "—", key="empty")
            return
        for commitment in commitments:
            deadline = (
                commitment.deadline.resolved_date.isoformat()
                if commitment.deadline and commitment.deadline.resolved_date
                else (commitment.deadline.raw if commitment.deadline else "—")
            )
            table.add_row(
                commitment.id,
                commitment.owner,
                commitment.action,
                deadline,
                commitment.language,
                commitment.confidence,
                key=commitment.id,
            )

    def _populate_question_table(self, questions: tuple[OpenQuestion, ...]) -> None:
        table = self.query_one("#question-table", DataTable)
        table.clear(columns=False)
        if not questions:
            table.add_row("—", "No open questions", "—", "—", "—", key="empty")
            return
        for question in questions:
            table.add_row(
                question.id,
                question.question,
                question.asked_by or "—",
                self._timestamp(question.timestamp_s),
                question.language,
                key=question.id,
            )

    def _populate_rejection_table(self, rejections: tuple[Rejection, ...]) -> None:
        table = self.query_one("#rejection-table", DataTable)
        table.clear(columns=False)
        if not rejections:
            table.add_row("—", "No rejected proposals", "—", "—", "—", key="empty")
            return
        for rejection in rejections:
            table.add_row(
                rejection.id,
                rejection.summary,
                rejection.reason or "—",
                self._timestamp(rejection.timestamp_s),
                rejection.language,
                key=rejection.id,
            )

    def _apply_responsive_layout(self, width: int) -> None:
        self._toggle_widget_class(
            self.query_one("#shell", Horizontal), "stack", width < SHELL_STACK_WIDTH
        )
        self._toggle_widget_class(
            self.query_one("#studio-layout", Horizontal),
            "stack",
            width < STUDIO_STACK_WIDTH,
        )
        self._toggle_widget_class(
            self.query_one("#artifacts-layout", Horizontal),
            "stack",
            width < STUDIO_STACK_WIDTH,
        )
        self._toggle_widget_class(
            self.query_one("#form-scroll", VerticalScroll),
            "compact",
            width < FORM_STACK_WIDTH,
        )
        stage_grid = self.query_one("#stage-grid", Grid)
        self._toggle_widget_class(stage_grid, "compact", width < STUDIO_STACK_WIDTH)
        self._toggle_widget_class(stage_grid, "dense", width < SHELL_STACK_WIDTH)
        self._toggle_widget_class(
            self.query_one("#topbar", Horizontal), "compact", width < FORM_STACK_WIDTH
        )

    def _toggle_widget_class(
        self, widget: Static | Horizontal | VerticalScroll | Grid, name: str, enabled: bool
    ) -> None:
        if enabled:
            widget.add_class(name)
        else:
            widget.remove_class(name)

    def _set_main_tab(self, tab_id: str) -> None:
        self.query_one("#main-tabs", TabbedContent).active = tab_id

    def _set_select_value(
        self,
        widget_id: str,
        value: str,
        *,
        allowed_values: set[str],
    ) -> None:
        if value not in allowed_values:
            return
        self.query_one(widget_id, Select).value = value

    def _language_label(self, transcript: Transcript | None) -> str:
        if transcript is None:
            return "unspecified"
        languages = tuple(lang for lang in transcript.detected_languages if lang and lang != "None")
        if languages:
            return ", ".join(languages)
        if transcript.language and transcript.language != "None":
            return transcript.language
        return "unspecified"

    def _timestamp(self, value: float | None) -> str:
        if value is None:
            return "—"
        minutes, seconds = divmod(round(value), 60)
        return f"{minutes:02d}:{seconds:02d}"


def main(*, project_root: Path | None = None) -> None:
    """Launch the Textual app."""

    resolved_root = (project_root or Path.cwd()).resolve()
    load_env_file(resolved_root / DEFAULT_ENV_FILE)
    apply_api_key_aliases()
    ParlerTUIApp(project_root=resolved_root).run()


__all__ = [
    "FIXTURE_PRESETS",
    "ParlerTUIApp",
    "PipelineRequest",
    "build_tui_config",
    "main",
]
