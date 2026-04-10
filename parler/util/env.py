"""Environment loading helpers for local workflows."""

from __future__ import annotations

import os
from pathlib import Path

DEFAULT_ENV_FILE = Path(".env")


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        key = name.strip()
        if not key:
            continue
        normalized = value.strip().strip("'\"")
        os.environ.setdefault(key, normalized)


def apply_api_key_aliases() -> None:
    if "MISTRAL_API_KEY" not in os.environ and "PARLER_API_KEY" in os.environ:
        os.environ["MISTRAL_API_KEY"] = os.environ["PARLER_API_KEY"]


__all__ = ["DEFAULT_ENV_FILE", "apply_api_key_aliases", "load_env_file"]
