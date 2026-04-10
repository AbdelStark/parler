"""Audio ingestion and normalization."""

from __future__ import annotations

import math
import subprocess
import tempfile
import wave
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ..errors import EnvironmentError, InputError
from ..models import AudioFile
from ..util.hashing import sha256_file
from .ffmpeg import convert_with_ffmpeg, ffmpeg_available, probe_audio

_NATIVE_EXTENSIONS = {"mp3", "wav", "ogg", "flac", "m4a", "webm"}
_FFMPEG_EXTENSIONS = {"mkv", "mp4", "mov", "avi", "ts"}
_MAX_FILE_SIZE_BYTES = 4 * 1024**3
_INSTALL_HINT = "Install FFmpeg via `brew install ffmpeg` or `apt install ffmpeg`."
_TEMP_AUDIO_DIR = Path(tempfile.gettempdir()) / "parler-audio"


def _read_header(path: Path, size: int = 32) -> bytes:
    with path.open("rb") as handle:
        return handle.read(size)


def _is_mp3(header: bytes) -> bool:
    return header.startswith(b"ID3") or (
        len(header) >= 2 and header[0] == 0xFF and (header[1] & 0xE0) == 0xE0
    )


def _is_wav(header: bytes) -> bool:
    return header.startswith(b"RIFF") and b"WAVE" in header[:12]


def _is_ogg(header: bytes) -> bool:
    return header.startswith(b"OggS")


def _is_flac(header: bytes) -> bool:
    return header.startswith(b"fLaC")


def _is_m4_family(header: bytes) -> bool:
    return len(header) >= 8 and header[4:8] == b"ftyp"


def _is_matroska(header: bytes) -> bool:
    return header.startswith(b"\x1a\x45\xdf\xa3")


def _looks_like_html_or_text(header: bytes) -> bool:
    lowered = header.lower()
    if lowered.startswith((b"<!doctype html", b"<html", b"<?xml")):
        return True
    printable = sum(32 <= byte <= 126 or byte in {9, 10, 13} for byte in header)
    return (
        bool(header)
        and printable / max(len(header), 1) > 0.9
        and not any(
            checker(header)
            for checker in (_is_mp3, _is_wav, _is_ogg, _is_flac, _is_m4_family, _is_matroska)
        )
    )


def _detect_format(path: Path) -> tuple[str, bool]:
    extension = path.suffix.lower().lstrip(".")
    if extension not in _NATIVE_EXTENSIONS | _FFMPEG_EXTENSIONS:
        raise InputError(f"Unsupported format: .{extension}")

    header = _read_header(path)
    if not header:
        raise InputError(f"Input file is empty: {path.name}")
    if _looks_like_html_or_text(header):
        raise InputError(f"{path.name} is not a valid audio file")

    if extension == "mp3" and not _is_mp3(header):
        raise InputError(f"{path.name} is not a valid audio file")
    if extension == "wav" and not _is_wav(header):
        raise InputError(f"{path.name} is not a valid audio file")
    if extension == "ogg" and not _is_ogg(header):
        raise InputError(f"{path.name} is not a valid audio file")
    if extension == "flac" and not _is_flac(header):
        raise InputError(f"{path.name} is not a valid audio file")
    if extension in {"m4a", "mp4", "mov"} and not _is_m4_family(header):
        raise InputError(f"{path.name} is not a valid audio file")
    if extension in {"mkv", "webm"} and not _is_matroska(header):
        raise InputError(f"{path.name} is not a valid audio file")
    if extension == "ts" and header[0] != 0x47:
        raise InputError(f"{path.name} is not a valid audio file")

    return extension, extension in _FFMPEG_EXTENSIONS


