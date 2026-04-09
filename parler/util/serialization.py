"""JSON serialization helpers for local artifacts."""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from datetime import date, datetime
from enum import Enum
import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any


def to_jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return to_jsonable(asdict(value))
    if isinstance(value, dict):
        return {str(key): to_jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [to_jsonable(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    return value


def to_json(value: Any, *, indent: int = 2, sort_keys: bool = False) -> str:
    return json.dumps(to_jsonable(value), indent=indent, sort_keys=sort_keys, ensure_ascii=False)


def write_json_atomic(path: Path, value: Any, *, indent: int = 2, sort_keys: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = to_json(value, indent=indent, sort_keys=sort_keys)
    with NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        handle.write(payload)
        temp_path = Path(handle.name)
    os.replace(temp_path, path)


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))
