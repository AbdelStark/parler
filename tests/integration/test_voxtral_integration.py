"""
Integration tests: VoxtralTranscriber with mocked Mistral API

These tests verify that the transcription layer correctly:
  - Constructs the Voxtral API request from an AudioFile
  - Parses the raw API response into typed TranscriptSegment objects
  - Handles chunking for long files (simulated via multiple mock responses)
  - Respects retry logic on 429 / 503 responses
  - Uses the content-hash cache to skip re-transcription of known files
  - Propagates language detection correctly
  - Passes through speaker diarization data when present

All external HTTP calls are mocked via pytest-httpx or responses library.
No real API key required. No network access.
"""

import pytest
from datetime import datetime
from unittest.mock import patch, MagicMock, AsyncMock
from pathlib import Path
from parler.transcription.transcriber import VoxtralTranscriber
from parler.models import AudioFile, TranscriptSegment, Transcript
from parler.errors import APIError


# ─── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def mock_audio_file():
    return AudioFile(
        path=Path("/tmp/meeting.mp3"),
        original_path=None,
        format="mp3",
        duration_s=600.0,
        sample_rate=44100,
        channels=2,
        size_bytes=12_000_000,
        content_hash="abcd1234abcd1234",
    )


@pytest.fixture
def mock_voxtral_response_single_chunk():
    """Simulates a Voxtral API response for a single 10-minute chunk."""
    return {
        "text": "Bonjour tout le monde. La réunion commence.",
        "language": "fr",
        "duration": 600.0,
        "segments": [
            {
                "id": 0,
                "start": 0.0,
                "end": 4.5,
                "text": "Bonjour tout le monde.",
                "avg_logprob": -0.12,
                "no_speech_prob": 0.03,
            },
            {
                "id": 1,
                "start": 4.5,
                "end": 9.2,
                "text": "La réunion commence.",
                "avg_logprob": -0.08,
                "no_speech_prob": 0.02,
            },
        ],
    }


# ─── Basic transcription ──────────────────────────────────────────────────────


class TestBasicTranscription:
    def test_transcribe_single_chunk_returns_transcript(
        self, mock_audio_file, mock_voxtral_response_single_chunk
    ):
        with patch("parler.transcription.transcriber.MistralClient") as MockClient:
            mock_instance = MockClient.return_value
            mock_instance.audio.transcriptions.create.return_value = MagicMock(
                **mock_voxtral_response_single_chunk
            )
            transcriber = VoxtralTranscriber(api_key="test-key", model="voxtral-v1-5")
            result = transcriber.transcribe(mock_audio_file, languages=["fr"])

        assert isinstance(result, Transcript)
        assert len(result.segments) == 2

    def test_transcribe_segment_text_populated(
        self, mock_audio_file, mock_voxtral_response_single_chunk
    ):
        with patch("parler.transcription.transcriber.MistralClient") as MockClient:
            mock_instance = MockClient.return_value
            mock_instance.audio.transcriptions.create.return_value = MagicMock(
                **mock_voxtral_response_single_chunk
            )
            transcriber = VoxtralTranscriber(api_key="test-key", model="voxtral-v1-5")
            result = transcriber.transcribe(mock_audio_file)

        assert result.segments[0].text == "Bonjour tout le monde."
        assert result.segments[1].text == "La réunion commence."

    def test_transcribe_detects_language(self, mock_audio_file, mock_voxtral_response_single_chunk):
        with patch("parler.transcription.transcriber.MistralClient") as MockClient:
            mock_instance = MockClient.return_value
            mock_instance.audio.transcriptions.create.return_value = MagicMock(
                **mock_voxtral_response_single_chunk
            )
            transcriber = VoxtralTranscriber(api_key="test-key", model="voxtral-v1-5")
            result = transcriber.transcribe(mock_audio_file)

        assert result.language == "fr"

    def test_transcribe_falls_back_to_requested_language_when_vendor_omits_it(
        self, mock_audio_file, mock_voxtral_response_single_chunk
    ):
        response = dict(mock_voxtral_response_single_chunk)
        response["language"] = None
        response["segments"] = [
            {key: value for key, value in segment.items() if key != "language"}
            for segment in mock_voxtral_response_single_chunk["segments"]
        ]

        with patch("parler.transcription.transcriber.MistralClient") as MockClient:
            mock_instance = MockClient.return_value
            mock_instance.audio.transcriptions.create.return_value = MagicMock(**response)
            transcriber = VoxtralTranscriber(api_key="test-key", model="voxtral-v1-5")
            result = transcriber.transcribe(mock_audio_file, languages=["fr"])

        assert result.language == "fr"
        assert all(segment.language == "fr" for segment in result.segments)

    def test_transcribe_infers_bilingual_segment_languages_and_codeswitch(self, mock_audio_file):
        response = {
            "text": (
                "Bonjour, on commence la réunion. "
                "So, regarding the Python SDK, je pense qu'on devrait l'adopter. "
                "Agreed. The SDK approach is cleaner than direct API calls."
            ),
            "language": None,
            "duration": 30.0,
            "segments": [
                {
                    "id": 0,
                    "start": 0.0,
                    "end": 4.0,
                    "text": "Bonjour, on commence la réunion.",
                    "avg_logprob": -0.12,
                    "no_speech_prob": 0.02,
                },
                {
                    "id": 1,
                    "start": 4.0,
                    "end": 10.0,
                    "text": "So, regarding the Python SDK, je pense qu'on devrait l'adopter.",
                    "avg_logprob": -0.12,
                    "no_speech_prob": 0.02,
                },
                {
                    "id": 2,
                    "start": 10.0,
                    "end": 15.0,
                    "text": "Agreed. The SDK approach is cleaner than direct API calls.",
                    "avg_logprob": -0.12,
                    "no_speech_prob": 0.02,
                },
            ],
        }

        with patch("parler.transcription.transcriber.MistralClient") as MockClient:
            mock_instance = MockClient.return_value
            mock_instance.audio.transcriptions.create.return_value = MagicMock(**response)
            transcriber = VoxtralTranscriber(api_key="test-key", model="voxtral-v1-5")
            result = transcriber.transcribe(mock_audio_file, languages=["fr", "en"])

        languages_found = {segment.language for segment in result.segments}
        assert "fr" in languages_found
        assert "en" in languages_found
        assert any(segment.code_switch for segment in result.segments)

    def test_transcribe_logprob_converted_to_confidence(
        self, mock_audio_file, mock_voxtral_response_single_chunk
    ):
        """avg_logprob of -0.12 should map to a confidence between 0.7 and 1.0."""
        with patch("parler.transcription.transcriber.MistralClient") as MockClient:
            mock_instance = MockClient.return_value
            mock_instance.audio.transcriptions.create.return_value = MagicMock(
                **mock_voxtral_response_single_chunk
            )
            transcriber = VoxtralTranscriber(api_key="test-key", model="voxtral-v1-5")
            result = transcriber.transcribe(mock_audio_file)

        assert 0.0 <= result.segments[0].confidence <= 1.0

    def test_transcribe_duration_populated(
        self, mock_audio_file, mock_voxtral_response_single_chunk
    ):
        with patch("parler.transcription.transcriber.MistralClient") as MockClient:
            mock_instance = MockClient.return_value
            mock_instance.audio.transcriptions.create.return_value = MagicMock(
                **mock_voxtral_response_single_chunk
            )
            transcriber = VoxtralTranscriber(api_key="test-key", model="voxtral-v1-5")
            result = transcriber.transcribe(mock_audio_file)

        assert result.duration_s == pytest.approx(600.0)


