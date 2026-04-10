"""
TDD specification: AudioIngester.ingest()

The audio ingestion layer reads audio files from disk, validates them,
and normalises them into a pipeline-ready AudioFile object.

Design contract:
  - Accepts: .mp3, .wav, .ogg, .flac, .m4a, .webm natively
  - Requires FFmpeg for: .mkv, .mp4, .mov, .avi, .ts
  - Never reads more than 4 GB into memory without streaming
  - Rejects files that are clearly not audio (binary blobs, HTML, text)
  - Extracts duration, sample rate, channel count from file headers
  - Computes a content-hash (sha256[:16]) for cache keying
  - Raises InputError (exit_code=2) for bad files
  - Raises EnvironmentError (exit_code=3) for missing FFmpeg on container formats
"""

import hashlib
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from parler.audio.ingester import AudioIngester, _convert_with_ffmpeg
from parler.errors import EnvironmentError, InputError

# ─── Helpers ────────────────────────────────────────────────────────────────


def make_tmp_audio(
    tmp_path: Path, name: str, content: bytes = b"RIFF\x00\x00\x00\x00WAVEfmt "
) -> Path:
    """Write a fake audio file with recognisable magic bytes."""
    p = tmp_path / name
    p.write_bytes(content)
    return p


# ─── Supported formats ───────────────────────────────────────────────────────


class TestSupportedFormats:
    def test_mp3_file_accepted(self, tmp_path):
        """ID3v2 header magic: 0x49 0x44 0x33"""
        f = make_tmp_audio(tmp_path, "meeting.mp3", b"ID3\x04\x00\x00" + b"\x00" * 100)
        with patch("parler.audio.ingester._probe_audio") as mock_probe:
            mock_probe.return_value = {"duration": 1800.0, "sample_rate": 44100, "channels": 2}
            result = AudioIngester().ingest(f)
        assert result.path == f
        assert result.format == "mp3"

    def test_wav_file_accepted(self, tmp_path):
        """RIFF/WAVE header."""
        f = make_tmp_audio(tmp_path, "meeting.wav", b"RIFF\x24\x00\x00\x00WAVE")
        with patch("parler.audio.ingester._probe_audio") as mock_probe:
            mock_probe.return_value = {"duration": 600.0, "sample_rate": 16000, "channels": 1}
            result = AudioIngester().ingest(f)
        assert result.format == "wav"

    def test_ogg_file_accepted(self, tmp_path):
        f = make_tmp_audio(tmp_path, "meeting.ogg", b"OggS" + b"\x00" * 100)
        with patch("parler.audio.ingester._probe_audio") as mock_probe:
            mock_probe.return_value = {"duration": 900.0, "sample_rate": 48000, "channels": 2}
            result = AudioIngester().ingest(f)
        assert result.format == "ogg"

    def test_flac_file_accepted(self, tmp_path):
        f = make_tmp_audio(tmp_path, "meeting.flac", b"fLaC" + b"\x00" * 100)
        with patch("parler.audio.ingester._probe_audio") as mock_probe:
            mock_probe.return_value = {"duration": 1200.0, "sample_rate": 44100, "channels": 2}
            result = AudioIngester().ingest(f)
        assert result.format == "flac"

    def test_m4a_file_accepted(self, tmp_path):
        """M4A (ftyp atom at byte 4)."""
        f = make_tmp_audio(tmp_path, "meeting.m4a", b"\x00\x00\x00\x20ftypM4A " + b"\x00" * 100)
        with patch("parler.audio.ingester._probe_audio") as mock_probe:
            mock_probe.return_value = {"duration": 3600.0, "sample_rate": 44100, "channels": 2}
            result = AudioIngester().ingest(f)
        assert result.format == "m4a"


# ─── FFmpeg-required formats ──────────────────────────────────────────────────


