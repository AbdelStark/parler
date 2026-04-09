"""Shared export result contract."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ExportResult:
    success: bool
    url: str | None
    error: str | None


__all__ = ["ExportResult"]
