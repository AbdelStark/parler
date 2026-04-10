"""Compatibility tests for the legacy flat PipelineConfig surface."""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path
from unittest.mock import patch

from parler.config import ParlerConfig
from parler.pipeline import PipelineConfig, run_pipeline


class TestPipelineConfigCompatibility:
    def test_flat_pipeline_config_normalizes_to_nested_parler_config(self, tmp_path: Path) -> None:
        config = PipelineConfig(
            api_key="test-key",
            languages=["fr", "en"],
            participants=["Pierre", "Sophie"],
            meeting_date=date(2026, 4, 9),
            output_format="json",
            output_path=tmp_path / "report.json",
            anonymize_speakers=True,
            cache_dir=tmp_path / ".cache",
            cache_enabled=True,
            cache_ttl_days=14,
            max_chunk_s=900,
            overlap_s=45,
            confirm_above_usd=2.5,
            max_usd=20.0,
            extraction_prompt_version="v1.2.0",
        )

        normalized = config.to_parler_config()

        assert isinstance(normalized, ParlerConfig)
        assert normalized.transcription.languages == ["fr", "en"]
        assert normalized.participants == ["Pierre", "Sophie"]
        assert normalized.meeting_date == date(2026, 4, 9)
        assert normalized.output.format == "json"
        assert normalized.output.output_path == tmp_path / "report.json"
        assert normalized.output.anonymize_speakers is True
        assert normalized.cache.directory == tmp_path / ".cache"
        assert normalized.cache.ttl_days == 14
        assert normalized.chunking.max_chunk_s == 900
        assert normalized.chunking.overlap_s == 45
        assert normalized.cost.confirm_above_usd == 2.5
        assert normalized.cost.max_usd == 20.0
        assert normalized.extraction.prompt_version == "v1.2.0"

    def test_run_pipeline_accepts_legacy_pipeline_config(self, tmp_path: Path) -> None:
        config = PipelineConfig(
            api_key="test-key",
            languages=["fr"],
            cache_dir=tmp_path / ".cache",
        )

        with patch("parler.pipeline.PipelineOrchestrator") as mock_orchestrator:
            run_pipeline("meeting.mp3", config)

        called_config = mock_orchestrator.call_args.args[0]
        assert isinstance(called_config, ParlerConfig)
        assert called_config.transcription.languages == ["fr"]
        assert called_config.cache.directory == tmp_path / ".cache"

    def test_env_override_can_supply_e2e_extraction_model(self) -> None:
        with patch.dict(os.environ, {"PARLER_E2E_EXTRACTION_MODEL": "mistral-medium-latest"}):
            config = PipelineConfig(api_key="test-key")

        normalized = config.to_parler_config()

        assert normalized.extraction.model == "mistral-medium-latest"

    def test_explicit_extraction_model_overrides_env_default(self) -> None:
        with patch.dict(os.environ, {"PARLER_E2E_EXTRACTION_MODEL": "mistral-medium-latest"}):
            config = PipelineConfig(
                api_key="test-key",
                extraction_model="mistral-large-latest",
            )

        normalized = config.to_parler_config()

        assert normalized.extraction.model == "mistral-large-latest"
