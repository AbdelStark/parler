"""
TDD specification: PipelineOrchestrator state machine

The orchestrator is the spine of parler. It coordinates the five pipeline
stages and manages:
  - Stage skipping (cache hits, --transcribe-only, --no-diarize)
  - Checkpoint saving and resumption (ProcessingState → .parler-state.json)
  - Cost tracking and confirmation gate
  - Progress reporting callbacks
  - Stage failure isolation (non-fatal stages continue; fatal stages abort)

Design contract:
  - Stages run in order: ingest → transcribe → attribute → extract → render
  - Each stage receives the output of the prior stage via ProcessingState
  - ProcessingState is an immutable value object; each stage produces a new one
  - Cache hits short-circuit transcription and/or extraction
  - Cost guard fires before the first billable API call if estimate > confirm_above_usd
  - Checkpoint is written after each billable stage completes
  - On resume, completed stages in the checkpoint are replayed from disk
"""

import json
from datetime import date
from pathlib import Path
from unittest.mock import patch

import pytest
from parler.errors import APIError, ProcessingError
from parler.models import AudioFile, DecisionLog, Transcript, TranscriptSegment
from parler.pipeline.orchestrator import (
    PipelineOrchestrator,
    PipelineStage,
    ProcessingState,
    estimate_cost,
)

# ─── Fixtures ────────────────────────────────────────────────────────────────


def make_audio_file(path="/tmp/meeting.mp3", duration=600.0, content_hash="abc123abc123"):
    return AudioFile(
        path=Path(path),
        original_path=None,
        format="mp3",
        duration_s=duration,
        sample_rate=44100,
        channels=2,
        size_bytes=10_000_000,
        content_hash=content_hash,
    )


def make_segment(id=0, text="Test."):
    return TranscriptSegment(
        id=id,
        start_s=float(id * 5),
        end_s=float((id + 1) * 5),
        text=text,
        language="fr",
        speaker_id=None,
        speaker_confidence=None,
        confidence=0.9,
        no_speech_prob=0.01,
        code_switch=False,
        words=None,
    )


def make_transcript(segments=None):
    segs = segments or [make_segment(0, "Test.")]
    return Transcript(
        text=" ".join(s.text for s in segs),
        language="fr",
        duration_s=segs[-1].end_s,
        segments=tuple(segs),
    )


def make_empty_log():
    from parler.models import ExtractionMetadata

    return DecisionLog(
        decisions=(),
        commitments=(),
        rejected=(),
        open_questions=(),
        metadata=ExtractionMetadata(
            model="mistral-large-latest",
            prompt_version="v1.0",
            meeting_date=date(2026, 4, 9),
            extracted_at="2026-04-09T10:00:00Z",
            input_tokens=100,
            output_tokens=20,
        ),
    )


# ─── Stage sequencing ────────────────────────────────────────────────────────


