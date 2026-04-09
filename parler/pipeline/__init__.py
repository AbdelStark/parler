"""Pipeline entry points."""

from __future__ import annotations

from pathlib import Path

from ..config import ParlerConfig
from .orchestrator import PipelineOrchestrator
from .state import PipelineStage, ProcessingState

PipelineConfig = ParlerConfig


def run_pipeline(input_path: str | Path, config: PipelineConfig) -> ProcessingState | None:
    orchestrator = PipelineOrchestrator(config)
    return orchestrator.run(input_path)


__all__ = [
    "PipelineConfig",
    "PipelineOrchestrator",
    "PipelineStage",
    "ProcessingState",
    "run_pipeline",
]