class TestFFmpegFormats:
    def test_mkv_without_ffmpeg_raises_environment_error(self, tmp_path):
        f = make_tmp_audio(tmp_path, "meeting.mkv", b"\x1a\x45\xdf\xa3" + b"\x00" * 100)
        with (
            patch("parler.audio.ingester.ffmpeg_available", return_value=False),
            pytest.raises(EnvironmentError, match=r"FFmpeg required for \.mkv"),
        ):
            AudioIngester().ingest(f)

    def test_mkv_with_ffmpeg_converts_and_succeeds(self, tmp_path):
        f = make_tmp_audio(tmp_path, "meeting.mkv", b"\x1a\x45\xdf\xa3" + b"\x00" * 100)
        converted = tmp_path / "meeting_converted.wav"
        converted.write_bytes(b"RIFF\x00\x00\x00\x00WAVE")
        with (
            patch("parler.audio.ingester.ffmpeg_available", return_value=True),
            patch("parler.audio.ingester._convert_with_ffmpeg", return_value=converted),
            patch("parler.audio.ingester._probe_audio") as mock_probe,
        ):
            mock_probe.return_value = {"duration": 600.0, "sample_rate": 44100, "channels": 2}
            result = AudioIngester().ingest(f)
        assert result.path == converted
        assert result.original_path == f

    def test_mp4_without_ffmpeg_raises_environment_error(self, tmp_path):
        f = make_tmp_audio(tmp_path, "meeting.mp4", b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 100)
        with (
            patch("parler.audio.ingester.ffmpeg_available", return_value=False),
            pytest.raises(EnvironmentError, match=r"FFmpeg required for \.mp4"),
        ):
            AudioIngester().ingest(f)

    def test_ffmpeg_error_raises_with_install_hint(self, tmp_path):
        """When FFmpeg is missing, error message must include install instructions."""
        f = make_tmp_audio(tmp_path, "meeting.mkv", b"\x1a\x45\xdf\xa3" + b"\x00" * 100)
        with (
            patch("parler.audio.ingester.ffmpeg_available", return_value=False),
            pytest.raises(EnvironmentError) as exc_info,
        ):
            AudioIngester().ingest(f)
        msg = str(exc_info.value)
        assert "brew install ffmpeg" in msg or "apt install ffmpeg" in msg

    def test_conversion_writes_to_temp_area_not_source_directory(self, tmp_path):
        f = make_tmp_audio(tmp_path, "meeting.mkv", b"\x1a\x45\xdf\xa3" + b"\x00" * 100)

        def fake_convert(source: Path, destination: Path) -> Path:
            destination.write_bytes(b"RIFF\x00\x00\x00\x00WAVE")
            return destination

        with patch("parler.audio.ingester.convert_with_ffmpeg", side_effect=fake_convert):
            converted = _convert_with_ffmpeg(f)

        assert converted.parent != tmp_path
        assert str(converted).startswith(str(Path(tempfile.gettempdir())))

    def test_ffmpeg_decode_failure_raises_input_error(self, tmp_path):
        f = make_tmp_audio(tmp_path, "meeting.mkv", b"\x1a\x45\xdf\xa3" + b"\x00" * 100)
        error = subprocess.CalledProcessError(
            1,
            ["ffmpeg"],
            stderr="Invalid data found when processing input",
        )
        with (
            patch("parler.audio.ingester.ffmpeg_available", return_value=True),
            patch("parler.audio.ingester.convert_with_ffmpeg", side_effect=error),
            pytest.raises(InputError, match="could not decode"),
        ):
            AudioIngester().ingest(f)


# ─── File validation ──────────────────────────────────────────────────────────


class TestFileValidation:
    def test_missing_file_raises_input_error(self, tmp_path):
        nonexistent = tmp_path / "nonexistent.mp3"
        with pytest.raises(InputError, match="File not found"):
            AudioIngester().ingest(nonexistent)

    def test_input_error_message_contains_filename(self, tmp_path):
        nonexistent = tmp_path / "my-meeting.mp3"
        with pytest.raises(InputError) as exc_info:
            AudioIngester().ingest(nonexistent)
        assert "my-meeting.mp3" in str(exc_info.value)

    def test_text_file_with_mp3_extension_rejected(self, tmp_path):
        """A text file masquerading as MP3 should be rejected by magic byte check."""
        f = tmp_path / "not-audio.mp3"
        f.write_text("This is not audio data, it is text.")
        with pytest.raises(InputError, match="not a valid audio file"):
            AudioIngester().ingest(f)

    def test_html_file_with_mp3_extension_rejected(self, tmp_path):
        f = tmp_path / "page.mp3"
        f.write_bytes(b"<!DOCTYPE html><html><body>")
        with pytest.raises(InputError):
            AudioIngester().ingest(f)

    def test_zero_byte_file_raises_input_error(self, tmp_path):
        f = tmp_path / "empty.mp3"
        f.write_bytes(b"")
        with pytest.raises(InputError, match="empty"):
            AudioIngester().ingest(f)

    def test_unsupported_extension_raises_input_error(self, tmp_path):
        f = tmp_path / "document.pdf"
        f.write_bytes(b"%PDF-1.4 ")
        with pytest.raises(InputError, match="Unsupported format"):
            AudioIngester().ingest(f)

    def test_metadata_probe_failure_raises_typed_input_error(self, tmp_path):
        f = make_tmp_audio(tmp_path, "meeting.mp3", b"ID3\x04\x00\x00" + b"\x00" * 100)
        error = subprocess.CalledProcessError(
            1,
            ["ffprobe"],
            stderr="Invalid data found when processing input",
        )
        with (
            patch("parler.audio.ingester.probe_audio", side_effect=error),
            pytest.raises(InputError, match="Unable to read audio metadata"),
        ):
            AudioIngester().ingest(f)


# ─── AudioFile properties ─────────────────────────────────────────────────────