class TestStageSequencing:
    def test_stages_run_in_order(self, parler_config):
        """Stages must always execute in: ingest → transcribe → attribute → extract → render."""
        stage_order = []

        with (
            patch("parler.pipeline.orchestrator.AudioIngester") as MockIngester,
            patch("parler.pipeline.orchestrator.VoxtralTranscriber") as MockTranscriber,
            patch("parler.pipeline.orchestrator.SpeakerAttributor") as MockAttributor,
            patch("parler.pipeline.orchestrator.DecisionExtractor") as MockExtractor,
            patch("parler.pipeline.orchestrator.ReportRenderer") as MockRenderer,
        ):
            MockIngester.return_value.ingest.side_effect = lambda *a, **k: (
                stage_order.append("ingest") or make_audio_file()
            )
            MockTranscriber.return_value.transcribe.side_effect = lambda *a, **k: (
                stage_order.append("transcribe") or make_transcript()
            )
            MockAttributor.return_value.attribute.side_effect = lambda *a, **k: (
                stage_order.append("attribute") or make_transcript()
            )
            MockExtractor.return_value.extract.side_effect = lambda *a, **k: (
                stage_order.append("extract") or make_empty_log()
            )
            MockRenderer.return_value.render.side_effect = lambda *a, **k: (
                stage_order.append("render") or "# Report"
            )

            orchestrator = PipelineOrchestrator(parler_config)
            orchestrator.run(Path("/tmp/meeting.mp3"))

        assert stage_order == ["ingest", "transcribe", "attribute", "extract", "render"], (
            f"Stage order was: {stage_order}"
        )

    def test_transcribe_only_skips_attribute_extract_render(self, parler_config):
        """With transcribe_only=True, only ingest + transcribe run."""
        with (
            patch("parler.pipeline.orchestrator.AudioIngester") as MockIngester,
            patch("parler.pipeline.orchestrator.VoxtralTranscriber") as MockTranscriber,
            patch("parler.pipeline.orchestrator.DecisionExtractor") as MockExtractor,
        ):
            MockIngester.return_value.ingest.return_value = make_audio_file()
            MockTranscriber.return_value.transcribe.return_value = make_transcript()

            orchestrator = PipelineOrchestrator(parler_config)
            orchestrator.run(Path("/tmp/meeting.mp3"), transcribe_only=True)

        MockExtractor.return_value.extract.assert_not_called()

    def test_no_diarize_skips_attribution_stage(self, parler_config):
        """With no_diarize=True, attribution stage is skipped."""
        with (
            patch("parler.pipeline.orchestrator.AudioIngester") as MockIngester,
            patch("parler.pipeline.orchestrator.VoxtralTranscriber") as MockTranscriber,
            patch("parler.pipeline.orchestrator.SpeakerAttributor") as MockAttributor,
            patch("parler.pipeline.orchestrator.DecisionExtractor") as MockExtractor,
            patch("parler.pipeline.orchestrator.ReportRenderer") as MockRenderer,
        ):
            MockIngester.return_value.ingest.return_value = make_audio_file()
            MockTranscriber.return_value.transcribe.return_value = make_transcript()
            MockExtractor.return_value.extract.return_value = make_empty_log()
            MockRenderer.return_value.render.return_value = "# Report"

            orchestrator = PipelineOrchestrator(parler_config)
            orchestrator.run(Path("/tmp/meeting.mp3"), no_diarize=True)

        MockAttributor.return_value.attribute.assert_not_called()


# ─── ProcessingState immutability ────────────────────────────────────────────


class TestProcessingState:
    def test_processing_state_is_immutable(self):
        """ProcessingState is a frozen dataclass — mutation raises."""
        audio = make_audio_file()
        state = ProcessingState(
            audio_file=audio,
            transcript=None,
            attributed_transcript=None,
            decision_log=None,
            report=None,
            completed_stages=frozenset(),
            checkpoint_path=None,
        )
        with pytest.raises((AttributeError, TypeError)):
            state.transcript = make_transcript()

    def test_state_transitions_produce_new_objects(self):
        """Each stage transition creates a new ProcessingState, never mutates the prior."""
        audio = make_audio_file()
        state1 = ProcessingState(
            audio_file=audio,
            transcript=None,
            attributed_transcript=None,
            decision_log=None,
            report=None,
            completed_stages=frozenset(),
            checkpoint_path=None,
        )
        transcript = make_transcript()
        state2 = state1.with_transcript(transcript)

        assert id(state1) != id(state2)
        assert state1.transcript is None
        assert state2.transcript is transcript

    def test_completed_stages_accumulate_correctly(self):
        """completed_stages frozenset grows as stages complete."""
        audio = make_audio_file()
        state = ProcessingState(
            audio_file=audio,
            transcript=None,
            attributed_transcript=None,
            decision_log=None,
            report=None,
            completed_stages=frozenset(),
            checkpoint_path=None,
        )
        state2 = state.with_transcript(make_transcript())
        assert PipelineStage.TRANSCRIBE in state2.completed_stages
        assert PipelineStage.ATTRIBUTE not in state2.completed_stages


# ─── Checkpoint save and resume ───────────────────────────────────────────────


