# Contributing

`parler` uses [`uv`](https://docs.astral.sh/uv/) for Python version management, dependency resolution, locking, and command execution.

## Development setup

```bash
uv python install
uv sync --locked --group dev
cp .env.example .env
```

This creates `.venv/`, installs the project in editable mode, and gives you a
local environment template for `MISTRAL_API_KEY`.

## Common commands

```bash
uv run pytest tests/unit/test_config_loading.py tests/unit/test_pipeline_config_compat.py tests/unit/test_cli_commands.py tests/unit/test_e2e_runner.py tests/unit/test_report_rendering.py tests/unit/test_pipeline_orchestration.py tests/unit/test_audio_ingestion.py tests/unit/test_chunk_assembly.py tests/unit/test_transcript_quality.py tests/unit/test_speaker_attribution.py tests/unit/test_decision_extraction_parsing.py tests/unit/test_deadline_resolution.py tests/unit/test_deadline_resolution_parametrized.py tests/integration/test_retry_behavior.py tests/integration/test_voxtral_integration.py tests/integration/test_cache_behavior.py tests/integration/test_mistral_extraction.py tests/integration/test_export_integrations.py tests/property/test_deadline_resolver_properties.py tests/property/test_parsing_properties.py -q
uv run pytest tests/unit/test_cli_commands.py -q
uv run pytest tests/benchmarks/test_performance.py -q -m benchmark
uv run pytest tests/benchmarks/test_performance.py -q -m benchmark --benchmark-json /tmp/parler-benchmark-raw.json
uv run python tests/benchmarks/update_baseline.py /tmp/parler-benchmark-raw.json tests/benchmarks/baseline.json
uv run python tests/fixtures/generate_fixtures.py --all
uv run parler-e2e
uv run parler-e2e tests/e2e/test_full_pipeline_fr.py -q
uv run parler tui
uv run python tests/smoke_test.py
uv run ruff check parler tests/smoke_test.py tests/unit/test_cli_commands.py tests/unit/test_e2e_runner.py tests/unit/test_pipeline_config_compat.py tests/fixtures/generate_fixtures.py tests/fixtures/record_voxtral.py tests/fixtures/record_extraction.py
uv run ruff format --check parler tests/smoke_test.py tests/unit/test_cli_commands.py tests/unit/test_e2e_runner.py tests/unit/test_pipeline_config_compat.py tests/fixtures/generate_fixtures.py tests/fixtures/record_voxtral.py tests/fixtures/record_extraction.py
uv run mypy parler/
uv build
```

The wider `tests/`, `features/`, and roadmap modules still contain future-facing coverage. Keep CI green by expanding the validated surface only when a phase is actually delivered. Phase 8 now adds the verification scaffold itself: legacy E2E config compatibility, fixture-generation scripts, benchmark baselines, and the manual `.github/workflows/phase8-verification.yml` workflow for live verification.

## Contribution rules

- Keep changes traceable to `SPEC.md`, `SDD.md`, and `TESTING.md`.
- Prefer narrow, test-backed vertical slices over wide speculative scaffolding.
- Treat local caches and checkpoints as sensitive data; never commit them.
- Do not commit real audio, transcripts, secrets, or third-party API tokens.

## Releases

Releases are built and published with `uv` and GitHub Actions Trusted Publishing.
See `.github/workflows/publish.yml` and update [CHANGELOG.md](./CHANGELOG.md)
for every user-visible change.