def _probe_audio(path: Path) -> dict[str, float | int]:
    try:
        return probe_audio(path)
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or "").strip()
        lowered = detail.lower()
        if any(
            marker in lowered
            for marker in ("library not loaded", "symbol not found", "image not found")
        ):
            raise EnvironmentError(
                f"ffprobe failed while probing {path.name}. {_INSTALL_HINT}"
            ) from exc
        if path.suffix.lower() == ".wav":
            with wave.open(str(path), "rb") as handle:
                rate = handle.getframerate()
                frames = handle.getnframes()
                return {
                    "duration": frames / rate if rate else 0.0,
                    "sample_rate": rate,
                    "channels": handle.getnchannels(),
                }
        detail_suffix = f": {detail}" if detail else ""
        raise InputError(f"Unable to read audio metadata from {path.name}{detail_suffix}") from exc
    except FileNotFoundError as exc:
        raise EnvironmentError(f"ffprobe is unavailable. {_INSTALL_HINT}") from exc
    except Exception:
        if path.suffix.lower() == ".wav":
            with wave.open(str(path), "rb") as handle:
                rate = handle.getframerate()
                frames = handle.getnframes()
                return {
                    "duration": frames / rate if rate else 0.0,
                    "sample_rate": rate,
                    "channels": handle.getnchannels(),
                }
        raise


def _convert_with_ffmpeg(source: Path) -> Path:
    _TEMP_AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    stat = source.stat()
    destination = _TEMP_AUDIO_DIR / f"{source.stem}-{stat.st_mtime_ns}-{stat.st_size}.wav"
    if destination.exists() and destination.stat().st_size > 0:
        return destination
    try:
        converted = convert_with_ffmpeg(source, destination)
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or "").strip()
        lowered = detail.lower()
        if any(
            marker in lowered
            for marker in ("library not loaded", "symbol not found", "image not found")
        ):
            raise EnvironmentError(
                f"FFmpeg failed while decoding {source.name}. {_INSTALL_HINT}"
            ) from exc
        detail_suffix = f": {detail}" if detail else ""
        raise InputError(f"FFmpeg could not decode {source.name}{detail_suffix}") from exc
    except FileNotFoundError as exc:
        raise EnvironmentError(f"FFmpeg is unavailable. {_INSTALL_HINT}") from exc
    with suppress(OSError):
        converted.chmod(0o600)
    return converted


def managed_audio_directory() -> Path:
    _TEMP_AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    return _TEMP_AUDIO_DIR


def managed_audio_file_count() -> int:
    directory = managed_audio_directory()
    return len(list(directory.glob("*.wav")))


def prune_managed_audio_files(*, older_than_days: float = 1.0) -> int:
    directory = managed_audio_directory()
    threshold = datetime.now(UTC) - timedelta(days=older_than_days)
    removed = 0
    for candidate in directory.glob("*.wav"):
        modified = datetime.fromtimestamp(candidate.stat().st_mtime, tz=UTC)
        if math.isclose(older_than_days, 0.0) or modified < threshold:
            candidate.unlink(missing_ok=True)
            removed += 1
    return removed


class AudioIngester:
    """Validate and normalize audio inputs into canonical AudioFile objects."""

    def ingest(self, input_path: str | Path) -> AudioFile:
        path = Path(input_path)
        if not path.exists():
            raise InputError(f"File not found: {path.name}")

        stat = path.stat()
        if stat.st_size == 0:
            raise InputError(f"Input file is empty: {path.name}")
        if stat.st_size > _MAX_FILE_SIZE_BYTES:
            raise InputError(f"Input file exceeds 4 GB limit: {path.name}")

        detected_format, needs_ffmpeg = _detect_format(path)
        working_path = path
        original_path: Path | None = None

        if needs_ffmpeg:
            if not ffmpeg_available():
                raise EnvironmentError(f"FFmpeg required for .{detected_format}. {_INSTALL_HINT}")
            original_path = path
            working_path = _convert_with_ffmpeg(path)
            detected_format = working_path.suffix.lower().lstrip(".") or "wav"
            stat = working_path.stat()

        probe = _probe_audio(working_path)

        return AudioFile(
            path=working_path,
            original_path=original_path,
            format=detected_format,
            duration_s=float(probe["duration"]),
            sample_rate=int(probe["sample_rate"]),
            channels=int(probe["channels"]),
            size_bytes=stat.st_size,
            content_hash=sha256_file(working_path, prefix=16),
        )


__all__ = [
    "AudioFile",
    "AudioIngester",
    "_convert_with_ffmpeg",
    "_probe_audio",
    "ffmpeg_available",
    "managed_audio_directory",
    "managed_audio_file_count",
    "prune_managed_audio_files",
]
