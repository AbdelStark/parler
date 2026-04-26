"""Unit tests for FFmpeg version detection in `parler doctor`."""

from __future__ import annotations

import subprocess
from unittest.mock import patch

import pytest
from parler.audio.ffmpeg import (
    MIN_RECOMMENDED_FFMPEG_VERSION,
    FFmpegVersion,
    _parse_version_parts,
    detect_ffmpeg_version,
)
from parler.doctor import _ffmpeg_toolchain_check


class TestParseVersionParts:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("7.1", (7, 1)),
            ("4.4.4-1ubuntu1", (4, 4, 4)),
            ("n6.0", (6, 0)),
            ("git-2024-01-01", ()),
            ("", ()),
            ("3.4.8", (3, 4, 8)),
        ],
    )
    def test_parses_leading_numeric_tuple(self, raw: str, expected: tuple[int, ...]) -> None:
        assert _parse_version_parts(raw) == expected


class TestDetectFfmpegVersion:
    def test_returns_none_when_ffmpeg_not_on_path(self) -> None:
        with patch("parler.audio.ffmpeg.shutil.which", return_value=None):
            assert detect_ffmpeg_version() is None

    def test_parses_modern_version(self) -> None:
        completed = subprocess.CompletedProcess(
            args=["ffmpeg", "-version"],
            returncode=0,
            stdout=(
                "ffmpeg version 7.1 Copyright (c) 2000-2024 the FFmpeg developers\n"
                "built with Apple clang version 15.0.0\n"
            ),
            stderr="",
        )
        with (
            patch("parler.audio.ffmpeg.shutil.which", return_value="/usr/bin/ffmpeg"),
            patch("parler.audio.ffmpeg.subprocess.run", return_value=completed),
        ):
            info = detect_ffmpeg_version()
        assert info is not None
        assert info.version == "7.1"
        assert info.parts == (7, 1)
        assert info.is_at_least(MIN_RECOMMENDED_FFMPEG_VERSION) is True

    def test_parses_old_version_with_distro_suffix(self) -> None:
        completed = subprocess.CompletedProcess(
            args=["ffmpeg", "-version"],
            returncode=0,
            stdout="ffmpeg version 3.4.8-0ubuntu0.2 Copyright\n",
            stderr="",
        )
        with (
            patch("parler.audio.ffmpeg.shutil.which", return_value="/usr/bin/ffmpeg"),
            patch("parler.audio.ffmpeg.subprocess.run", return_value=completed),
        ):
            info = detect_ffmpeg_version()
        assert info is not None
        assert info.version == "3.4.8-0ubuntu0.2"
        assert info.parts == (3, 4, 8)
        assert info.is_at_least(MIN_RECOMMENDED_FFMPEG_VERSION) is False

    def test_handles_unparseable_output(self) -> None:
        completed = subprocess.CompletedProcess(
            args=["ffmpeg", "-version"],
            returncode=0,
            stdout="some unrelated output\n",
            stderr="",
        )
        with (
            patch("parler.audio.ffmpeg.shutil.which", return_value="/usr/bin/ffmpeg"),
            patch("parler.audio.ffmpeg.subprocess.run", return_value=completed),
        ):
            info = detect_ffmpeg_version()
        assert info is not None
        assert info.version is None
        assert info.parts == ()

    def test_handles_subprocess_error(self) -> None:
        with (
            patch("parler.audio.ffmpeg.shutil.which", return_value="/usr/bin/ffmpeg"),
            patch(
                "parler.audio.ffmpeg.subprocess.run",
                side_effect=subprocess.TimeoutExpired(cmd=["ffmpeg"], timeout=5.0),
            ),
        ):
            info = detect_ffmpeg_version()
        assert info is not None
        assert info.version is None
        assert info.parts == ()


class TestFfmpegToolchainCheck:
    def test_warns_when_ffmpeg_missing(self) -> None:
        check = _ffmpeg_toolchain_check(ffmpeg_ready=False)
        assert check.name == "FFmpeg toolchain"
        assert check.status == "warn"
        assert "not found" in check.detail
        assert check.remedy is not None
        assert "Install FFmpeg" in check.remedy

    def test_passes_with_modern_version_in_detail(self) -> None:
        info = FFmpegVersion(raw="ffmpeg version 7.1 ...", version="7.1", parts=(7, 1))
        with patch("parler.doctor.detect_ffmpeg_version", return_value=info):
            check = _ffmpeg_toolchain_check(ffmpeg_ready=True)
        assert check.status == "pass"
        assert "7.1" in check.detail
        assert check.remedy is None

    def test_warns_on_old_version_with_upgrade_hint(self) -> None:
        info = FFmpegVersion(raw="ffmpeg version 3.4.8 ...", version="3.4.8", parts=(3, 4, 8))
        with patch("parler.doctor.detect_ffmpeg_version", return_value=info):
            check = _ffmpeg_toolchain_check(ffmpeg_ready=True)
        assert check.status == "warn"
        assert "3.4.8" in check.detail
        assert "older than the recommended" in check.detail
        assert check.remedy is not None
        assert "Upgrade FFmpeg" in check.remedy

    def test_passes_with_unknown_version_string(self) -> None:
        info = FFmpegVersion(raw="ffmpeg ...", version=None, parts=())
        with patch("parler.doctor.detect_ffmpeg_version", return_value=info):
            check = _ffmpeg_toolchain_check(ffmpeg_ready=True)
        assert check.status == "pass"
        assert "version unknown" in check.detail

    def test_passes_when_detect_returns_none_despite_ready(self) -> None:
        with patch("parler.doctor.detect_ffmpeg_version", return_value=None):
            check = _ffmpeg_toolchain_check(ffmpeg_ready=True)
        assert check.status == "pass"
        assert "version unknown" in check.detail

    def test_passes_with_nightly_build_has_empty_parts(self) -> None:
        # `ffmpeg -version` git builds match `_VERSION_RE` (so `version`
        # is set) but `_parse_version_parts` returns () for `git-...`.
        # The check must NOT then classify it as 'older than 4.0' — that
        # would warn against any nightly. Treat as version-unknown.
        info = FFmpegVersion(
            raw="ffmpeg version git-2024-01-01-abcdef ...",
            version="git-2024-01-01-abcdef",
            parts=(),
        )
        with patch("parler.doctor.detect_ffmpeg_version", return_value=info):
            check = _ffmpeg_toolchain_check(ffmpeg_ready=True)
        assert check.status == "pass"
        assert "version unknown" in check.detail
        assert "older than" not in check.detail