class TestCheckpointSaveResume:
    def test_checkpoint_written_after_transcription(self, parler_config, tmp_path):
        """A .parler-state.json checkpoint is written after transcription completes."""
        checkpoint_path = tmp_path / ".parler-state.json"

        with (
            patch("parler.pipeline.orchestrator.AudioIngester") as MockIngester,
            patch("parler.pipeline.orchestrator.VoxtralTranscriber") as MockTranscriber,
            patch("parler.pipeline.orchestrator.SpeakerAttributor") as MockAttributor,
            patch("parler.pipeline.orchestrator.DecisionExtractor") as MockExtractor,
            patch("parler.pipeline.orchestrator.ReportRenderer") as MockRenderer,
        ):
            MockIngester.return_value.ingest.return_value = make_audio_file()
            MockTranscriber.return_value.transcribe.return_value = make_transcript()
            MockAttributor.return_value.attribute.return_value = make_transcript()
            MockExtractor.return_value.extract.return_value = make_empty_log()
            MockRenderer.return_value.render.return_value = "# Report"

            orchestrator = PipelineOrchestrator(parler_config)
            orchestrator.run(
                Path("/tmp/meeting.mp3"),
                checkpoint_path=checkpoint_path,
            )

        assert checkpoint_path.exists(), "Checkpoint was not written"

    def test_checkpoint_contains_transcript_data(self, parler_config, tmp_path):
        """The checkpoint JSON must contain the serialized transcript."""
        checkpoint_path = tmp_path / ".parler-state.json"
        transcript = make_transcript([make_segment(0, "Bonjour.")])

        with (
            patch("parler.pipeline.orchestrator.AudioIngester") as MockIngester,
            patch("parler.pipeline.orchestrator.VoxtralTranscriber") as MockTranscriber,
            patch("parler.pipeline.orchestrator.SpeakerAttributor") as MockAttributor,
            patch("parler.pipeline.orchestrator.DecisionExtractor") as MockExtractor,
            patch("parler.pipeline.orchestrator.ReportRenderer") as MockRenderer,
        ):
            MockIngester.return_value.ingest.return_value = make_audio_file()
            MockTranscriber.return_value.transcribe.return_value = transcript
            MockAttributor.return_value.attribute.return_value = transcript
            MockExtractor.return_value.extract.return_value = make_empty_log()
            MockRenderer.return_value.render.return_value = "# Report"

            orchestrator = PipelineOrchestrator(parler_config)
            orchestrator.run(Path("/tmp/meeting.mp3"), checkpoint_path=checkpoint_path)

        state = json.loads(checkpoint_path.read_text())
        assert "audio_file" in state
        assert "transcript" in state
        assert "completed_stages" in state

    def test_resume_skips_completed_stages(self, parler_config, tmp_path):
        """On --resume, stages listed in checkpoint.completed_stages are not re-run."""
        checkpoint_path = tmp_path / ".parler-state.json"

        # Write a fake checkpoint
        checkpoint_data = {
            "audio_hash": "abc123abc123",
            "completed_stages": ["TRANSCRIBE"],
            "transcript": {
                "text": "Pre-cached.",
                "language": "fr",
                "duration_s": 5.0,
                "segments": [
                    {
                        "id": 0,
                        "start_s": 0.0,
                        "end_s": 5.0,
                        "text": "Pre-cached.",
                        "language": "fr",
                        "speaker_id": None,
                        "speaker_confidence": None,
                        "confidence": 0.9,
                        "no_speech_prob": 0.01,
                        "code_switch": False,
                        "words": None,
                    }
                ],
            },
        }
        checkpoint_path.write_text(json.dumps(checkpoint_data))

        with (
            patch("parler.pipeline.orchestrator.AudioIngester") as MockIngester,
            patch("parler.pipeline.orchestrator.VoxtralTranscriber") as MockTranscriber,
            patch("parler.pipeline.orchestrator.SpeakerAttributor") as MockAttributor,
            patch("parler.pipeline.orchestrator.DecisionExtractor") as MockExtractor,
            patch("parler.pipeline.orchestrator.ReportRenderer") as MockRenderer,
        ):
            MockIngester.return_value.ingest.return_value = make_audio_file(
                content_hash="abc123abc123"
            )
            MockAttributor.return_value.attribute.return_value = make_transcript()
            MockExtractor.return_value.extract.return_value = make_empty_log()
            MockRenderer.return_value.render.return_value = "# Report"

            orchestrator = PipelineOrchestrator(parler_config)
            orchestrator.run(
                Path("/tmp/meeting.mp3"),
                checkpoint_path=checkpoint_path,
                resume=True,
            )

        # Transcription should NOT have been called (it was in the checkpoint)
        MockTranscriber.return_value.transcribe.assert_not_called()

    def test_resume_rejects_checkpoint_with_missing_transcript(self, parler_config, tmp_path):
        """Resume must fail fast when the checkpoint claims a completed stage without data."""
        checkpoint_path = tmp_path / ".parler-state.json"
        checkpoint_path.write_text(
            json.dumps(
                {
                    "audio_hash": "abc123abc123",
                    "completed_stages": ["TRANSCRIBE"],
                }
            )
        )

        with patch("parler.pipeline.orchestrator.AudioIngester") as MockIngester:
            MockIngester.return_value.ingest.return_value = make_audio_file(
                content_hash="abc123abc123"
            )
            orchestrator = PipelineOrchestrator(parler_config)
            with pytest.raises(ProcessingError, match="transcript is missing"):
                orchestrator.run(
                    Path("/tmp/meeting.mp3"),
                    checkpoint_path=checkpoint_path,
                    resume=True,
                )

    def test_resume_with_wrong_audio_hash_raises(self, parler_config, tmp_path):
        """Resuming with a different audio file (different hash) should raise an error."""
        checkpoint_path = tmp_path / ".parler-state.json"
        checkpoint_data = {
            "audio_hash": "original_hash_1234",
            "completed_stages": ["TRANSCRIBE"],
            "transcript": {},
        }
        checkpoint_path.write_text(json.dumps(checkpoint_data))

        with patch("parler.pipeline.orchestrator.AudioIngester") as MockIngester:
            MockIngester.return_value.ingest.return_value = make_audio_file(
                content_hash="different_hash_9999"  # different from checkpoint
            )
            orchestrator = PipelineOrchestrator(parler_config)
            with pytest.raises(ProcessingError, match=r"checkpoint.*mismatch|audio.*changed"):
                orchestrator.run(
                    Path("/tmp/different_meeting.mp3"),
                    checkpoint_path=checkpoint_path,
                    resume=True,
                )


