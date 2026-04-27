"""Focused CLI tests for the Phase 7 command surface."""

from __future__ import annotations

import json
import os
from dataclasses import replace
from datetime import date
from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner
from parler.cli import cli, main
from parler.config import CacheConfig, OutputConfig, ParlerConfig
from parler.models import (
    AudioFile,
    Commitment,
    CommitmentDeadline,
    Decision,
    DecisionLog,
    ExtractionMetadata,
    Transcript,
    TranscriptSegment,
)
from parler.pipeline.state import PipelineStage, ProcessingState, save_processing_state
from parler.runlog import RunRecorder


def make_audio_file(path: str = "/tmp/meeting.mp3") -> AudioFile:
    return AudioFile(
        path=Path(path),
        original_path=None,
        format="mp3",
        duration_s=600.0,
        sample_rate=44100,
        channels=2,
        size_bytes=10_000_000,
        content_hash="abc123abc123",
    )


def make_transcript() -> Transcript:
    segment = TranscriptSegment(
        id=0,
        start_s=0.0,
        end_s=5.0,
        text="Bonjour.",
        language="fr",
        speaker_id=None,
        speaker_confidence=None,
        confidence=0.9,
        no_speech_prob=0.01,
        code_switch=False,
        words=None,
    )
    return Transcript(
        text="Bonjour.",
        language="fr",
        duration_s=5.0,
        segments=(segment,),
    )


def make_decision_log() -> DecisionLog:
    return DecisionLog(
        decisions=(
            Decision(
                id="D1",
                summary="Launch date set to May 15",
                timestamp_s=42.0,
                speaker="Pierre",
                confirmed_by=("Sophie",),
                quote="On part sur le 15 mai.",
                confidence="high",
                language="fr",
            ),
        ),
        commitments=(
            Commitment(
                id="C1",
                owner="Sophie",
                action="Review deployment checklist",
                deadline=CommitmentDeadline(
                    raw="vendredi prochain",
                    resolved_date=date(2026, 4, 17),
                    is_explicit=False,
                ),
                timestamp_s=82.0,
                quote="Je vais revoir la checklist.",
                confidence="high",
                language="fr",
            ),
        ),
        rejected=(),
        open_questions=(),
        metadata=ExtractionMetadata(
            model="mistral-large-latest",
            prompt_version="v1.2.0",
            meeting_date=date(2026, 4, 9),
            extracted_at="2026-04-09T10:30:00Z",
            input_tokens=512,
            output_tokens=128,
        ),
    )


def make_state(
    *,
    transcript: Transcript | None = None,
    decision_log: DecisionLog | None = None,
    report: str | None = None,
) -> ProcessingState:
    return ProcessingState(
        audio_file=make_audio_file(),
        transcript=transcript,
        attributed_transcript=None,
        decision_log=decision_log,
        report=report,
        completed_stages=frozenset(),
        checkpoint_path=None,
    )


def make_config(
    *,
    cache_dir: Path | None = None,
    output_format: str = "markdown",
    output_path: Path | None = None,
) -> ParlerConfig:
    return ParlerConfig(
        api_key="test-api-key",
        cache=CacheConfig(directory=cache_dir or Path(".parler-cache")),
        output=OutputConfig(format=output_format, output_path=output_path),
    )


