"""Stable hashing helpers."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path, *, prefix: int | None = None, chunk_size: int = 1024 * 1024) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            hasher.update(chunk)
    digest = hasher.hexdigest()
    if prefix is None:
        return digest
    return digest[:prefix]


def stable_fingerprint(*parts: Any, prefix: int = 16) -> str:
    payload = json.dumps(parts, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return sha256_hex(payload)[:prefix]