# ─── Cost tracking ────────────────────────────────────────────────────────────


class TestCostTracking:
    def test_estimate_cost_returns_positive_value_for_billable_pipeline(self, parler_config):
        """The default estimator should produce a non-zero conservative value."""
        assert estimate_cost(make_audio_file(duration=600.0), parler_config) > 0

    def test_cost_estimate_computed_before_transcription(self, parler_config):
        """Cost estimate fires before any API call — allows dry-run mode."""
        with (
            patch("parler.pipeline.orchestrator.AudioIngester") as MockIngester,
            patch("parler.pipeline.orchestrator.estimate_cost") as MockEstimate,
            patch("parler.pipeline.orchestrator.VoxtralTranscriber") as MockTranscriber,
            patch("parler.pipeline.orchestrator.SpeakerAttributor") as MockAttributor,
            patch("parler.pipeline.orchestrator.DecisionExtractor") as MockExtractor,
            patch("parler.pipeline.orchestrator.ReportRenderer") as MockRenderer,
        ):
            MockIngester.return_value.ingest.return_value = make_audio_file()
            MockEstimate.return_value = 0.15
            MockTranscriber.return_value.transcribe.return_value = make_transcript()
            MockAttributor.return_value.attribute.return_value = make_transcript()
            MockExtractor.return_value.extract.return_value = make_empty_log()
            MockRenderer.return_value.render.return_value = "# Report"

            orchestrator = PipelineOrchestrator(parler_config)
            orchestrator.run(Path("/tmp/meeting.mp3"))

        MockEstimate.assert_called_once()

    def test_cost_above_threshold_triggers_confirmation(self, parler_config):
        """When estimated cost > confirm_above_usd, a confirmation callback fires."""
        confirm_calls = []

        def on_confirm(estimated_cost):
            confirm_calls.append(estimated_cost)
            return True  # user says yes

        with (
            patch("parler.pipeline.orchestrator.AudioIngester") as MockIngester,
            patch("parler.pipeline.orchestrator.estimate_cost") as MockEstimate,
            patch("parler.pipeline.orchestrator.VoxtralTranscriber") as MockTranscriber,
            patch("parler.pipeline.orchestrator.SpeakerAttributor") as MockAttributor,
            patch("parler.pipeline.orchestrator.DecisionExtractor") as MockExtractor,
            patch("parler.pipeline.orchestrator.ReportRenderer") as MockRenderer,
        ):
            MockIngester.return_value.ingest.return_value = make_audio_file()
            MockEstimate.return_value = 5.00  # above default threshold of 1.0
            MockTranscriber.return_value.transcribe.return_value = make_transcript()
            MockAttributor.return_value.attribute.return_value = make_transcript()
            MockExtractor.return_value.extract.return_value = make_empty_log()
            MockRenderer.return_value.render.return_value = "# Report"

            orchestrator = PipelineOrchestrator(parler_config)
            orchestrator.run(
                Path("/tmp/meeting.mp3"),
                on_cost_confirm=on_confirm,
            )

        assert len(confirm_calls) == 1
        assert confirm_calls[0] == pytest.approx(5.00)

    def test_cost_confirmation_refused_aborts_pipeline(self, parler_config):
        """If on_cost_confirm returns False, the pipeline aborts before any API call."""
        with (
            patch("parler.pipeline.orchestrator.AudioIngester") as MockIngester,
            patch("parler.pipeline.orchestrator.estimate_cost") as MockEstimate,
            patch("parler.pipeline.orchestrator.VoxtralTranscriber") as MockTranscriber,
        ):
            MockIngester.return_value.ingest.return_value = make_audio_file()
            MockEstimate.return_value = 5.00

            orchestrator = PipelineOrchestrator(parler_config)
            result = orchestrator.run(
                Path("/tmp/meeting.mp3"),
                on_cost_confirm=lambda cost: False,  # user says no
            )

        MockTranscriber.return_value.transcribe.assert_not_called()
        assert result is None or result.transcript is None

    def test_cost_above_max_budget_raises_processing_error(self, parler_config):
        """The hard budget cap must abort before the first billable stage."""
        with (
            patch("parler.pipeline.orchestrator.AudioIngester") as MockIngester,
            patch("parler.pipeline.orchestrator.estimate_cost") as MockEstimate,
            patch("parler.pipeline.orchestrator.VoxtralTranscriber") as MockTranscriber,
        ):
            MockIngester.return_value.ingest.return_value = make_audio_file()
            MockEstimate.return_value = 9.00  # fixture max_usd=5.0

            orchestrator = PipelineOrchestrator(parler_config)
            with pytest.raises(ProcessingError, match="exceeds configured cap"):
                orchestrator.run(Path("/tmp/meeting.mp3"))

        MockTranscriber.return_value.transcribe.assert_not_called()


