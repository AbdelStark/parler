"""FFmpeg/ffprobe wrappers."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Versions older than 4.0 ship without several codecs we rely on
# (notably modern AAC profiles, libopus, libx264 in some distro builds).
MIN_RECOMMENDED_FFMPEG_VERSION: tuple[int, int] = (4, 0)


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


@dataclass(frozen=True)
class FFmpegVersion:
    """Parsed result of `ffmpeg -version`."""

    raw: str
    """First line of `ffmpeg -version` output, trimmed."""

    version: str | None
    """Detected version string (e.g. `"7.1"`, `"4.4.4"`). None when unparseable."""

    parts: tuple[int, ...]
    """Numeric prefix of `version`, suitable for tuple comparison."""

    def is_at_least(self, minimum: tuple[int, ...]) -> bool:
        if not self.parts:
            return False
        return self.parts >= minimum


_VERSION_RE = re.compile(r"ffmpeg version\s+(\S+)", re.IGNORECASE)


def _parse_version_parts(version: str) -> tuple[int, ...]:
    """Pull a leading numeric tuple out of a free-form version string.

    `7.1`, `4.4.4-1ubuntu1`, `n6.0` and `git-2024-01-01` all return what
    they should: `(7, 1)`, `(4, 4, 4)`, `(6, 0)`, and `()`. Suffixes after
    the first non-numeric, non-dot character are ignored.
    """

    match = re.match(r"n?(\d+(?:\.\d+)*)", version.strip())
    if not match:
        return ()
    return tuple(int(x) for x in match.group(1).split("."))


def detect_ffmpeg_version(*, timeout: float = 5.0) -> FFmpegVersion | None:
    """Return parsed `ffmpeg -version` output, or None when ffmpeg is missing.

    Catches subprocess timeouts and OS errors so doctor can render a
    'present but version unknown' state without crashing.
    """

    if shutil.which("ffmpeg") is None:
        return None
    try:
        completed = subprocess.run(
            ["ffmpeg", "-version"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return FFmpegVersion(raw="", version=None, parts=())
    raw_line = (completed.stdout or "").splitlines()
    raw = raw_line[0].strip() if raw_line else ""
    match = _VERSION_RE.search(raw) if raw else None
    if match is None:
        return FFmpegVersion(raw=raw, version=None, parts=())
    version_str = match.group(1)
    return FFmpegVersion(
        raw=raw,
        version=version_str,
        parts=_parse_version_parts(version_str),
    )


def convert_with_ffmpeg(source: Path, destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(source),
            "-vn",
            "-acodec",
            "pcm_s16le",
            str(destination),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return destination


def probe_audio(path: Path) -> dict[str, float | int]:
    completed = subprocess.run(
        [
            "ffprobe",
            "-v",
            "quiet",
            "-print_format",
            "json",
            "-show_streams",
            "-show_format",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(completed.stdout or "{}")
    stream: dict[str, Any] = next(
        (item for item in payload.get("streams", []) if item.get("codec_type") == "audio"),
        {},
    )
    format_data = payload.get("format", {})
    duration = float(stream.get("duration") or format_data.get("duration") or 0.0)
    return {
        "duration": duration,
        "sample_rate": int(stream.get("sample_rate") or 0),
        "channels": int(stream.get("channels") or 0),
    }
