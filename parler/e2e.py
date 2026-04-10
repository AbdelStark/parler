"""Convenience runner for local E2E verification."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

from .util.env import DEFAULT_ENV_FILE, apply_api_key_aliases, load_env_file

DEFAULT_EXTRACTION_MODEL = "mistral-medium-latest"
REQUIRED_AUDIO_FIXTURES = (
    Path("tests/fixtures/audio/fr_meeting_5min.mp3"),
    Path("tests/fixtures/audio/bilingual_meeting_5min.mp3"),
    Path("tests/fixtures/audio/earnings_call_45min.mp3"),
)


def _has_explicit_target(args: Sequence[str]) -> bool:
    return any(arg == "tests/e2e" or arg.endswith(".py") or "::" in arg for arg in args)


def build_pytest_args(extra_args: Sequence[str]) -> list[str]:
    args = list(extra_args)
    if not _has_explicit_target(args):
        args.insert(0, "tests/e2e")
    if "-m" not in args and not any(arg.startswith("--markexpr") for arg in args):
        args.extend(["-m", "slow"])
    if "-s" not in args and "--capture=no" not in args:
        args.append("-s")
    if not any(arg in {"-q", "-v", "-vv", "-vvv"} for arg in args):
        args.append("-v")
    return args


def ensure_ffprobe() -> None:
    try:
        subprocess.run(
            ["ffprobe", "-version"],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except FileNotFoundError as exc:  # pragma: no cover - host-specific
        raise SystemExit("ffprobe is required. Install FFmpeg first.") from exc
    except subprocess.CalledProcessError as exc:  # pragma: no cover - host-specific
        raise SystemExit("ffprobe is installed but broken. Reinstall FFmpeg and retry.") from exc


def ensure_audio_fixtures(*, generate: bool) -> None:
    missing = [path for path in REQUIRED_AUDIO_FIXTURES if not path.exists()]
    if not missing:
        return
    if not generate:
        joined = ", ".join(str(path) for path in missing)
        raise SystemExit(f"Missing E2E fixtures: {joined}")

    subprocess.run(
        [sys.executable, "tests/fixtures/generate_fixtures.py", "--all"],
        check=True,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run parler E2E tests with local setup.")
    parser.add_argument(
        "--env-file",
        type=Path,
        default=DEFAULT_ENV_FILE,
        help="Environment file to load before running pytest.",
    )
    parser.add_argument(
        "--model",
        default=DEFAULT_EXTRACTION_MODEL,
        help="Extraction model override for legacy E2E PipelineConfig defaults.",
    )
    parser.add_argument(
        "--no-generate-fixtures",
        action="store_true",
        help="Fail instead of generating missing synthetic audio fixtures.",
    )
    args, pytest_args = parser.parse_known_args(argv)

    load_env_file(args.env_file)
    apply_api_key_aliases()
    if "MISTRAL_API_KEY" not in os.environ:
        raise SystemExit("MISTRAL_API_KEY is required. Set it in the environment or .env.")

    os.environ.setdefault("PARLER_E2E_EXTRACTION_MODEL", args.model)
    ensure_ffprobe()
    ensure_audio_fixtures(generate=not args.no_generate_fixtures)

    command = [sys.executable, "-m", "pytest", *build_pytest_args(pytest_args)]
    completed = subprocess.run(command, check=False)
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