class TestProcessCommand:
    def test_process_writes_default_markdown_output_file(self) -> None:
        runner = CliRunner()
        state = make_state(decision_log=make_decision_log(), report="# Decision Log\n")

        with runner.isolated_filesystem():
            with (
                patch("parler.cli.load_config", return_value=make_config()),
                patch("parler.cli.PipelineOrchestrator") as mock_orchestrator,
            ):
                mock_orchestrator.return_value.run.return_value = state
                result = runner.invoke(cli, ["process", "meeting.mp3"])

            assert result.exit_code == 0
            output_path = Path("meeting-decisions.md")
            assert output_path.exists()
            assert "# Decision Log" in output_path.read_text(encoding="utf-8")

    def test_process_infers_html_format_from_output_extension(self) -> None:
        runner = CliRunner()
        captured_overrides: dict[str, object] = {}

        def fake_load_config(
            *, config_path: Path | None = None, overrides: dict[str, object] | None = None
        ) -> ParlerConfig:
            del config_path
            captured_overrides.update(overrides or {})
            return make_config(
                output_format=str(captured_overrides.get("output.format", "markdown")),
                output_path=Path(str(captured_overrides["output.output_path"])),
            )

        with runner.isolated_filesystem():
            with (
                patch("parler.cli.load_config", side_effect=fake_load_config),
                patch("parler.cli.PipelineOrchestrator") as mock_orchestrator,
            ):
                mock_orchestrator.return_value.run.return_value = make_state(
                    decision_log=make_decision_log(),
                    report="<html></html>",
                )
                result = runner.invoke(
                    cli,
                    ["process", "meeting.mp3", "--output", "custom-report.html"],
                )

            assert result.exit_code == 0
            assert captured_overrides["output.format"] == "html"

    def test_process_cost_estimate_skips_pipeline_execution(self) -> None:
        runner = CliRunner()

        with (
            patch("parler.cli.load_config", return_value=make_config()),
            patch("parler.cli.AudioIngester") as mock_ingester,
            patch("parler.cli.estimate_cost", return_value=1.23),
            patch("parler.cli.PipelineOrchestrator") as mock_orchestrator,
        ):
            mock_ingester.return_value.ingest.return_value = make_audio_file()
            result = runner.invoke(cli, ["process", "meeting.mp3", "--cost-estimate"])

        assert result.exit_code == 0
        assert "1.23" in result.output
        mock_orchestrator.assert_not_called()

    def test_process_local_cost_estimate_uses_zero_cost_without_audio_ingest(self) -> None:
        runner = CliRunner()
        captured_overrides: dict[str, object] = {}

        def fake_load_config(
            *, config_path: Path | None = None, overrides: dict[str, object] | None = None
        ) -> ParlerConfig:
            del config_path
            captured_overrides.update(overrides or {})
            return make_config()

        with (
            patch("parler.cli.load_config", side_effect=fake_load_config),
            patch("parler.cli.AudioIngester") as mock_ingester,
            patch("parler.cli.PipelineOrchestrator") as mock_orchestrator,
        ):
            result = runner.invoke(cli, ["process", "meeting.mp3", "--local", "--cost-estimate"])

        assert result.exit_code == 0
        assert "Estimated total cost: $0.00 (local inference)" in result.output
        assert captured_overrides["api_key"] == "local-mode"
        assert str(captured_overrides["transcription.model"]).startswith("local:")
        assert str(captured_overrides["extraction.model"]).startswith("local:")
        mock_ingester.assert_not_called()
        mock_orchestrator.assert_not_called()

    def test_process_records_run_artifacts(self) -> None:
        runner = CliRunner()
        state = make_state(decision_log=make_decision_log(), report="# Decision Log\n")

        def fake_run(*args, **kwargs):
            del args
            kwargs["on_stage_start"](PipelineStage.INGEST)
            kwargs["on_stage_complete"](PipelineStage.INGEST, 0.125)
            kwargs["on_stage_start"](PipelineStage.TRANSCRIBE)
            kwargs["on_stage_complete"](PipelineStage.TRANSCRIBE, 0.5)
            return state

        with runner.isolated_filesystem():
            with (
                patch("parler.cli.load_config", return_value=make_config()),
                patch("parler.cli.PipelineOrchestrator") as mock_orchestrator,
            ):
                mock_orchestrator.return_value.run.side_effect = fake_run
                result = runner.invoke(cli, ["process", "meeting.mp3"])

            assert result.exit_code == 0
            summaries = sorted(Path(".parler-runs").glob("*/run.json"))
            assert len(summaries) == 1
            payload = json.loads(summaries[0].read_text(encoding="utf-8"))
            assert payload["command"] == "process"
            assert payload["status"] == "completed"
            assert payload["result"]["decision_log"]["decision_count"] == 1
            assert payload["stages"]["INGEST"]["status"] == "completed"
            events = summaries[0].with_name("events.jsonl").read_text(encoding="utf-8")
            assert "stage_started" in events
            assert "run_completed" in events

    def test_process_verbose_logs_pipeline_context_to_stderr(self) -> None:
        runner = CliRunner(mix_stderr=False)
        state = make_state(
            transcript=make_transcript(),
            decision_log=make_decision_log(),
            report="# Decision Log\n",
        )

        def fake_run(*args, **kwargs):
            del args
            for stage in (
                PipelineStage.INGEST,
                PipelineStage.TRANSCRIBE,
                PipelineStage.ATTRIBUTE,
                PipelineStage.EXTRACT,
                PipelineStage.RENDER,
            ):
                kwargs["on_stage_start"](stage)
                kwargs["on_stage_complete"](stage, 0.25)
            return state

        with runner.isolated_filesystem():
            with (
                patch("parler.cli.load_config", return_value=make_config()),
                patch("parler.cli.PipelineOrchestrator") as mock_orchestrator,
            ):
                mock_orchestrator.return_value.run.side_effect = fake_run
                result = runner.invoke(cli, ["process", "meeting.mp3", "--verbose"])

            assert result.exit_code == 0
            output_path = Path(result.stdout.strip())
            assert output_path.name == "meeting-decisions.md"
            assert output_path.exists()
            assert (
                "[verbose] command=process input=meeting.mp3 format=markdown "
                "checkpoint=- resume=no execution=remote"
            ) in result.stderr
            assert (
                "[verbose] models transcription=voxtral-mini-latest "
                "extraction=mistral-medium-latest"
            ) in result.stderr
            assert "[verbose] trace_id=" in result.stderr
            assert (
                "[verbose] ingest: meeting.mp3 (probe input and normalize audio)" in result.stderr
            )
            assert (
                "[verbose] transcribe: model=voxtral-mini-latest languages=auto cache=on"
            ) in result.stderr
            assert "[verbose] attribute: participants=0 anonymize=no" in result.stderr
            assert (
                "[verbose] extract: model=mistral-medium-latest prompt=v1.0 "
                "meeting_date=unspecified cache=on"
            ) in result.stderr
            assert "[verbose] render: format=markdown" in result.stderr
            assert (
                "[verbose] transcript=segments=1 language=fr detected=fr model=-" in result.stderr
            )
            assert (
                "[verbose] decision_log=decisions=1 commitments=1 questions=0 "
                "rejected=0 model=mistral-large-latest"
            ) in result.stderr
            assert "[verbose] report_bytes=" in result.stderr
            assert f"[verbose] wrote_output={output_path}" in result.stderr