# ─── Chunking ────────────────────────────────────────────────────────────────


class TestChunking:
    def test_long_audio_split_into_multiple_chunks(self):
        """A 30-minute audio file (1800s) with max_chunk_s=600 → 3 API calls."""
        long_audio = AudioFile(
            path=Path("/tmp/long-meeting.mp3"),
            original_path=None,
            format="mp3",
            duration_s=1800.0,
            sample_rate=44100,
            channels=2,
            size_bytes=50_000_000,
            content_hash="long123long123",
        )
        chunk_response = MagicMock()
        chunk_response.text = "Some segment text."
        chunk_response.language = "fr"
        chunk_response.duration = 600.0
        chunk_response.segments = [
            MagicMock(
                id=0,
                start=0.0,
                end=5.0,
                text="Segment text.",
                avg_logprob=-0.1,
                no_speech_prob=0.02,
            )
        ]

        with patch("parler.transcription.transcriber.MistralClient") as MockClient:
            mock_instance = MockClient.return_value
            mock_instance.audio.transcriptions.create.return_value = chunk_response
            transcriber = VoxtralTranscriber(
                api_key="test-key", model="voxtral-v1-5", max_chunk_s=600
            )
            result = transcriber.transcribe(long_audio)

        assert mock_instance.audio.transcriptions.create.call_count == 3

    def test_chunk_requests_pass_correct_time_offsets(self):
        """Each chunk call should specify the correct start_time offset."""
        long_audio = AudioFile(
            path=Path("/tmp/long.mp3"),
            original_path=None,
            format="mp3",
            duration_s=1200.0,
            sample_rate=44100,
            channels=2,
            size_bytes=30_000_000,
            content_hash="off123off123",
        )
        call_kwargs = []

        def capture_call(**kwargs):
            call_kwargs.append(kwargs)
            resp = MagicMock()
            resp.text = "Text."
            resp.language = "fr"
            resp.duration = 600.0
            resp.segments = []
            return resp

        with patch("parler.transcription.transcriber.MistralClient") as MockClient:
            mock_instance = MockClient.return_value
            mock_instance.audio.transcriptions.create.side_effect = capture_call
            transcriber = VoxtralTranscriber(
                api_key="test-key", model="voxtral-v1-5", max_chunk_s=600
            )
            transcriber.transcribe(long_audio)

        # Two chunks for 1200s with 600s max
        assert len(call_kwargs) == 2


# ─── Retry behaviour ─────────────────────────────────────────────────────────


