"""
TDD specification: config_loader.load_config()

The config loader reads a TOML/YAML/JSON config file and produces a validated
ParlerConfig object. It merges three layers in priority order:
  1. Defaults (built-in)
  2. Config file (~/.parler.toml or ./parler.toml or --config path)
  3. Environment variables (PARLER_* prefix)
  4. CLI flags (highest priority, passed in as overrides dict)

Design contract:
  - Missing optional fields default to built-in values (never raises for missing optionals)
  - Missing required fields raise ConfigError with the field name
  - Environment variables override config file (PARLER_API_KEY, PARLER_MODEL, etc.)
  - CLI overrides beat everything
  - Invalid values (negative duration, unknown model name) raise ConfigError
  - The loaded config is a fully-typed ParlerConfig dataclass
  - API key never appears in repr() or str() of the config object (security)
"""

import os
from pathlib import Path
from unittest.mock import patch

import pytest
from parler.config import ParlerConfig, load_config
from parler.errors import ConfigError

# ─── Helpers ────────────────────────────────────────────────────────────────


def write_toml(tmp_path: Path, content: str) -> Path:
    p = tmp_path / "parler.toml"
    p.write_text(content)
    return p


def write_yaml(tmp_path: Path, content: str) -> Path:
    p = tmp_path / "parler.yaml"
    p.write_text(content)
    return p


# ─── Default values ──────────────────────────────────────────────────────────


class TestDefaults:
    def test_empty_config_file_uses_all_defaults(self, tmp_path):
        """An empty config file (with API key from env) should load with defaults."""
        cfg_file = write_toml(tmp_path, "")
        with patch.dict(os.environ, {"MISTRAL_API_KEY": "test-key"}):
            config = load_config(config_path=cfg_file)
        assert config is not None
        assert isinstance(config, ParlerConfig)

    def test_default_transcription_model(self, tmp_path):
        cfg_file = write_toml(tmp_path, "")
        with patch.dict(os.environ, {"MISTRAL_API_KEY": "test-key"}):
            config = load_config(config_path=cfg_file)
        assert "voxtral" in config.transcription.model.lower()

    def test_default_extraction_model(self, tmp_path):
        cfg_file = write_toml(tmp_path, "")
        with patch.dict(os.environ, {"MISTRAL_API_KEY": "test-key"}):
            config = load_config(config_path=cfg_file)
        assert config.extraction.model is not None

    def test_default_chunk_size_is_600s(self, tmp_path):
        cfg_file = write_toml(tmp_path, "")
        with patch.dict(os.environ, {"MISTRAL_API_KEY": "test-key"}):
            config = load_config(config_path=cfg_file)
        assert config.chunking.max_chunk_s == 600

    def test_default_overlap_is_30s(self, tmp_path):
        cfg_file = write_toml(tmp_path, "")
        with patch.dict(os.environ, {"MISTRAL_API_KEY": "test-key"}):
            config = load_config(config_path=cfg_file)
        assert config.chunking.overlap_s == 30

    def test_default_cache_enabled(self, tmp_path):
        cfg_file = write_toml(tmp_path, "")
        with patch.dict(os.environ, {"MISTRAL_API_KEY": "test-key"}):
            config = load_config(config_path=cfg_file)
        assert config.cache.enabled is True

    def test_default_output_format_is_markdown(self, tmp_path):
        cfg_file = write_toml(tmp_path, "")
        with patch.dict(os.environ, {"MISTRAL_API_KEY": "test-key"}):
            config = load_config(config_path=cfg_file)
        assert config.output.format == "markdown"


# ─── TOML config file ────────────────────────────────────────────────────────