class TestTranscribeCommand:
    def test_transcribe_json_output_contains_transcript_not_decisions(self, tmp_path: Path) -> None:
        runner = CliRunner()
        output_path = tmp_path / "transcript.json"
        state = make_state(transcript=make_transcript())

        with (
            patch("parler.cli.load_config", return_value=make_config()),
            patch("parler.cli.PipelineOrchestrator") as mock_orchestrator,
        ):
            mock_orchestrator.return_value.run.return_value = state
            result = runner.invoke(
                cli,
                ["transcribe", "meeting.mp3", "--output", str(output_path), "--format", "json"],
            )

        assert result.exit_code == 0
        payload = json.loads(output_path.read_text(encoding="utf-8"))
        assert payload["text"] == "Bonjour."
        assert "decisions" not in payload

    def test_transcribe_verbose_logs_resume_details_to_stderr(self) -> None:
        runner = CliRunner(mix_stderr=False)
        state = replace(
            make_state(transcript=make_transcript()),
            completed_stages=frozenset({PipelineStage.INGEST, PipelineStage.TRANSCRIBE}),
            checkpoint_path=Path(".parler-state.json"),
        )

        def fake_run(*args, **kwargs):
            del args
            kwargs["on_stage_start"](PipelineStage.INGEST)
            kwargs["on_stage_complete"](PipelineStage.INGEST, 0.25)
            return state

        with runner.isolated_filesystem():
            with (
                patch("parler.cli.load_config", return_value=make_config()),
                patch("parler.cli.PipelineOrchestrator") as mock_orchestrator,
            ):
                mock_orchestrator.return_value.run.side_effect = fake_run
                result = runner.invoke(
                    cli,
                    [
                        "transcribe",
                        "meeting.mp3",
                        "--resume",
                        "--verbose",
                        "--output",
                        "transcript.txt",
                    ],
                )

            assert result.exit_code == 0
            assert result.stdout.strip() == "transcript.txt"
            assert Path("transcript.txt").read_text(encoding="utf-8") == "Bonjour."
            assert (
                "[verbose] command=transcribe input=meeting.mp3 format=text "
                "checkpoint=auto (.parler-state.json) resume=yes execution=remote"
            ) in result.stderr
            assert (
                "[verbose] model transcription=voxtral-mini-latest "
                "languages=auto cache_dir=.parler-cache"
            ) in result.stderr
            assert "[verbose] trace_id=" in result.stderr
            assert (
                "[verbose] ingest: meeting.mp3 (probe input and normalize audio)" in result.stderr
            )
            assert "[verbose] ingest: complete in 0.25s" in result.stderr
            assert "[verbose] reused completed stages from checkpoint: transcribe" in result.stderr
            assert (
                "[verbose] transcript=segments=1 language=fr detected=fr model=-" in result.stderr
            )
            assert "[verbose] wrote_output=transcript.txt" in result.stderr


