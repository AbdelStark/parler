"""Tests for the local E2E convenience runner."""

from __future__ import annotations

import os
from pathlib import Path

from parler.e2e import build_pytest_args, load_env_file


class TestE2ERunner:
    def test_load_env_file_sets_missing_values(self, tmp_path: Path) -> None:
        env_file = tmp_path / ".env"
        env_file.write_text(
            "MISTRAL_API_KEY=test-key\nPARLER_E2E_EXTRACTION_MODEL=mistral-medium-latest\n"
        )

        previous = dict(os.environ)
        try:
            os.environ.pop("MISTRAL_API_KEY", None)
            os.environ.pop("PARLER_E2E_EXTRACTION_MODEL", None)

            load_env_file(env_file)

            assert os.environ["MISTRAL_API_KEY"] == "test-key"
            assert os.environ["PARLER_E2E_EXTRACTION_MODEL"] == "mistral-medium-latest"
        finally:
            os.environ.clear()
            os.environ.update(previous)

    def test_load_env_file_preserves_existing_values(self, tmp_path: Path) -> None:
        env_file = tmp_path / ".env"
        env_file.write_text("MISTRAL_API_KEY=file-key\n")

        previous = dict(os.environ)
        try:
            os.environ["MISTRAL_API_KEY"] = "existing-key"

            load_env_file(env_file)

            assert os.environ["MISTRAL_API_KEY"] == "existing-key"
        finally:
            os.environ.clear()
            os.environ.update(previous)

    def test_build_pytest_args_uses_e2e_defaults(self) -> None:
        assert build_pytest_args([]) == ["tests/e2e", "-m", "slow", "-s", "-v"]

    def test_build_pytest_args_preserves_explicit_target_and_verbosity(self) -> None:
        assert build_pytest_args(["tests/e2e/test_full_pipeline_fr.py", "-q"]) == [
            "tests/e2e/test_full_pipeline_fr.py",
            "-q",
            "-m",
            "slow",
            "-s",
        ]
