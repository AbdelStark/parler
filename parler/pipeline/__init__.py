"""Pipeline entry points."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from ..config import (
    AttributionConfig,
    CacheConfig,
    ChunkingConfig,
    CostConfig,
    ExtractionConfig,
    OutputConfig,
    ParlerConfig,
    TranscriptionConfig,
)
from .orchestrator import PipelineOrchestrator
from .state import PipelineStage, ProcessingState


def _env_default(name: str, fallback: str) -> str:
    return os.environ.get(name, fallback)


@dataclass(frozen=True)
class PipelineConfig:
    """Legacy flat config surface kept for E2E and script compatibility."""

    api_key: str
    languages: list[str] = field(default_factory=list)
    participants: list[str] = field(default_factory=list)
    meeting_date: date | None = None
    output_format: str = "markdown"
    output_path: Path | None = None
    anonymize_speakers: bool = False
    cache_dir: Path = Path(".parler-cache")
    cache_enabled: bool = True
    cache_ttl_days: int = 30
    max_chunk_s: int = 600
    overlap_s: int = 30
    confirm_above_usd: float = 1.0
    max_usd: float = 10.0
    transcription_model: str = field(
        default_factory=lambda: _env_default(
            "PARLER_E2E_TRANSCRIPTION_MODEL", "voxtral-mini-latest"
        )
    )
    transcription_timeout_s: int = 300
    transcription_max_retries: int = 3
    extraction_model: str = field(
        default_factory=lambda: _env_default("PARLER_E2E_EXTRACTION_MODEL", "mistral-large-latest")
    )
    extraction_temperature: float = 0.0
    extraction_max_tokens: int = 4096
    extraction_prompt_version: str = "v1.0"
    extraction_multi_pass_threshold: int = 25_000
    attribution_enabled: bool = True
    attribution_confidence_threshold: float = 0.7
    attribution_model: str = field(
        default_factory=lambda: _env_default("PARLER_E2E_ATTRIBUTION_MODEL", "mistral-large-latest")
    )

    def to_parler_config(self) -> ParlerConfig:
        return ParlerConfig(
            api_key=self.api_key,
            transcription=TranscriptionConfig(
                model=self.transcription_model,
                languages=list(self.languages),
                timeout_s=self.transcription_timeout_s,
                max_retries=self.transcription_max_retries,
            ),
            chunking=ChunkingConfig(
                max_chunk_s=self.max_chunk_s,
                overlap_s=self.overlap_s,
            ),
            attribution=AttributionConfig(
                enabled=self.attribution_enabled,
                confidence_threshold=self.attribution_confidence_threshold,
                model=self.attribution_model,
            ),
            extraction=ExtractionConfig(
                model=self.extraction_model,
                temperature=self.extraction_temperature,
                max_tokens=self.extraction_max_tokens,
                prompt_version=self.extraction_prompt_version,
                multi_pass_threshold=self.extraction_multi_pass_threshold,
            ),
            cache=CacheConfig(
                enabled=self.cache_enabled,
                directory=self.cache_dir,
                ttl_days=self.cache_ttl_days,
            ),
            output=OutputConfig(
                format=self.output_format,
                output_path=self.output_path,
                anonymize_speakers=self.anonymize_speakers,
            ),
            cost=CostConfig(
                max_usd=self.max_usd,
                confirm_above_usd=self.confirm_above_usd,
            ),
            participants=list(self.participants),
            meeting_date=self.meeting_date,
        )


def run_pipeline(
    input_path: str | Path, config: PipelineConfig | ParlerConfig
) -> ProcessingState | None:
    parler_config = config.to_parler_config() if isinstance(config, PipelineConfig) else config
    orchestrator = PipelineOrchestrator(parler_config)
    return orchestrator.run(input_path)


__all__ = [
    "PipelineConfig",
    "PipelineOrchestrator",
    "PipelineStage",
    "ProcessingState",
    "run_pipeline",
]