class TestExtractAndReportCommands:
    def test_extract_from_state_updates_checkpoint_and_prints_json(self, tmp_path: Path) -> None:
        runner = CliRunner()
        state_path = tmp_path / ".parler-state.json"
        save_processing_state(state_path, make_state(transcript=make_transcript()))

        with (
            patch("parler.cli.load_config", return_value=make_config()),
            patch("parler.cli.DecisionExtractor") as mock_extractor,
        ):
            mock_extractor.return_value.extract.return_value = make_decision_log()
            result = runner.invoke(
                cli, ["extract", "--from-state", str(state_path), "--format", "json"]
            )

        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert payload["decisions"][0]["id"] == "D1"

        saved_state = json.loads(state_path.read_text(encoding="utf-8"))
        assert "decision_log" in saved_state

    def test_report_from_state_renders_html_without_pipeline(self, tmp_path: Path) -> None:
        runner = CliRunner()
        state_path = tmp_path / ".parler-state.json"
        output_path = tmp_path / "report.html"
        save_processing_state(state_path, make_state(decision_log=make_decision_log()))

        with patch("parler.cli.PipelineOrchestrator") as mock_orchestrator:
            result = runner.invoke(
                cli,
                [
                    "report",
                    "--from-state",
                    str(state_path),
                    "--format",
                    "html",
                    "--output",
                    str(output_path),
                ],
            )

        assert result.exit_code == 0
        assert "<html" in output_path.read_text(encoding="utf-8")
        mock_orchestrator.assert_not_called()


class TestCacheCommands:
    def test_cache_list_show_and_clear(self, tmp_path: Path) -> None:
        runner = CliRunner()
        cache_dir = tmp_path / ".parler-cache"
        cache_dir.mkdir()
        first = cache_dir / "abc123.json"
        second = cache_dir / "def456.json"
        first.write_text('{"kind":"transcript"}', encoding="utf-8")
        second.write_text('{"kind":"decision_log"}', encoding="utf-8")
        config = make_config(cache_dir=cache_dir)

        with patch("parler.cli.load_config", return_value=config):
            list_result = runner.invoke(cli, ["cache", "list"])
            show_result = runner.invoke(cli, ["cache", "show", "abc123"])
            clear_result = runner.invoke(cli, ["cache", "clear", "--yes"])

        assert list_result.exit_code == 0
        assert "abc123" in list_result.output
        assert "def456" in list_result.output

        assert show_result.exit_code == 0
        assert '"kind":"transcript"' in show_result.output

        assert clear_result.exit_code == 0
        assert not any(cache_dir.glob("*.json"))


class TestOperationalCommands:
    def test_doctor_fails_without_api_key(self) -> None:
        runner = CliRunner()

        with runner.isolated_filesystem(), patch.dict(os.environ, {}, clear=True):
            result = runner.invoke(cli, ["doctor", "--json"])

        assert result.exit_code == 1
        payload = json.loads(result.output)
        assert payload["ready"] is False
        failing_checks = [check for check in payload["checks"] if check["status"] == "fail"]
        assert any(check["name"] == "API key" for check in failing_checks)

    def test_doctor_json_reports_ready_state(self, tmp_path: Path) -> None:
        runner = CliRunner()
        (tmp_path / ".env").write_text("MISTRAL_API_KEY=test-key\n", encoding="utf-8")

        with patch.dict(os.environ, {"MISTRAL_API_KEY": "test-key"}, clear=True):
            result = runner.invoke(cli, ["doctor", "--project-root", str(tmp_path), "--json"])

        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert payload["ready"] is True
        assert any(
            check["name"] == "API key" and check["status"] == "pass" for check in payload["checks"]
        )

    def test_runs_list_show_and_cleanup(self) -> None:
        runner = CliRunner()

        with runner.isolated_filesystem():
            recorder = RunRecorder(
                command="process",
                project_root=Path.cwd(),
                input_path=Path("meeting.mp3"),
            )
            recorder.finish_cancelled()

            list_result = runner.invoke(cli, ["runs", "list"])
            show_result = runner.invoke(cli, ["runs", "show", recorder.trace_id, "--json"])
            with (
                patch("parler.cli.prune_run_summaries", return_value=2),
                patch("parler.cli.prune_managed_audio_files", return_value=3),
            ):
                cleanup_result = runner.invoke(cli, ["cleanup", "--json"])

        assert list_result.exit_code == 0
        assert recorder.trace_id in list_result.output

        assert show_result.exit_code == 0
        payload = json.loads(show_result.output)
        assert payload["trace_id"] == recorder.trace_id
        assert payload["status"] == "cancelled"

        assert cleanup_result.exit_code == 0
        cleanup_payload = json.loads(cleanup_result.output)
        assert cleanup_payload["removed_runs"] == 2
        assert cleanup_payload["removed_temp_audio"] == 3


