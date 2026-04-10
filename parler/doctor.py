"""Operational readiness checks for local parler workflows."""

from __future__ import annotations

import json
import os
import sys
import tomllib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Literal

import yaml

from .audio.ffmpeg import ffmpeg_available
from .audio.ingester import managed_audio_directory, managed_audio_file_count
from .runlog import default_run_directory, iter_run_summaries
from .util.env import DEFAULT_ENV_FILE

_CONFIG_CANDIDATES = ("parler.toml", "parler.yaml", "parler.yml", "parler.json")


@dataclass(frozen=True)
class DoctorCheck:
    """One local readiness check."""

    name: str
    status: Literal["pass", "warn", "fail"]
    detail: str
    critical: bool = False
    remedy: str | None = None


@dataclass(frozen=True)
class DoctorReport:
    """Summarized output for `parler doctor`."""

    project_root: Path
    env_file: Path
    config_path: Path | None
    cache_directory: Path
    run_directory: Path
    temp_audio_directory: Path
    checks: tuple[DoctorCheck, ...]

    @property
    def critical_failures(self) -> tuple[DoctorCheck, ...]:
        return tuple(check for check in self.checks if check.status == "fail" and check.critical)

    @property
    def failures(self) -> tuple[DoctorCheck, ...]:
        return tuple(check for check in self.checks if check.status == "fail")

    @property
    def warnings(self) -> tuple[DoctorCheck, ...]:
        return tuple(check for check in self.checks if check.status == "warn")

    @property
    def ready(self) -> bool:
        return not self.critical_failures


def discover_project_config(project_root: Path) -> Path | None:
    """Return the first project-local parler config file if present."""

    for candidate_name in _CONFIG_CANDIDATES:
        candidate = project_root / candidate_name
        if candidate.exists():
            return candidate
    return None


def _load_raw_config(path: Path) -> dict[str, Any]:
    suffix = path.suffix.lower()
    text = path.read_text(encoding="utf-8")
    if suffix == ".toml":
        payload = tomllib.loads(text)
    elif suffix == ".json":
        payload = json.loads(text or "{}")
    elif suffix in {".yaml", ".yml"}:
        payload = yaml.safe_load(text) or {}
    else:
        raise ValueError(f"Unsupported config format: {path.suffix}")
    if not isinstance(payload, dict):
        raise ValueError("Config root must be a mapping")
    return payload


def _cache_directory_from_raw(project_root: Path, raw_config: dict[str, Any] | None) -> Path:
    default = project_root / ".parler-cache"
    if raw_config is None:
        return default
    cache_data = raw_config.get("cache")
    if not isinstance(cache_data, dict):
        return default
    raw_directory = cache_data.get("directory")
    if not isinstance(raw_directory, str) or not raw_directory.strip():
        return default
    path = Path(raw_directory.strip())
    if not path.is_absolute():
        path = project_root / path
    return path.resolve()


def _api_key_source(raw_config: dict[str, Any] | None, config_path: Path | None) -> str | None:
    if os.environ.get("MISTRAL_API_KEY"):
        return "environment:MISTRAL_API_KEY"
    if os.environ.get("PARLER_API_KEY"):
        return "environment:PARLER_API_KEY"
    if raw_config is not None and raw_config.get("api_key"):
        assert config_path is not None
        return f"config:{config_path.name}"
    return None


def _check_directory_writable(
    *,
    name: str,
    path: Path,
    critical: bool = False,
    remedy: str | None = None,
) -> DoctorCheck:
    try:
        path.mkdir(parents=True, exist_ok=True)
        with NamedTemporaryFile("w", encoding="utf-8", dir=path, delete=False) as handle:
            probe = Path(handle.name)
            handle.write("ok")
        probe.unlink(missing_ok=True)
    except OSError as exc:
        return DoctorCheck(
            name=name,
            status="fail" if critical else "warn",
            detail=f"Not writable: {path} ({exc})",
            critical=critical,
            remedy=remedy,
        )
    return DoctorCheck(name=name, status="pass", detail=str(path), critical=critical)


def _stale_managed_audio_count(directory: Path, *, older_than_days: float) -> int:
    threshold = datetime.now(UTC) - timedelta(days=older_than_days)
    return sum(
        1
        for candidate in directory.glob("*.wav")
        if datetime.fromtimestamp(candidate.stat().st_mtime, tz=UTC) < threshold
    )


