"""Persistent speaker memory / participant roster."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .util.serialization import read_json, write_json_atomic


def _timestamp() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


@dataclass
class ParticipantEntry:
    name: str
    aliases: list[str] = field(default_factory=list)
    role: str | None = None
    team: str | None = None
    added_at: str = field(default_factory=_timestamp)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "aliases": self.aliases,
            "role": self.role,
            "team": self.team,
            "added_at": self.added_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ParticipantEntry:
        return cls(
            name=str(data["name"]),
            aliases=list(data.get("aliases", [])),
            role=data.get("role"),
            team=data.get("team"),
            added_at=str(data.get("added_at", _timestamp())),
        )


class Roster:
    DEFAULT_PATH = Path.home() / ".parler" / "roster.json"

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or self.DEFAULT_PATH
        self._entries: list[ParticipantEntry] = []
        self._load()

    def add(self, entry: ParticipantEntry) -> None:
        existing = self.find(entry.name)
        if existing is not None:
            self._entries.remove(existing)
        self._entries.append(entry)
        self._save()

    def remove(self, name: str) -> bool:
        entry = self.find(name)
        if entry is None:
            return False
        self._entries.remove(entry)
        self._save()
        return True

    def find(self, name: str) -> ParticipantEntry | None:
        normalized = name.strip().lower()
        for entry in self._entries:
            if entry.name.lower() == normalized:
                return entry
            if any(alias.lower() == normalized for alias in entry.aliases):
                return entry
        return None

    def all_entries(self) -> list[ParticipantEntry]:
        return list(self._entries)

    def all_names(self) -> list[str]:
        names: list[str] = []
        seen: set[str] = set()
        for entry in self._entries:
            for n in [entry.name, *entry.aliases]:
                if n.lower() not in seen:
                    names.append(n)
                    seen.add(n.lower())
        return names

    def _load(self) -> None:
        if not self._path.exists():
            self._entries = []
            return
        try:
            data = read_json(self._path)
            if isinstance(data, list):
                self._entries = [ParticipantEntry.from_dict(item) for item in data]
            else:
                self._entries = []
        except Exception:
            self._entries = []

    def _save(self) -> None:
        write_json_atomic(self._path, [e.to_dict() for e in self._entries])


__all__ = ["ParticipantEntry", "Roster"]