class TestCliMain:
    def test_main_loads_dotenv_before_invoking_click(self, tmp_path: Path) -> None:
        env_file = tmp_path / ".env"
        env_file.write_text("MISTRAL_API_KEY=dotenv-key\n", encoding="utf-8")

        previous = dict(os.environ)
        try:
            os.environ.pop("MISTRAL_API_KEY", None)

            with (
                patch("pathlib.Path.cwd", return_value=tmp_path),
                patch("parler.cli.cli.main", return_value=None) as mock_click_main,
            ):
                main()

            assert os.environ["MISTRAL_API_KEY"] == "dotenv-key"
            mock_click_main.assert_called_once()
        finally:
            os.environ.clear()
            os.environ.update(previous)


class TestRunsSearch:
    def test_search_returns_all_when_no_filters(self) -> None:
        runner = CliRunner()

        with runner.isolated_filesystem():
            for cmd in ("process", "transcribe"):
                rec = RunRecorder(command=cmd, project_root=Path.cwd())
                rec.finish_cancelled()

            result = runner.invoke(cli, ["runs", "search"])

        assert result.exit_code == 0
        assert "process" in result.output
        assert "transcribe" in result.output

    def test_search_filter_by_status(self) -> None:
        runner = CliRunner()

        with runner.isolated_filesystem():
            rec1 = RunRecorder(command="process", project_root=Path.cwd())
            rec1.finish_cancelled()
            rec2 = RunRecorder(command="transcribe", project_root=Path.cwd())
            rec2.finish_failure(RuntimeError("boom"))

            result = runner.invoke(cli, ["runs", "search", "--status", "cancelled"])

        assert result.exit_code == 0
        assert rec1.trace_id in result.output
        assert "transcribe" not in result.output or "cancelled" in result.output

    def test_search_filter_by_command(self) -> None:
        runner = CliRunner()

        with runner.isolated_filesystem():
            rec_p = RunRecorder(command="process", project_root=Path.cwd())
            rec_p.finish_cancelled()
            rec_t = RunRecorder(command="transcribe", project_root=Path.cwd())
            rec_t.finish_cancelled()

            result = runner.invoke(cli, ["runs", "search", "--command", "process"])

        assert result.exit_code == 0
        assert rec_p.trace_id in result.output
        assert rec_t.trace_id not in result.output

    def test_search_filter_by_input_pattern(self) -> None:
        runner = CliRunner()

        with runner.isolated_filesystem():
            rec = RunRecorder(
                command="process",
                project_root=Path.cwd(),
                input_path=Path("/tmp/board-meeting.mp3"),
            )
            rec.finish_cancelled()
            rec2 = RunRecorder(
                command="process",
                project_root=Path.cwd(),
                input_path=Path("/tmp/team-standup.mp3"),
            )
            rec2.finish_cancelled()

            result = runner.invoke(cli, ["runs", "search", "--input", "board"])

        assert result.exit_code == 0
        assert rec.trace_id in result.output
        assert rec2.trace_id not in result.output

    def test_search_json_output(self) -> None:
        runner = CliRunner()

        with runner.isolated_filesystem():
            rec = RunRecorder(command="process", project_root=Path.cwd())
            rec.finish_cancelled()

            result = runner.invoke(cli, ["runs", "search", "--json"])

        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert isinstance(payload, list)
        assert any(item["trace_id"] == rec.trace_id for item in payload)

    def test_search_no_results_message(self) -> None:
        runner = CliRunner()

        with runner.isolated_filesystem():
            result = runner.invoke(cli, ["runs", "search", "--status", "completed"])

        assert result.exit_code == 0
        assert "No matching runs found." in result.output

    def test_search_no_header_omits_header_row(self) -> None:
        runner = CliRunner()

        with runner.isolated_filesystem():
            rec = RunRecorder(command="process", project_root=Path.cwd())
            rec.finish_cancelled()

            with_header = runner.invoke(cli, ["runs", "search"])
            without_header = runner.invoke(cli, ["runs", "search", "--no-header"])

        assert with_header.exit_code == 0
        assert without_header.exit_code == 0
        assert "trace_id\tcommand" in with_header.output
        assert "trace_id\tcommand" not in without_header.output
        assert rec.trace_id in with_header.output
        assert rec.trace_id in without_header.output

    def test_list_no_header_omits_header_row(self) -> None:
        runner = CliRunner()

        with runner.isolated_filesystem():
            rec = RunRecorder(command="process", project_root=Path.cwd())
            rec.finish_cancelled()

            with_header = runner.invoke(cli, ["runs", "list"])
            without_header = runner.invoke(cli, ["runs", "list", "--no-header"])

        assert with_header.exit_code == 0
        assert without_header.exit_code == 0
        assert "trace_id\tcommand" in with_header.output
        assert "trace_id\tcommand" not in without_header.output
        assert rec.trace_id in without_header.output


