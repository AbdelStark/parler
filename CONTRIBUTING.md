# Contributing

`parler` uses [`uv`](https://docs.astral.sh/uv/) for Python version management, dependency resolution, locking, and command execution.

## Development setup

```bash
uv python install
uv sync --locked --group dev
```

This creates `.venv/` and installs the project in editable mode.

## Common commands

```bash
uv run pytest tests/unit/test_config_loading.py tests/unit/test_report_rendering.py tests/unit/test_pipeline_orchestration.py tests/unit/test_audio_ingestion.py tests/unit/test_chunk_assembly.py tests/unit/test_transcript_quality.py tests/unit/test_speaker_attribution.py tests/integration/test_retry_behavior.py tests/integration/test_voxtral_integration.py tests/integration/test_cache_behavior.py -q
uv run pytest tests/unit/test_decision_extraction_parsing.py tests/unit/test_deadline_resolution.py tests/unit/test_deadline_resolution_parametrized.py tests/integration/test_mistral_extraction.py tests/integration/test_export_integrations.py tests/property/test_deadline_resolver_properties.py tests/property/test_parsing_properties.py -q
uv run pytest tests/unit/test_cli_commands.py -q
uv run python tests/smoke_test.py
uv run ruff check parler tests/smoke_test.py
uv run ruff format --check parler tests/smoke_test.py
uv run mypy parler/
uv build
```

The wider `tests/`, `features/`, and roadmap modules are intentionally ahead of the currently implemented phases. Keep CI green by expanding the validated surface only when a phase is actually delivered; after the current Phase 7 core surface, the next major unfinished domains are full-system E2E/benchmark verification and fully provisioned fixtures.

## Contribution rules

- Keep changes traceable to `SPEC.md`, `SDD.md`, and `TESTING.md`.
- Prefer narrow, test-backed vertical slices over wide speculative scaffolding.
- Treat local caches and checkpoints as sensitive data; never commit them.
- Do not commit real audio, transcripts, secrets, or third-party API tokens.

## Releases

Releases are built and published with `uv` and GitHub Actions Trusted Publishing.
See `.github/workflows/publish.yml`.
