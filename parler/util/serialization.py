"""JSON serialization helpers for local artifacts."""

from __future__ import annotations

import json
import os
from contextlib import suppress
from dataclasses import asdict, is_dataclass
from datetime import date, datetime
from enum import Enum
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, cast


def to_jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return to_jsonable(asdict(cast(Any, value)))
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


def _restrict_permissions(path: Path) -> None:
    with suppress(OSError):
        path.chmod(0o600)


def write_json_atomic(path: Path, value: Any, *, indent: int = 2, sort_keys: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = to_json(value, indent=indent, sort_keys=sort_keys)
    temp_path: Path | None = None
    try:
        with NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
            temp_path = Path(handle.name)
            _restrict_permissions(temp_path)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        temp_path.replace(path)
        _restrict_permissions(path)
    except Exception:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
        raise


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))