class TestRosterCommands:
    def _make_roster(self, tmp_path: Path):
        """Return a Roster instance backed by a temp file."""
        from parler.roster import Roster

        return Roster(path=tmp_path / "roster.json")

    def test_add_and_list(self, tmp_path: Path) -> None:
        runner = CliRunner()
        roster_path = tmp_path / "roster.json"

        with patch("parler.roster.Roster.DEFAULT_PATH", roster_path):
            add_result = runner.invoke(
                cli,
                ["roster", "add", "Alice", "--role", "PM", "--team", "Product"],
            )
            assert add_result.exit_code == 0
            assert "Added 'Alice' to roster." in add_result.output

            list_result = runner.invoke(cli, ["roster", "list"])
            assert list_result.exit_code == 0
            assert "Alice" in list_result.output
            assert "PM" in list_result.output

    def test_add_with_aliases(self, tmp_path: Path) -> None:
        runner = CliRunner()
        roster_path = tmp_path / "roster.json"

        with patch("parler.roster.Roster.DEFAULT_PATH", roster_path):
            result = runner.invoke(
                cli,
                ["roster", "add", "Bob", "--alias", "Robert", "--alias", "Rob"],
            )
            assert result.exit_code == 0

            show_result = runner.invoke(cli, ["roster", "show", "Bob"])
            assert show_result.exit_code == 0
            assert "Robert" in show_result.output

    def test_remove_existing(self, tmp_path: Path) -> None:
        runner = CliRunner()
        roster_path = tmp_path / "roster.json"

        from parler.roster import ParticipantEntry, Roster

        Roster(path=roster_path).add(ParticipantEntry(name="Charlie"))

        with patch("parler.roster.Roster.DEFAULT_PATH", roster_path):
            result = runner.invoke(cli, ["roster", "remove", "Charlie"])
            assert result.exit_code == 0
            assert "Removed 'Charlie' from roster." in result.output

    def test_remove_nonexistent(self, tmp_path: Path) -> None:
        runner = CliRunner()
        roster_path = tmp_path / "roster.json"

        with patch("parler.roster.Roster.DEFAULT_PATH", roster_path):
            result = runner.invoke(cli, ["roster", "remove", "NoOne"])
            assert result.exit_code == 1

    def test_list_empty(self, tmp_path: Path) -> None:
        runner = CliRunner()
        roster_path = tmp_path / "roster.json"

        with patch("parler.roster.Roster.DEFAULT_PATH", roster_path):
            result = runner.invoke(cli, ["roster", "list"])
            assert result.exit_code == 0
            assert "Roster is empty." in result.output

    def test_list_json(self, tmp_path: Path) -> None:
        runner = CliRunner()
        roster_path = tmp_path / "roster.json"

        from parler.roster import ParticipantEntry, Roster

        Roster(path=roster_path).add(ParticipantEntry(name="Diana", role="CTO"))

        with patch("parler.roster.Roster.DEFAULT_PATH", roster_path):
            result = runner.invoke(cli, ["roster", "list", "--json"])
            assert result.exit_code == 0
            payload = json.loads(result.output)
            assert isinstance(payload, list)
            assert payload[0]["name"] == "Diana"

    def test_show_not_found(self, tmp_path: Path) -> None:
        runner = CliRunner()
        roster_path = tmp_path / "roster.json"

        with patch("parler.roster.Roster.DEFAULT_PATH", roster_path):
            result = runner.invoke(cli, ["roster", "show", "Unknown"])
            assert result.exit_code != 0

    def test_list_no_header_omits_header_row(self, tmp_path: Path) -> None:
        runner = CliRunner()
        roster_path = tmp_path / "roster.json"

        with patch("parler.roster.Roster.DEFAULT_PATH", roster_path):
            runner.invoke(
                cli,
                ["roster", "add", "Alice", "--role", "PM", "--team", "Product"],
            )

            with_header = runner.invoke(cli, ["roster", "list"])
            without_header = runner.invoke(cli, ["roster", "list", "--no-header"])

        assert with_header.exit_code == 0
        assert without_header.exit_code == 0
        assert "name\trole\tteam" in with_header.output
        assert "name\trole\tteam" not in without_header.output
        assert "Alice" in without_header.output


