"""Focused tests for the Textual TUI surface."""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner
from parler.cli import cli
from parler.tui import FIXTURE_PRESETS, ParlerTUIApp, PipelineRequest, build_tui_config
from textual.containers import Grid, Horizontal
from textual.widgets import Button, Input, Select, Static, Switch

REPO_ROOT = Path(__file__).resolve().parents[2]


class TestBuildTuiConfig:
    def test_build_tui_config_applies_visible_form_overrides(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        monkeypatch.setenv("MISTRAL_API_KEY", "test-api-key")
        request = PipelineRequest(
            input_path=tmp_path / "meeting.mp3",
            config_path=None,
            output_path=tmp_path / "report.html",
            checkpoint_path=tmp_path / ".parler-state.json",
            meeting_date=date(2026, 4, 9),
            languages=("fr", "en"),
            participants=("Pierre", "Sophie"),
            output_format="html",
            cache_dir=tmp_path / ".cache",
            transcription_model="voxtral-mini-latest",
            extraction_model="mistral-medium-latest",
            transcribe_only=False,
            no_diarize=False,
            anonymize_speakers=True,
            resume=False,
        )

        config = build_tui_config(request)

        assert config.api_key == "test-api-key"
        assert config.transcription.languages == ["fr", "en"]
        assert config.output.format == "html"
        assert config.output.output_path == tmp_path / "report.html"
        assert config.output.anonymize_speakers is True
        assert config.cache.directory == tmp_path / ".cache"
        assert config.participants == ["Pierre", "Sophie"]
        assert config.meeting_date == date(2026, 4, 9)


@pytest.mark.asyncio
class TestParlerTuiApp:
    async def test_startup_preloads_default_synthetic_french_showcase(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("MISTRAL_API_KEY", "test-api-key")
        app = ParlerTUIApp(project_root=REPO_ROOT)

        async with app.run_test() as pilot:
            await pilot.pause()

            assert app.query_one("#input-path", Input).value == str(FIXTURE_PRESETS["fr"].path)
            assert app.query_one("#languages-input", Input).value == "fr"
            assert app.query_one("#participants-input", Input).value == "Pierre, Sophie"
            assert app.query_one("#voxpopuli-select", Select).value == "voxpopuli_01"
            assert app.query_one("#transcribe-only-switch", Switch).value is False
            assert "Ready to run." in str(app.query_one("#run-summary", Static).render())
            assert app.focused is app.query_one("#run-button", Button)

    async def test_fixture_action_populates_showcase_fields(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("MISTRAL_API_KEY", "test-api-key")
        app = ParlerTUIApp(project_root=REPO_ROOT)

        with patch("parler.tui.app._preview_text", return_value="fixture preview"):
            async with app.run_test() as pilot:
                app.action_load_french_demo()
                await pilot.pause()

                assert app.title == "parler 🇫🇷"
                assert "🇫🇷" in str(app.query_one("#topbar-brand", Static).render())
                assert app.query_one("#input-path", Input).value == str(FIXTURE_PRESETS["fr"].path)
                assert (
                    app.query_one("#meeting-date", Input).value
                    == FIXTURE_PRESETS["fr"].meeting_date.isoformat()
                )
                assert app.query_one("#languages-input", Input).value == "fr"
                assert app.query_one("#participants-input", Input).value == "Pierre, Sophie"
                assert app.query_one("#output-format-select", Select).value == "markdown"
                assert app.query_one("#transcribe-only-switch", Switch).value is False

    async def test_selected_voxpopuli_clip_populates_showcase_fields(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("MISTRAL_API_KEY", "test-api-key")
        app = ParlerTUIApp(project_root=REPO_ROOT)

        async with app.run_test() as pilot:
            app.query_one("#voxpopuli-select", Select).value = "voxpopuli_03"
            app.action_load_selected_voxpopuli_demo()
            await pilot.pause()

            assert app.query_one("#input-path", Input).value == str(
                FIXTURE_PRESETS["voxpopuli_03"].path
            )
            assert (
                app.query_one("#meeting-date", Input).value
                == FIXTURE_PRESETS["voxpopuli_03"].meeting_date.isoformat()
            )
            assert app.query_one("#transcribe-only-switch", Switch).value is True

    async def test_layout_switches_to_stack_mode_for_medium_terminal(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("MISTRAL_API_KEY", "test-api-key")
        app = ParlerTUIApp(project_root=REPO_ROOT)

        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()

            assert app.query_one("#shell", Horizontal).has_class("stack")
            assert app.query_one("#studio-layout", Horizontal).has_class("stack")
            assert app.query_one("#stage-grid", Grid).has_class("dense")

    async def test_topbar_does_not_consume_terminal_height(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("MISTRAL_API_KEY", "test-api-key")
        app = ParlerTUIApp(project_root=REPO_ROOT)

        async with app.run_test(size=(160, 48)) as pilot:
            await pilot.pause()

            assert app.query_one("#topbar", Horizontal).region.height <= 3
            assert app.query_one("#shell", Horizontal).region.height >= 40


class TestTuiCliCommand:
    def test_tui_command_launches_textual_app(self, tmp_path: Path) -> None:
        runner = CliRunner()
        captured: dict[str, str] = {}
        (tmp_path / ".env").write_text("MISTRAL_API_KEY=file-key\n", encoding="utf-8")

        def fake_run(self: ParlerTUIApp) -> None:
            captured["project_root"] = str(self.project_root)
            captured["api_key"] = os.environ.get("MISTRAL_API_KEY", "")

        with (
            patch.dict(os.environ, {}, clear=True),
            patch("parler.tui.ParlerTUIApp.run", new=fake_run),
        ):
            result = runner.invoke(cli, ["tui", "--project-root", str(tmp_path)])

        assert result.exit_code == 0
        assert captured["project_root"] == str(tmp_path.resolve())
        assert captured["api_key"] == "file-key"