def run_doctor(project_root: Path, *, config_path: Path | None = None) -> DoctorReport:
    """Evaluate local readiness for operator-driven parler runs."""

    project_root = project_root.resolve()
    env_file = project_root / DEFAULT_ENV_FILE
    selected_config = (config_path.resolve() if config_path is not None else None) or (
        discover_project_config(project_root)
    )
    raw_config: dict[str, Any] | None = None
    checks: list[DoctorCheck] = [
        DoctorCheck(
            name="Python runtime",
            status="pass" if sys.version_info >= (3, 11) else "fail",
            detail=f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
            critical=sys.version_info < (3, 11),
            remedy="Use `uv python install 3.11` or newer." if sys.version_info < (3, 11) else None,
        ),
        DoctorCheck(
            name="Project env file",
            status="pass" if env_file.exists() else "warn",
            detail=str(env_file) if env_file.exists() else f"Missing optional {env_file.name}",
            remedy="Create it from `.env.example` for local-only secrets."
            if not env_file.exists()
            else None,
        ),
    ]

    if selected_config is None:
        checks.append(
            DoctorCheck(
                name="Project config",
                status="warn",
                detail="No project-local config file found; built-in defaults will be used.",
                remedy="Add `parler.toml` if you want persistent local overrides.",
            )
        )
    else:
        try:
            raw_config = _load_raw_config(selected_config)
        except Exception as exc:
            checks.append(
                DoctorCheck(
                    name="Project config",
                    status="fail",
                    detail=f"Unreadable config: {selected_config} ({exc})",
                    critical=True,
                    remedy="Fix or remove the config file before running parler.",
                )
            )
        else:
            checks.append(
                DoctorCheck(
                    name="Project config",
                    status="pass",
                    detail=str(selected_config),
                )
            )

    cache_directory = _cache_directory_from_raw(project_root, raw_config)
    run_directory = default_run_directory(project_root)
    temp_audio_directory = managed_audio_directory()
    ffmpeg_ready = ffmpeg_available()

    api_key_source = _api_key_source(raw_config, selected_config)
    if api_key_source is None:
        checks.append(
            DoctorCheck(
                name="API key",
                status="fail",
                detail="No Mistral API key configured.",
                critical=True,
                remedy="Set `MISTRAL_API_KEY` or add `api_key` to `parler.toml`.",
            )
        )
    else:
        checks.append(
            DoctorCheck(
                name="API key",
                status="pass",
                detail=f"Configured via {api_key_source}.",
                critical=True,
            )
        )

    checks.append(
        DoctorCheck(
            name="FFmpeg toolchain",
            status="pass" if ffmpeg_ready else "warn",
            detail="ffmpeg + ffprobe available" if ffmpeg_ready else "ffmpeg/ffprobe not found",
            remedy=None
            if ffmpeg_ready
            else "Install FFmpeg to normalize container inputs like mp4/mkv/avi.",
        )
    )
    checks.append(
        _check_directory_writable(
            name="Cache directory",
            path=cache_directory,
            remedy="Ensure the cache path is writable or override `cache.directory`.",
        )
    )
    checks.append(
        _check_directory_writable(
            name="Run artifacts directory",
            path=run_directory,
            remedy="Ensure `.parler-runs/` is writable in the project root.",
        )
    )
    checks.append(
        _check_directory_writable(
            name="Managed temp audio directory",
            path=temp_audio_directory,
            remedy="Ensure the system temp directory is writable.",
        )
    )

    run_count = len(iter_run_summaries(project_root))
    checks.append(
        DoctorCheck(
            name="Recorded runs",
            status="pass",
            detail=f"{run_count} run artifact bundle(s) under {run_directory}",
        )
    )

    temp_audio_count = managed_audio_file_count()
    stale_audio_count = _stale_managed_audio_count(temp_audio_directory, older_than_days=1.0)
    temp_audio_status: Literal["pass", "warn"] = "warn" if stale_audio_count else "pass"
    temp_audio_detail = (
        f"{temp_audio_count} managed audio file(s), {stale_audio_count} stale"
        if temp_audio_count
        else "No managed temp audio files present."
    )
    checks.append(
        DoctorCheck(
            name="Managed temp audio backlog",
            status=temp_audio_status,
            detail=temp_audio_detail,
            remedy=(
                None
                if not stale_audio_count
                else "Run `parler cleanup --temp-audio --older-than-days 1`."
            ),
        )
    )

    return DoctorReport(
        project_root=project_root,
        env_file=env_file,
        config_path=selected_config,
        cache_directory=cache_directory,
        run_directory=run_directory,
        temp_audio_directory=temp_audio_directory,
        checks=tuple(checks),
    )


def format_doctor_report(report: DoctorReport) -> str:
    """Render a stable human-readable doctor report."""

    lines = [
        f"parler doctor · {report.project_root}",
        "",
    ]
    for check in report.checks:
        prefix = {
            "pass": "PASS",
            "warn": "WARN",
            "fail": "FAIL",
        }[check.status]
        detail = f"{prefix}  {check.name}: {check.detail}"
        if check.remedy:
            detail = f"{detail} Remedy: {check.remedy}"
        lines.append(detail)
    lines.extend(
        (
            "",
            "Summary: "
            f"{len(report.failures)} fail, {len(report.warnings)} warn, "
            f"ready={'yes' if report.ready else 'no'}",
        )
    )
    return "\n".join(lines)


__all__ = [
    "DoctorCheck",
    "DoctorReport",
    "discover_project_config",
    "format_doctor_report",
    "run_doctor",
]