class TestReviewCommand:
    def test_review_yes_flag_approves_without_prompt(self, tmp_path: Path) -> None:
        runner = CliRunner()
        state_path = tmp_path / ".parler-state.json"
        save_processing_state(state_path, make_state(decision_log=make_decision_log()))

        result = runner.invoke(
            cli,
            ["review", "--from-state", str(state_path), "--yes"],
        )

        assert result.exit_code == 0
        assert "Launch date" in result.output

    def test_review_no_decision_log_raises_error(self, tmp_path: Path) -> None:
        runner = CliRunner()
        state_path = tmp_path / ".parler-state.json"
        save_processing_state(state_path, make_state(transcript=make_transcript()))

        result = runner.invoke(cli, ["review", "--from-state", str(state_path), "--yes"])

        assert result.exit_code != 0

    def test_review_saves_updated_state(self, tmp_path: Path) -> None:
        runner = CliRunner()
        state_path = tmp_path / ".parler-state.json"
        save_processing_state(state_path, make_state(decision_log=make_decision_log()))

        result = runner.invoke(
            cli,
            ["review", "--from-state", str(state_path), "--yes"],
        )

        assert result.exit_code == 0
        saved = json.loads(state_path.read_text(encoding="utf-8"))
        assert "decision_log" in saved

    def test_review_interactive_delete(self, tmp_path: Path) -> None:
        runner = CliRunner()
        state_path = tmp_path / ".parler-state.json"
        save_processing_state(state_path, make_state(decision_log=make_decision_log()))

        result = runner.invoke(
            cli,
            ["review", "--from-state", str(state_path)],
            input="delete D1\n\n",
        )

        assert result.exit_code == 0
        assert "Deleted D1" in result.output
        saved = json.loads(state_path.read_text(encoding="utf-8"))
        decisions = saved["decision_log"]["decisions"]
        assert all(d["id"] != "D1" for d in decisions)

    def test_review_writes_output_file(self, tmp_path: Path) -> None:
        runner = CliRunner()
        state_path = tmp_path / ".parler-state.json"
        output_path = tmp_path / "reviewed.md"
        save_processing_state(state_path, make_state(decision_log=make_decision_log()))

        result = runner.invoke(
            cli,
            ["review", "--from-state", str(state_path), "--yes", "--output", str(output_path)],
        )

        assert result.exit_code == 0
        assert output_path.exists()
        assert "Launch date" in output_path.read_text(encoding="utf-8")


class TestSearchRunSummaries:
    def test_filter_by_status(self, tmp_path: Path) -> None:
        from parler.runlog import search_run_summaries

        rec1 = RunRecorder(command="process", project_root=tmp_path)
        rec1.finish_cancelled()
        rec2 = RunRecorder(command="transcribe", project_root=tmp_path)
        rec2.finish_failure(RuntimeError("err"))

        results = search_run_summaries(project_root=tmp_path, status="cancelled")
        assert len(results) == 1
        assert results[0]["trace_id"] == rec1.trace_id

    def test_filter_by_command(self, tmp_path: Path) -> None:
        from parler.runlog import search_run_summaries

        rec1 = RunRecorder(command="process", project_root=tmp_path)
        rec1.finish_cancelled()
        rec2 = RunRecorder(command="transcribe", project_root=tmp_path)
        rec2.finish_cancelled()

        results = search_run_summaries(project_root=tmp_path, command="process")
        assert len(results) == 1
        assert results[0]["command"] == "process"

    def test_filter_by_input_pattern(self, tmp_path: Path) -> None:
        from parler.runlog import search_run_summaries

        rec1 = RunRecorder(
            command="process",
            project_root=tmp_path,
            input_path=Path("/recordings/board-meeting.mp3"),
        )
        rec1.finish_cancelled()
        rec2 = RunRecorder(
            command="process",
            project_root=tmp_path,
            input_path=Path("/recordings/standup.mp3"),
        )
        rec2.finish_cancelled()

        results = search_run_summaries(project_root=tmp_path, input_pattern="board")
        assert len(results) == 1
        assert rec1.trace_id == results[0]["trace_id"]

    def test_limit_is_respected(self, tmp_path: Path) -> None:
        from parler.runlog import search_run_summaries

        for _ in range(5):
            rec = RunRecorder(command="process", project_root=tmp_path)
            rec.finish_cancelled()

        results = search_run_summaries(project_root=tmp_path, limit=3)
        assert len(results) == 3

    def test_empty_directory_returns_empty(self, tmp_path: Path) -> None:
        from parler.runlog import search_run_summaries

        results = search_run_summaries(project_root=tmp_path)
        assert results == []


