"""Pipeline entry points."""

from __future__ import annotations

from ..config import ParlerConfig
from .orchestrator import PipelineOrchestrator, PipelineStage, ProcessingState

PipelineConfig = ParlerConfig


def run_pipeline(input_path, config: PipelineConfig):
    orchestrator = PipelineOrchestrator(config)
    return orchestrator.run(input_path)


__all__ = ["PipelineConfig", "PipelineOrchestrator", "PipelineStage", "ProcessingState", "run_pipeline"]
