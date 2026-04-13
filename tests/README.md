# Test Specifications

TDD test specifications for `parler`. These define expected behavior at unit,
integration, property, and E2E levels and are implemented with `pytest`.

The canonical contract for these tests is now:

- [`SPEC.md`](../SPEC.md)
- [`SDD.md`](../SDD.md)
- [`TESTING.md`](../TESTING.md)

Where an individual draft test reflects an older assumption, the canonical documents
win and the test should be updated as part of the same change.

## Structure

```
tests/
├── README.md                     (this file)
├── conftest.py                   (shared fixtures — see fixtures/)
├── fixtures/                     (test data and mock responses)
│   ├── audio/                    (generated synthetic audio fixtures)
│   ├── transcripts/              (optional recorded Voxtral responses)
│   ├── extractions/              (optional recorded extraction responses)
│   └── decision_logs/            (expected decision log outputs)
├── unit/                         (pure function tests, no API calls)
│   ├── test_audio_ingestion.py
│   ├── test_chunk_assembly.py
│   ├── test_cli_commands.py
│   ├── test_e2e_runner.py
│   ├── test_pipeline_config_compat.py
│   ├── test_deadline_resolution.py
│   ├── test_decision_extraction_parsing.py
│   ├── test_speaker_attribution.py
│   ├── test_transcript_quality.py
│   ├── test_report_rendering.py
│   └── test_config_loading.py
├── integration/                  (tests that mock external APIs)
│   ├── test_voxtral_integration.py
│   ├── test_mistral_extraction.py
│   ├── test_retry_behavior.py
│   ├── test_cache_behavior.py
│   └── test_export_integrations.py
├── e2e/                          (real API calls, marked @slow)
│   ├── test_full_pipeline_fr.py
│   ├── test_full_pipeline_bilingual.py
│   └── test_earnings_call.py
└── benchmarks/                   (opt-in performance budgets and baseline)
    ├── test_performance.py
    ├── update_baseline.py
    └── baseline.json
```

## Running tests

```bash
# All unit tests (fast, no API)
uv run pytest tests/unit/ -v

# All integration tests (mocked API)
uv run pytest tests/integration/ -v

# All BDD scenarios (mocked API)
uv run pytest features/ -v

# Everything except E2E (CI default)
uv run pytest tests/unit tests/integration tests/property -v --tb=short

# E2E only (requires MISTRAL_API_KEY; spend depends on selected models)
uv run pytest tests/e2e/ -v -s

# Convenient local E2E runner
uv run parler-e2e
uv run parler-e2e tests/e2e/test_full_pipeline_fr.py -q

# Benchmarks
uv run pytest tests/benchmarks/test_performance.py -q -m benchmark
uv run pytest tests/benchmarks/test_performance.py -q -m benchmark \
  --benchmark-json /tmp/parler-benchmark-raw.json
uv run python tests/benchmarks/update_baseline.py \
  /tmp/parler-benchmark-raw.json \
  tests/benchmarks/baseline.json
```

For fresh clones, generate synthetic audio before live E2E runs:

```bash
uv run python tests/fixtures/generate_fixtures.py --all
```

## Coverage targets

| Module | Line coverage | Branch coverage |
|--------|-------------|----------------|
| `parler.audio.ingester` | ≥ 95% | ≥ 90% |
| `parler.transcription.transcriber` | ≥ 90% | ≥ 85% |
| `parler.attribution.attributor` | ≥ 90% | ≥ 80% |
| `parler.extraction.extractor` | ≥ 95% | ≥ 90% |
| `parler.extraction.deadline_resolver` | ≥ 98% | ≥ 95% |
| `parler.rendering.renderer` | ≥ 90% | ≥ 85% |
| `parler.pipeline.orchestrator` | ≥ 85% | ≥ 80% |
| `parler.cli` | ≥ 85% | ≥ 75% |

CI currently enforces non-regression baselines for these modules via
`tests/check_coverage_baselines.py`. Raise those baselines as coverage improves;
the table above remains the longer-term target.

## Test data policy

- Deterministic contract fixtures are synthetic unless a test explicitly states otherwise
- The repository also includes a small set of public VoxPopuli-derived French audio clips for manual demos and TUI workflows
- Transcripts in `fixtures/transcripts/` are optional real Voxtral responses recorded against approved fixture audio only
- No real personal data or private meeting recordings belong in any test fixture
- Fresh clones contain committed baselines, synthetic audio fixtures, and the public VoxPopuli-derived clips; live vendor recordings remain opt-in