# ─── Progress reporting ───────────────────────────────────────────────────────


class TestProgressReporting:
    def test_on_stage_start_called_for_each_stage(self, parler_config):
        """on_stage_start callback is called once per pipeline stage."""
        started_stages = []

        with (
            patch("parler.pipeline.orchestrator.AudioIngester") as MockIngester,
            patch("parler.pipeline.orchestrator.VoxtralTranscriber") as MockTranscriber,
            patch("parler.pipeline.orchestrator.SpeakerAttributor") as MockAttributor,
            patch("parler.pipeline.orchestrator.DecisionExtractor") as MockExtractor,
            patch("parler.pipeline.orchestrator.ReportRenderer") as MockRenderer,
        ):
            MockIngester.return_value.ingest.return_value = make_audio_file()
            MockTranscriber.return_value.transcribe.return_value = make_transcript()
            MockAttributor.return_value.attribute.return_value = make_transcript()
            MockExtractor.return_value.extract.return_value = make_empty_log()
            MockRenderer.return_value.render.return_value = "# Report"

            orchestrator = PipelineOrchestrator(parler_config)
            orchestrator.run(
                Path("/tmp/meeting.mp3"),
                on_stage_start=lambda stage: started_stages.append(stage),
            )

        assert len(started_stages) >= 4  # ingest, transcribe, attribute, extract, render

    def test_on_stage_complete_called_with_duration(self, parler_config):
        """on_stage_complete callback is called with (stage, duration_s) after each stage."""
        completions = []

        with (
            patch("parler.pipeline.orchestrator.AudioIngester") as MockIngester,
            patch("parler.pipeline.orchestrator.VoxtralTranscriber") as MockTranscriber,
            patch("parler.pipeline.orchestrator.SpeakerAttributor") as MockAttributor,
            patch("parler.pipeline.orchestrator.DecisionExtractor") as MockExtractor,
            patch("parler.pipeline.orchestrator.ReportRenderer") as MockRenderer,
        ):
            MockIngester.return_value.ingest.return_value = make_audio_file()
            MockTranscriber.return_value.transcribe.return_value = make_transcript()
            MockAttributor.return_value.attribute.return_value = make_transcript()
            MockExtractor.return_value.extract.return_value = make_empty_log()
            MockRenderer.return_value.render.return_value = "# Report"

            orchestrator = PipelineOrchestrator(parler_config)
            orchestrator.run(
                Path("/tmp/meeting.mp3"),
                on_stage_complete=lambda stage, duration_s: completions.append((stage, duration_s)),
            )

        assert len(completions) >= 4
        for _stage, duration in completions:
            assert isinstance(duration, float)
            assert duration >= 0.0