class TestRosterModule:
    def test_add_and_find(self, tmp_path: Path) -> None:
        from parler.roster import ParticipantEntry, Roster

        roster = Roster(path=tmp_path / "roster.json")
        roster.add(ParticipantEntry(name="Eve", role="Dev"))
        assert roster.find("Eve") is not None
        assert roster.find("eve") is not None  # case-insensitive

    def test_find_by_alias(self, tmp_path: Path) -> None:
        from parler.roster import ParticipantEntry, Roster

        roster = Roster(path=tmp_path / "roster.json")
        roster.add(ParticipantEntry(name="Frank", aliases=["Frankie", "F"]))
        assert roster.find("Frankie") is not None
        assert roster.find("f") is not None

    def test_remove_returns_true(self, tmp_path: Path) -> None:
        from parler.roster import ParticipantEntry, Roster

        roster = Roster(path=tmp_path / "roster.json")
        roster.add(ParticipantEntry(name="Grace"))
        assert roster.remove("Grace") is True
        assert roster.find("Grace") is None

    def test_remove_missing_returns_false(self, tmp_path: Path) -> None:
        from parler.roster import Roster

        roster = Roster(path=tmp_path / "roster.json")
        assert roster.remove("Nobody") is False

    def test_all_names_includes_aliases(self, tmp_path: Path) -> None:
        from parler.roster import ParticipantEntry, Roster

        roster = Roster(path=tmp_path / "roster.json")
        roster.add(ParticipantEntry(name="Hank", aliases=["Henry"]))
        names = roster.all_names()
        assert "Hank" in names
        assert "Henry" in names

    def test_persists_across_instances(self, tmp_path: Path) -> None:
        from parler.roster import ParticipantEntry, Roster

        path = tmp_path / "roster.json"
        Roster(path=path).add(ParticipantEntry(name="Iris"))

        loaded = Roster(path=path)
        assert loaded.find("Iris") is not None

    def test_duplicate_add_replaces_entry(self, tmp_path: Path) -> None:
        from parler.roster import ParticipantEntry, Roster

        path = tmp_path / "roster.json"
        roster = Roster(path=path)
        roster.add(ParticipantEntry(name="Jake", role="Dev"))
        roster.add(ParticipantEntry(name="Jake", role="PM"))

        entries = roster.all_entries()
        assert len(entries) == 1
        assert entries[0].role == "PM"


class TestCompletionCommand:
    """Tests for `parler completion <shell>` (issue #15)."""

    def test_bash_completion_emits_non_empty_output(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["completion", "bash"])
        assert result.exit_code == 0, result.output
        assert "_parler_completion" in result.output
        assert "_PARLER_COMPLETE" in result.output

    def test_zsh_completion_emits_zsh_specific_function(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["completion", "zsh"])
        assert result.exit_code == 0, result.output
        assert "_PARLER_COMPLETE" in result.output
        # zsh helper looks like _parler_completion()
        assert "compdef" in result.output or "_parler_completion" in result.output

    def test_fish_completion_emits_fish_block(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["completion", "fish"])
        assert result.exit_code == 0, result.output
        assert "complete" in result.output
        assert "_PARLER_COMPLETE" in result.output

    def test_unknown_shell_exits_with_usage_error(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["completion", "tcsh"])
        # click.Choice raises UsageError -> exit 2
        assert result.exit_code == 2
        assert "Invalid value" in result.output or "Usage" in result.output
