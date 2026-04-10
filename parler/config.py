"""Configuration models and loader."""

from __future__ import annotations

import json
import os
import tomllib
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, cast

import yaml

from .errors import ConfigError


def _parse_scalar(value: str) -> Any:
    raw = value.strip()
    if raw == "":
        return ""
    lowered = raw.lower()
    if lowered in {"true", "false"}:
        return lowered == "true"
    if raw.startswith(("'", '"')) and raw.endswith(("'", '"')) and len(raw) >= 2:
        return raw[1:-1]
    if raw.startswith("[") and raw.endswith("]"):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            inner = raw[1:-1].strip()
            if not inner:
                return []
            return [item.strip().strip("'\"") for item in inner.split(",")]
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        pass
    if "," in raw:
        return [item.strip() for item in raw.split(",") if item.strip()]
    return raw


def _parse_yaml(text: str) -> dict[str, Any]:
    payload = yaml.safe_load(text) or {}
    if not isinstance(payload, dict):
        raise ConfigError("YAML config root must be a mapping")
    return cast(dict[str, Any], payload)


def _merge_dicts(base: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in incoming.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge_dicts(merged[key], value)
        else:
            merged[key] = value
    return merged


def _set_nested(target: dict[str, Any], path: list[str], value: Any) -> None:
    cursor = target
    for part in path[:-1]:
        existing = cursor.get(part)
        if not isinstance(existing, dict):
            existing = {}
            cursor[part] = existing
        cursor = existing
    cursor[path[-1]] = value


def _parse_config_file(path: Path) -> dict[str, Any]:
    suffix = path.suffix.lower()
    text = path.read_text(encoding="utf-8")
    if suffix == ".toml":
        return tomllib.loads(text)
    if suffix in {".json"}:
        return cast(dict[str, Any], json.loads(text or "{}"))
    if suffix in {".yaml", ".yml"}:
        return _parse_yaml(text)
    raise ConfigError(f"Unsupported config format: {path.suffix}")


@dataclass(frozen=True)
class TranscriptionConfig:
    model: str = "voxtral-mini-latest"
    languages: list[str] = field(default_factory=list)
    timeout_s: int = 300
    max_retries: int = 3


@dataclass(frozen=True)
class ChunkingConfig:
    max_chunk_s: int = 600
    overlap_s: int = 30
    silence_threshold_db: float = -40.0
    prefer_silence_splits: bool = True


@dataclass(frozen=True)
class AttributionConfig:
    enabled: bool = True
    confidence_threshold: float = 0.7
    model: str = "mistral-large-latest"


@dataclass(frozen=True)
class ExtractionConfig:
    model: str = "mistral-large-latest"
    temperature: float = 0.0
    max_tokens: int = 4096
    prompt_version: str = "v1.0"
    multi_pass_threshold: int = 25_000


@dataclass(frozen=True)
class CacheConfig:
    enabled: bool = True
    directory: Path = Path(".parler-cache")
    max_size_gb: float = 1.0
    ttl_days: int = 30


@dataclass(frozen=True)
class OutputConfig:
    format: str = "markdown"
    output_path: Path | None = None
    anonymize_speakers: bool = False


@dataclass(frozen=True)
class CostConfig:
    max_usd: float = 10.0
    confirm_above_usd: float = 1.0


@dataclass(frozen=True)
class ParlerConfig:
    api_key: str = field(repr=False)
    transcription: TranscriptionConfig = field(default_factory=TranscriptionConfig)
    chunking: ChunkingConfig = field(default_factory=ChunkingConfig)
    attribution: AttributionConfig = field(default_factory=AttributionConfig)
    extraction: ExtractionConfig = field(default_factory=ExtractionConfig)
    cache: CacheConfig = field(default_factory=CacheConfig)
    output: OutputConfig = field(default_factory=OutputConfig)
    cost: CostConfig = field(default_factory=CostConfig)
    participants: list[str] = field(default_factory=list)
    meeting_date: date | None = None


def _default_config_dict() -> dict[str, Any]:
    return {
        "api_key": None,
        "transcription": asdict(TranscriptionConfig()),
        "chunking": asdict(ChunkingConfig()),
        "attribution": asdict(AttributionConfig()),
        "extraction": asdict(ExtractionConfig()),
        "cache": {
            **asdict(CacheConfig()),
            "directory": str(CacheConfig().directory),
        },
        "output": asdict(OutputConfig()),
        "cost": asdict(CostConfig()),
        "participants": [],
        "meeting_date": None,
    }


def _discover_default_config() -> Path | None:
    candidates = [
        Path.cwd() / "parler.toml",
        Path.cwd() / "parler.yaml",
        Path.cwd() / "parler.yml",
        Path.cwd() / "parler.json",
        Path.home() / ".parler.toml",
        Path.home() / ".parler.yaml",
        Path.home() / ".parler.yml",
        Path.home() / ".parler.json",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def _apply_environment(data: dict[str, Any]) -> dict[str, Any]:
    result = dict(data)
    api_key = os.environ.get("MISTRAL_API_KEY") or os.environ.get("PARLER_API_KEY")
    if api_key:
        result["api_key"] = api_key

    sections = {"transcription", "chunking", "attribution", "extraction", "cache", "output", "cost"}
    for env_name, raw_value in os.environ.items():
        if not env_name.startswith("PARLER_") or env_name == "PARLER_API_KEY":
            continue
        remainder = env_name[len("PARLER_") :].lower()
        parts = remainder.split("_")
        if parts[0] in sections and len(parts) >= 2:
            path = [parts[0], "_".join(parts[1:])]
        else:
            path = [remainder]
        _set_nested(result, path, _parse_scalar(raw_value))
    return result


def _apply_overrides(data: dict[str, Any], overrides: dict[str, Any] | None) -> dict[str, Any]:
    result = dict(data)
    if not overrides:
        return result
    for key, value in overrides.items():
        path = key.split(".")
        _set_nested(result, path, value)
    return result


def _validate(data: dict[str, Any]) -> None:
    api_key = data.get("api_key")
    if not api_key:
        raise ConfigError("Missing required config field: api_key")
    chunking = data["chunking"]
    transcription = data["transcription"]
    attribution = data["attribution"]
    extraction = data["extraction"]
    cost = data["cost"]
    if chunking["max_chunk_s"] <= 0:
        raise ConfigError("chunking.max_chunk_s must be positive")
    if chunking["overlap_s"] >= chunking["max_chunk_s"]:
        raise ConfigError("chunking.overlap_s must be smaller than max_chunk_s")
    if data["output"]["format"] not in {"markdown", "html", "json"}:
        raise ConfigError("output.format must be one of markdown, html, json")
    if transcription["timeout_s"] <= 0:
        raise ConfigError("transcription.timeout_s must be positive")
    if transcription["max_retries"] < 0:
        raise ConfigError("transcription.max_retries must be non-negative")
    if not 0.0 <= attribution["confidence_threshold"] <= 1.0:
        raise ConfigError("attribution.confidence_threshold must be between 0 and 1")
    if not 0.0 <= extraction["temperature"] <= 2.0:
        raise ConfigError("extraction.temperature must be between 0 and 2")
    if extraction["max_tokens"] <= 0:
        raise ConfigError("extraction.max_tokens must be positive")
    if cost["max_usd"] < 0:
        raise ConfigError("cost.max_usd must be non-negative")
    if cost["confirm_above_usd"] < 0:
        raise ConfigError("cost.confirm_above_usd must be non-negative")
    if cost["confirm_above_usd"] > cost["max_usd"]:
        raise ConfigError("cost.confirm_above_usd must be less than or equal to cost.max_usd")
    if extraction["multi_pass_threshold"] <= 0:
        raise ConfigError("extraction.multi_pass_threshold must be positive")


def load_config(
    config_path: str | Path | None = None, overrides: dict[str, Any] | None = None
) -> ParlerConfig:
    config_file: Path | None
    if config_path is None:
        config_file = _discover_default_config()
    else:
        config_file = Path(config_path)
        if not config_file.exists():
            raise ConfigError(f"Config file not found: {config_file}")

    data = _default_config_dict()
    if config_file is not None:
        if not config_file.exists():
            raise ConfigError(f"Config file not found: {config_file}")
        data = _merge_dicts(data, _parse_config_file(config_file))

    data = _apply_environment(data)
    data = _apply_overrides(data, overrides)
    _validate(data)

    meeting_date = data.get("meeting_date")
    if isinstance(meeting_date, str):
        meeting_date = date.fromisoformat(meeting_date)

    return ParlerConfig(
        api_key=str(data["api_key"]),
        transcription=TranscriptionConfig(**data["transcription"]),
        chunking=ChunkingConfig(**data["chunking"]),
        attribution=AttributionConfig(**data["attribution"]),
        extraction=ExtractionConfig(**data["extraction"]),
        cache=CacheConfig(
            **{
                **data["cache"],
                "directory": Path(data["cache"]["directory"]),
            }
        ),
        output=OutputConfig(
            **{
                **data["output"],
                "output_path": (
                    Path(data["output"]["output_path"])
                    if data["output"].get("output_path")
                    else None
                ),
            }
        ),
        cost=CostConfig(**data["cost"]),
        participants=list(data.get("participants", [])),
        meeting_date=meeting_date,
    )