# ─── Error isolation ──────────────────────────────────────────────────────────


class TestErrorIsolation:
    def test_attribution_failure_does_not_abort_extraction(self, parler_config):
        """If attribution raises a non-fatal ProcessingError, extraction still runs."""
        with (
            patch("parler.pipeline.orchestrator.AudioIngester") as MockIngester,
            patch("parler.pipeline.orchestrator.VoxtralTranscriber") as MockTranscriber,
            patch("parler.pipeline.orchestrator.SpeakerAttributor") as MockAttributor,
            patch("parler.pipeline.orchestrator.DecisionExtractor") as MockExtractor,
            patch("parler.pipeline.orchestrator.ReportRenderer") as MockRenderer,
        ):
            MockIngester.return_value.ingest.return_value = make_audio_file()
            MockTranscriber.return_value.transcribe.return_value = make_transcript()
            MockAttributor.return_value.attribute.side_effect = ProcessingError(
                "Diarization failed — pyannote not installed"
            )
            MockExtractor.return_value.extract.return_value = make_empty_log()
            MockRenderer.return_value.render.return_value = "# Report"

            orchestrator = PipelineOrchestrator(parler_config)
            result = orchestrator.run(Path("/tmp/meeting.mp3"))

        # Extraction must still have run despite attribution failure
        MockExtractor.return_value.extract.assert_called_once()
        assert result is not None

    def test_transcription_api_error_aborts_pipeline(self, parler_config):
        """An APIError during transcription aborts the whole pipeline."""
        with (
            patch("parler.pipeline.orchestrator.AudioIngester") as MockIngester,
            patch("parler.pipeline.orchestrator.VoxtralTranscriber") as MockTranscriber,
        ):
            MockIngester.return_value.ingest.return_value = make_audio_file()
            MockTranscriber.return_value.transcribe.side_effect = APIError(
                "Rate limit exceeded after 3 retries"
            )

            orchestrator = PipelineOrchestrator(parler_config)
            with pytest.raises(APIError):
                orchestrator.run(Path("/tmp/meeting.mp3"))