class TestTOMLConfigFile:
    def test_transcription_model_override(self, tmp_path):
        cfg_file = write_toml(
            tmp_path,
            """
[transcription]
model = "voxtral-v1-5"
""",
        )
        with patch.dict(os.environ, {"MISTRAL_API_KEY": "test-key"}):
            config = load_config(config_path=cfg_file)
        assert config.transcription.model == "voxtral-v1-5"

    def test_extraction_model_override(self, tmp_path):
        cfg_file = write_toml(
            tmp_path,
            """
[extraction]
model = "mistral-large-latest"
""",
        )
        with patch.dict(os.environ, {"MISTRAL_API_KEY": "test-key"}):
            config = load_config(config_path=cfg_file)
        assert config.extraction.model == "mistral-large-latest"

    def test_chunking_config_override(self, tmp_path):
        cfg_file = write_toml(
            tmp_path,
            """
[chunking]
max_chunk_s = 300
overlap_s = 15
""",
        )
        with patch.dict(os.environ, {"MISTRAL_API_KEY": "test-key"}):
            config = load_config(config_path=cfg_file)
        assert config.chunking.max_chunk_s == 300
        assert config.chunking.overlap_s == 15

    def test_participants_list_loaded(self, tmp_path):
        cfg_file = write_toml(
            tmp_path,
            """
participants = ["Alice", "Bob", "Charlie"]
""",
        )
        with patch.dict(os.environ, {"MISTRAL_API_KEY": "test-key"}):
            config = load_config(config_path=cfg_file)
        assert config.participants == ["Alice", "Bob", "Charlie"]

    def test_api_key_from_toml_loaded(self, tmp_path):
        cfg_file = write_toml(
            tmp_path,
            """
api_key = "sk-from-config"
""",
        )
        with patch.dict(os.environ, {}, clear=True):
            config = load_config(config_path=cfg_file)
        assert config.api_key == "sk-from-config"

    def test_yaml_config_file_also_supported(self, tmp_path):
        cfg_file = write_yaml(
            tmp_path,
            """
transcription:
  model: voxtral-v1-5
""",
        )
        with patch.dict(os.environ, {"MISTRAL_API_KEY": "test-key"}):
            config = load_config(config_path=cfg_file)
        assert config.transcription.model == "voxtral-v1-5"


# ─── Environment variable overrides ──────────────────────────────────────────


class TestEnvironmentVariables:
    def test_mistral_api_key_env_var_loaded(self, tmp_path):
        cfg_file = write_toml(tmp_path, "")
        with patch.dict(os.environ, {"MISTRAL_API_KEY": "sk-env-key"}):
            config = load_config(config_path=cfg_file)
        assert config.api_key == "sk-env-key"

    def test_parler_api_key_env_var_loaded(self, tmp_path):
        """PARLER_API_KEY is an alias for MISTRAL_API_KEY."""
        cfg_file = write_toml(tmp_path, "")
        with patch.dict(os.environ, {"PARLER_API_KEY": "sk-parler-key"}, clear=True):
            config = load_config(config_path=cfg_file)
        assert config.api_key == "sk-parler-key"

    def test_env_var_overrides_config_file(self, tmp_path):
        """PARLER_* env vars beat config file values."""
        cfg_file = write_toml(
            tmp_path,
            """
[transcription]
model = "voxtral-v1"
""",
        )
        with patch.dict(
            os.environ,
            {
                "MISTRAL_API_KEY": "test-key",
                "PARLER_TRANSCRIPTION_MODEL": "voxtral-v1-5",
            },
        ):
            config = load_config(config_path=cfg_file)
        assert config.transcription.model == "voxtral-v1-5"

    def test_parler_cache_disabled_env_var(self, tmp_path):
        cfg_file = write_toml(tmp_path, "")
        with patch.dict(
            os.environ,
            {
                "MISTRAL_API_KEY": "test-key",
                "PARLER_CACHE_ENABLED": "false",
            },
        ):
            config = load_config(config_path=cfg_file)
        assert config.cache.enabled is False


# ─── CLI overrides ────────────────────────────────────────────────────────────


class TestCLIOverrides:
    def test_cli_output_format_overrides_config(self, tmp_path):
        cfg_file = write_toml(
            tmp_path,
            """
[output]
format = "markdown"
""",
        )
        with patch.dict(os.environ, {"MISTRAL_API_KEY": "test-key"}):
            config = load_config(config_path=cfg_file, overrides={"output.format": "json"})
        assert config.output.format == "json"

    def test_cli_api_key_override_beats_env_and_config(self, tmp_path):
        cfg_file = write_toml(tmp_path, 'api_key = "sk-from-file"')
        with patch.dict(os.environ, {"MISTRAL_API_KEY": "sk-from-env"}):
            config = load_config(config_path=cfg_file, overrides={"api_key": "sk-from-cli"})
        assert config.api_key == "sk-from-cli"

    def test_cli_participants_override(self, tmp_path):
        cfg_file = write_toml(tmp_path, "")
        with patch.dict(os.environ, {"MISTRAL_API_KEY": "test-key"}):
            config = load_config(
                config_path=cfg_file, overrides={"participants": ["Pierre", "Sophie"]}
            )
        assert "Pierre" in config.participants