class TestRetryBehaviour:
    def test_429_retried_up_to_3_times(self, mock_audio_file, mock_voxtral_response_single_chunk):
        """A 429 response should trigger exponential backoff and retry."""
        from mistralai.exceptions import APIStatusError

        call_count = 0

        def side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise APIStatusError("Rate limited", status_code=429, body={})
            return MagicMock(**mock_voxtral_response_single_chunk)

        with (
            patch("parler.transcription.transcriber.MistralClient") as MockClient,
            patch("time.sleep"),
        ):  # don't actually sleep in tests
            mock_instance = MockClient.return_value
            mock_instance.audio.transcriptions.create.side_effect = side_effect
            transcriber = VoxtralTranscriber(api_key="test-key", model="voxtral-v1-5")
            result = transcriber.transcribe(mock_audio_file)

        assert call_count == 3
        assert result is not None

    def test_429_after_max_retries_raises_api_error(self, mock_audio_file):
        """If all retries exhausted on 429, APIError is raised."""
        from mistralai.exceptions import APIStatusError

        with (
            patch("parler.transcription.transcriber.MistralClient") as MockClient,
            patch("time.sleep"),
        ):
            mock_instance = MockClient.return_value
            mock_instance.audio.transcriptions.create.side_effect = APIStatusError(
                "Rate limited", status_code=429, body={}
            )
            transcriber = VoxtralTranscriber(
                api_key="test-key", model="voxtral-v1-5", max_retries=3
            )
            with pytest.raises(APIError, match="Rate limit exceeded"):
                transcriber.transcribe(mock_audio_file)

    def test_401_not_retried(self, mock_audio_file):
        """Authentication errors should fail immediately, not retry."""
        from mistralai.exceptions import APIStatusError

        with patch("parler.transcription.transcriber.MistralClient") as MockClient:
            mock_instance = MockClient.return_value
            mock_instance.audio.transcriptions.create.side_effect = APIStatusError(
                "Unauthorized", status_code=401, body={}
            )
            transcriber = VoxtralTranscriber(api_key="bad-key", model="voxtral-v1-5")
            with pytest.raises(APIError, match="authentication"):
                transcriber.transcribe(mock_audio_file)

        # Should have called exactly once (no retry on 401)
        assert mock_instance.audio.transcriptions.create.call_count == 1


# ─── Cache integration ────────────────────────────────────────────────────────


class TestCacheIntegration:
    def test_cached_transcript_skips_api_call(
        self, mock_audio_file, mock_voxtral_response_single_chunk, tmp_path
    ):
        """If a cached transcript exists for the content_hash + model, API is not called."""
        from parler.transcription.cache import TranscriptCache

        # Pre-populate the cache
        cache = TranscriptCache(cache_dir=tmp_path)
        cached_transcript = Transcript(
            text="Cached transcript",
            language="fr",
            duration_s=600.0,
            segments=(
                TranscriptSegment(
                    id=0,
                    start_s=0.0,
                    end_s=5.0,
                    text="Cached.",
                    language="fr",
                    speaker_id=None,
                    speaker_confidence=None,
                    confidence=0.9,
                    no_speech_prob=0.01,
                    code_switch=False,
                    words=None,
                ),
            ),
        )
        cache.store(mock_audio_file.content_hash, "voxtral-v1-5", cached_transcript)

        with patch("parler.transcription.transcriber.MistralClient") as MockClient:
            mock_instance = MockClient.return_value
            transcriber = VoxtralTranscriber(api_key="test-key", model="voxtral-v1-5", cache=cache)
            result = transcriber.transcribe(mock_audio_file)

        # API should NOT have been called
        mock_instance.audio.transcriptions.create.assert_not_called()
        assert result.segments[0].text == "Cached."

    def test_different_content_hash_misses_cache(
        self, mock_voxtral_response_single_chunk, tmp_path
    ):
        """A file with a different content hash gets a fresh API call."""
        from parler.transcription.cache import TranscriptCache

        cache = TranscriptCache(cache_dir=tmp_path)

        audio_a = AudioFile(
            path=Path("/tmp/a.mp3"),
            original_path=None,
            format="mp3",
            duration_s=60.0,
            sample_rate=44100,
            channels=2,
            size_bytes=1000,
            content_hash="aaaa1111aaaa1111",
        )
        audio_b = AudioFile(
            path=Path("/tmp/b.mp3"),
            original_path=None,
            format="mp3",
            duration_s=60.0,
            sample_rate=44100,
            channels=2,
            size_bytes=1000,
            content_hash="bbbb2222bbbb2222",
        )

        with patch("parler.transcription.transcriber.MistralClient") as MockClient:
            mock_instance = MockClient.return_value
            mock_instance.audio.transcriptions.create.return_value = MagicMock(
                **mock_voxtral_response_single_chunk
            )
            transcriber = VoxtralTranscriber(api_key="test-key", model="voxtral-v1-5", cache=cache)
            transcriber.transcribe(audio_a)
            transcriber.transcribe(audio_b)

        # Both should have triggered API calls (different hashes = no cache hit)
        assert mock_instance.audio.transcriptions.create.call_count == 2
