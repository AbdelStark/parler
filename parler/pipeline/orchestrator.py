"""Minimal orchestrator and checkpoint/state surface."""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar

from ..attribution.attributor import SpeakerAttributor
from ..audio.ingester import AudioIngester
from ..config import ParlerConfig
from ..errors import ProcessingError
from ..extraction.cache import ExtractionCache
from ..extraction.extractor import DecisionExtractor
from ..models import AudioFile
from ..rendering.renderer import OutputFormat, RenderConfig, ReportRenderer
from ..transcription.cache import TranscriptCache
from ..transcription.transcriber import VoxtralTranscriber
from .state import PipelineStage, ProcessingState, load_processing_state, save_processing_state

_TRANSCRIPTION_PRICING_PER_MINUTE = {
    "voxtral-mini": 0.003,
    "voxtral-small": 0.006,
}
_TEXT_MODEL_PRICING_PER_MILLION = {
    "mistral-medium": (0.4, 2.0),
    "mistral-large": (0.5, 1.5),
}
_DEFAULT_TRANSCRIPTION_PRICE_PER_MINUTE = 0.006
_DEFAULT_TEXT_INPUT_PRICE_PER_MILLION = 0.5
_DEFAULT_TEXT_OUTPUT_PRICE_PER_MILLION = 2.0
_CONSERVATIVE_TRANSCRIPT_INPUT_TOKENS_PER_MINUTE = 260
_CONSERVATIVE_EXTRACTION_OUTPUT_TOKENS_PER_PASS = 900
_CONSERVATIVE_MULTI_PASS_OVERLAP_TOKENS = 256

T = TypeVar("T")


def _match_pricing(model: str, table: dict[str, T], default: T) -> T:
    normalized = model.lower()
    for prefix, price in table.items():
        if normalized.startswith(prefix):
            return price
    return default


def estimate_cost(audio_file: AudioFile, config: ParlerConfig) -> float:
    minutes = max(audio_file.duration_s / 60.0, 0.0)
    transcription_price_per_minute = _match_pricing(
        config.transcription.model,
        _TRANSCRIPTION_PRICING_PER_MINUTE,
        _DEFAULT_TRANSCRIPTION_PRICE_PER_MINUTE,
    )
    transcription_cost = minutes * transcription_price_per_minute

    estimated_input_tokens = max(
        math.ceil(minutes * _CONSERVATIVE_TRANSCRIPT_INPUT_TOKENS_PER_MINUTE),
        1,
    )
    pass_count = max(
        1,
        math.ceil(estimated_input_tokens / max(config.extraction.multi_pass_threshold, 1)),
    )
    overlap_tokens = max(pass_count - 1, 0) * _CONSERVATIVE_MULTI_PASS_OVERLAP_TOKENS
    priced_input_tokens = estimated_input_tokens + overlap_tokens
    priced_output_tokens = pass_count * min(
        config.extraction.max_tokens,
        _CONSERVATIVE_EXTRACTION_OUTPUT_TOKENS_PER_PASS,
    )
    input_price_per_million, output_price_per_million = _match_pricing(
        config.extraction.model,
        _TEXT_MODEL_PRICING_PER_MILLION,
        (_DEFAULT_TEXT_INPUT_PRICE_PER_MILLION, _DEFAULT_TEXT_OUTPUT_PRICE_PER_MILLION),
    )
    extraction_cost = (priced_input_tokens / 1_000_000) * input_price_per_million + (
        priced_output_tokens / 1_000_000
    ) * output_price_per_million
    return round(transcription_cost + extraction_cost, 4)