class TestAudioFileProperties:
    def test_duration_extracted_from_probe(self, tmp_path):
        f = make_tmp_audio(tmp_path, "meeting.mp3", b"ID3\x04\x00\x00" + b"\x00" * 100)
        with patch("parler.audio.ingester._probe_audio") as mock_probe:
            mock_probe.return_value = {"duration": 2700.0, "sample_rate": 44100, "channels": 2}
            result = AudioIngester().ingest(f)
        assert result.duration_s == pytest.approx(2700.0)

    def test_sample_rate_extracted(self, tmp_path):
        f = make_tmp_audio(tmp_path, "meeting.mp3", b"ID3\x04\x00\x00" + b"\x00" * 100)
        with patch("parler.audio.ingester._probe_audio") as mock_probe:
            mock_probe.return_value = {"duration": 600.0, "sample_rate": 16000, "channels": 1}
            result = AudioIngester().ingest(f)
        assert result.sample_rate == 16000

    def test_channel_count_extracted(self, tmp_path):
        f = make_tmp_audio(tmp_path, "meeting.mp3", b"ID3\x04\x00\x00" + b"\x00" * 100)
        with patch("parler.audio.ingester._probe_audio") as mock_probe:
            mock_probe.return_value = {"duration": 600.0, "sample_rate": 44100, "channels": 2}
            result = AudioIngester().ingest(f)
        assert result.channels == 2

    def test_content_hash_is_sha256_prefix(self, tmp_path):
        content = b"ID3\x04\x00\x00" + b"\x01" * 200
        f = make_tmp_audio(tmp_path, "meeting.mp3", content)
        expected_hash = hashlib.sha256(content).hexdigest()[:16]
        with patch("parler.audio.ingester._probe_audio") as mock_probe:
            mock_probe.return_value = {"duration": 10.0, "sample_rate": 44100, "channels": 1}
            result = AudioIngester().ingest(f)
        assert result.content_hash == expected_hash

    def test_content_hash_same_content_same_hash(self, tmp_path):
        content = b"ID3\x04\x00\x00" + b"\xab" * 200
        f1 = make_tmp_audio(tmp_path, "a.mp3", content)
        f2 = make_tmp_audio(tmp_path, "b.mp3", content)
        with patch("parler.audio.ingester._probe_audio") as mock_probe:
            mock_probe.return_value = {"duration": 10.0, "sample_rate": 44100, "channels": 1}
            r1 = AudioIngester().ingest(f1)
            r2 = AudioIngester().ingest(f2)
        assert r1.content_hash == r2.content_hash

    def test_content_hash_different_content_different_hash(self, tmp_path):
        f1 = make_tmp_audio(tmp_path, "a.mp3", b"ID3\x04\x00\x00" + b"\x01" * 200)
        f2 = make_tmp_audio(tmp_path, "b.mp3", b"ID3\x04\x00\x00" + b"\x02" * 200)
        with patch("parler.audio.ingester._probe_audio") as mock_probe:
            mock_probe.return_value = {"duration": 10.0, "sample_rate": 44100, "channels": 1}
            r1 = AudioIngester().ingest(f1)
            r2 = AudioIngester().ingest(f2)
        assert r1.content_hash != r2.content_hash

    def test_audiofile_is_immutable(self, tmp_path):
        """AudioFile is a frozen dataclass; mutation must raise."""
        f = make_tmp_audio(tmp_path, "meeting.mp3", b"ID3\x04\x00\x00" + b"\x00" * 100)
        with patch("parler.audio.ingester._probe_audio") as mock_probe:
            mock_probe.return_value = {"duration": 600.0, "sample_rate": 44100, "channels": 2}
            result = AudioIngester().ingest(f)
        with pytest.raises((AttributeError, TypeError)):
            result.duration_s = 999.0


# ─── Size limits ─────────────────────────────────────────────────────────────


class TestSizeLimits:
    def test_file_size_reported_correctly(self, tmp_path):
        content = b"ID3\x04\x00\x00" + b"\x00" * 1024
        f = make_tmp_audio(tmp_path, "meeting.mp3", content)
        with patch("parler.audio.ingester._probe_audio") as mock_probe:
            mock_probe.return_value = {"duration": 5.0, "sample_rate": 44100, "channels": 1}
            result = AudioIngester().ingest(f)
        assert result.size_bytes == len(content)

    def test_file_over_4gb_raises_input_error(self, tmp_path):
        """Files > 4 GB are rejected to prevent OOM."""
        f = tmp_path / "giant.mp3"
        # Don't actually write 4 GB — mock stat instead
        f.write_bytes(b"ID3\x04\x00\x00" + b"\x00" * 100)
        with patch("pathlib.Path.stat") as mock_stat:
            mock_stat.return_value = MagicMock(st_size=4 * 1024**3 + 1)
            with pytest.raises(InputError, match="exceeds 4 GB"):
                AudioIngester().ingest(f)

    def test_file_exactly_4gb_accepted(self, tmp_path):
        """Exactly 4 GB should not trigger the size guard."""
        f = tmp_path / "big.mp3"
        f.write_bytes(b"ID3\x04\x00\x00" + b"\x00" * 100)
        four_gb = 4 * 1024**3
        with (
            patch("pathlib.Path.stat") as mock_stat,
            patch("parler.audio.ingester._probe_audio") as mock_probe,
        ):
            mock_stat.return_value = MagicMock(st_size=four_gb)
            mock_probe.return_value = {"duration": 10.0, "sample_rate": 44100, "channels": 2}
            result = AudioIngester().ingest(f)
        assert result.size_bytes == four_gb