# ─── Validation errors ────────────────────────────────────────────────────────


class TestValidationErrors:
    def test_missing_api_key_raises_config_error(self, tmp_path):
        cfg_file = write_toml(tmp_path, "")
        with patch.dict(os.environ, {}, clear=True), pytest.raises(ConfigError, match="api_key"):
            load_config(config_path=cfg_file)

    def test_negative_chunk_size_raises_config_error(self, tmp_path):
        cfg_file = write_toml(
            tmp_path,
            """
[chunking]
max_chunk_s = -100
""",
        )
        with (
            patch.dict(os.environ, {"MISTRAL_API_KEY": "test-key"}),
            pytest.raises(ConfigError, match="max_chunk_s"),
        ):
            load_config(config_path=cfg_file)

    def test_overlap_larger_than_chunk_raises_config_error(self, tmp_path):
        """Overlap must be < max_chunk_s."""
        cfg_file = write_toml(
            tmp_path,
            """
[chunking]
max_chunk_s = 60
overlap_s = 90
""",
        )
        with (
            patch.dict(os.environ, {"MISTRAL_API_KEY": "test-key"}),
            pytest.raises(ConfigError, match="overlap"),
        ):
            load_config(config_path=cfg_file)

    def test_unknown_output_format_raises_config_error(self, tmp_path):
        cfg_file = write_toml(
            tmp_path,
            """
[output]
format = "docx"
""",
        )
        with (
            patch.dict(os.environ, {"MISTRAL_API_KEY": "test-key"}),
            pytest.raises(ConfigError, match="format"),
        ):
            load_config(config_path=cfg_file)

    def test_invalid_max_cost_usd_raises_config_error(self, tmp_path):
        cfg_file = write_toml(
            tmp_path,
            """
[cost]
max_usd = -5.0
""",
        )
        with (
            patch.dict(os.environ, {"MISTRAL_API_KEY": "test-key"}),
            pytest.raises(ConfigError, match="max_usd"),
        ):
            load_config(config_path=cfg_file)

    def test_confirm_threshold_above_max_cost_raises_config_error(self, tmp_path):
        cfg_file = write_toml(
            tmp_path,
            """
[cost]
max_usd = 2.0
confirm_above_usd = 5.0
""",
        )
        with (
            patch.dict(os.environ, {"MISTRAL_API_KEY": "test-key"}),
            pytest.raises(ConfigError, match="confirm_above_usd"),
        ):
            load_config(config_path=cfg_file)

    def test_non_positive_transcription_timeout_raises_config_error(self, tmp_path):
        cfg_file = write_toml(
            tmp_path,
            """
[transcription]
timeout_s = 0
""",
        )
        with (
            patch.dict(os.environ, {"MISTRAL_API_KEY": "test-key"}),
            pytest.raises(ConfigError, match="timeout_s"),
        ):
            load_config(config_path=cfg_file)

    def test_nonexistent_config_file_raises_config_error(self, tmp_path):
        nonexistent = tmp_path / "missing.toml"
        with (
            patch.dict(os.environ, {"MISTRAL_API_KEY": "test-key"}),
            pytest.raises(ConfigError, match="not found"),
        ):
            load_config(config_path=nonexistent)


# ─── Security ────────────────────────────────────────────────────────────────


class TestSecurityProperties:
    def test_api_key_not_in_repr(self, tmp_path):
        """API key must never appear in repr() output."""
        cfg_file = write_toml(tmp_path, "")
        with patch.dict(os.environ, {"MISTRAL_API_KEY": "sk-super-secret-key-12345"}):
            config = load_config(config_path=cfg_file)
        assert "sk-super-secret-key-12345" not in repr(config)

    def test_api_key_not_in_str(self, tmp_path):
        cfg_file = write_toml(tmp_path, "")
        with patch.dict(os.environ, {"MISTRAL_API_KEY": "sk-super-secret-key-12345"}):
            config = load_config(config_path=cfg_file)
        assert "sk-super-secret-key-12345" not in str(config)