class PipelineOrchestrator:
    def __init__(self, config: ParlerConfig):
        self.config = config

    def _run_stage(
        self,
        stage: PipelineStage,
        callback: Callable[[], Any],
        *,
        on_stage_start: Callable[[PipelineStage], None] | None = None,
        on_stage_complete: Callable[[PipelineStage, float], None] | None = None,
    ) -> Any:
        if on_stage_start is not None:
            on_stage_start(stage)
        start = time.perf_counter()
        result = callback()
        duration = time.perf_counter() - start
        if on_stage_complete is not None:
            on_stage_complete(stage, duration)
        return result

    def _save_checkpoint(self, state: ProcessingState) -> None:
        if state.checkpoint_path is None:
            return
        save_processing_state(state.checkpoint_path, state)

    def run(
        self,
        input_path: str | Path,
        *,
        transcribe_only: bool = False,
        no_diarize: bool = False,
        checkpoint_path: Path | None = None,
        resume: bool = False,
        on_cost_confirm: Callable[[float], bool] | None = None,
        on_stage_start: Callable[[PipelineStage], None] | None = None,
        on_stage_complete: Callable[[PipelineStage, float], None] | None = None,
    ) -> ProcessingState | None:
        input_file = Path(input_path)
        checkpoint = checkpoint_path
        if checkpoint is None and resume:
            checkpoint = Path(".parler-state.json")

        ingester = AudioIngester()
        audio_file = self._run_stage(
            PipelineStage.INGEST,
            lambda: ingester.ingest(input_file),
            on_stage_start=on_stage_start,
            on_stage_complete=on_stage_complete,
        )
        state = ProcessingState(
            audio_file=audio_file,
            transcript=None,
            attributed_transcript=None,
            decision_log=None,
            report=None,
            completed_stages=frozenset(),
            checkpoint_path=checkpoint,
        ).with_audio_file(audio_file)

        if resume and checkpoint is not None and checkpoint.exists():
            state = load_processing_state(
                checkpoint,
                audio_file=audio_file,
                expected_audio_hash=audio_file.content_hash,
            )

        if PipelineStage.TRANSCRIBE not in state.completed_stages:
            estimated_cost = estimate_cost(audio_file, self.config)
            if estimated_cost > self.config.cost.max_usd:
                raise ProcessingError(
                    "Estimated API cost "
                    f"${estimated_cost:.2f} exceeds configured cap "
                    f"${self.config.cost.max_usd:.2f}"
                )
            if (
                estimated_cost > self.config.cost.confirm_above_usd
                and on_cost_confirm is not None
                and not on_cost_confirm(estimated_cost)
            ):
                return None

            transcript_cache = None
            if self.config.cache.enabled:
                transcript_cache = TranscriptCache(
                    cache_dir=self.config.cache.directory,
                    ttl_days=self.config.cache.ttl_days,
                )

            transcriber = VoxtralTranscriber(
                api_key=self.config.api_key,
                model=self.config.transcription.model,
                max_chunk_s=self.config.chunking.max_chunk_s,
                max_retries=self.config.transcription.max_retries,
                cache=transcript_cache,
                timeout_ms=self.config.transcription.timeout_s * 1000,
            )
            transcript = self._run_stage(
                PipelineStage.TRANSCRIBE,
                lambda: transcriber.transcribe(
                    audio_file,
                    languages=self.config.transcription.languages or None,
                ),
                on_stage_start=on_stage_start,
                on_stage_complete=on_stage_complete,
            )
            state = state.with_transcript(transcript)
            self._save_checkpoint(state)

        if transcribe_only:
            return state

        working_transcript = state.attributed_transcript or state.transcript
        if not no_diarize and PipelineStage.ATTRIBUTE not in state.completed_stages:
            assert state.transcript is not None
            transcript_for_attribution = state.transcript
            attributor = SpeakerAttributor()
            try:
                attributed = self._run_stage(
                    PipelineStage.ATTRIBUTE,
                    lambda: attributor.attribute(
                        transcript_for_attribution,
                        participants=self.config.participants,
                        anonymize=self.config.output.anonymize_speakers,
                    ),
                    on_stage_start=on_stage_start,
                    on_stage_complete=on_stage_complete,
                )
                state = state.with_attributed_transcript(attributed)
                working_transcript = attributed
            except ProcessingError:
                working_transcript = state.transcript

        if PipelineStage.EXTRACT not in state.completed_stages:
            assert working_transcript is not None
            extraction_cache = None
            if self.config.cache.enabled:
                extraction_cache = ExtractionCache(cache_dir=self.config.cache.directory)
            extractor = DecisionExtractor(
                api_key=self.config.api_key,
                model=self.config.extraction.model,
                prompt_version=self.config.extraction.prompt_version,
                temperature=self.config.extraction.temperature,
                max_tokens=self.config.extraction.max_tokens,
                multi_pass_threshold=self.config.extraction.multi_pass_threshold,
                cache=extraction_cache,
            )
            decision_log = self._run_stage(
                PipelineStage.EXTRACT,
                lambda: extractor.extract(
                    working_transcript,
                    meeting_date=self.config.meeting_date,
                    participants=self.config.participants,
                ),
                on_stage_start=on_stage_start,
                on_stage_complete=on_stage_complete,
            )
            state = state.with_decision_log(decision_log)
            self._save_checkpoint(state)

        if PipelineStage.RENDER not in state.completed_stages:
            assert state.decision_log is not None
            decision_log_for_render = state.decision_log
            renderer = ReportRenderer()
            report = self._run_stage(
                PipelineStage.RENDER,
                lambda: renderer.render(
                    decision_log_for_render,
                    RenderConfig(format=OutputFormat(self.config.output.format)),
                ),
                on_stage_start=on_stage_start,
                on_stage_complete=on_stage_complete,
            )
            state = state.with_report(report)

        return state
