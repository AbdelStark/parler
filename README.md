# parler

`parler` is a local-first Python CLI, library, and Textual TUI that turns
recorded multilingual meeting audio into a structured decision log with
transcription, speaker attribution, commitments, rejected options, and
render-ready output.

**Status as of 2026-04-10: alpha.** The end-to-end pipeline, CLI, cache/state
flows, and TUI are implemented and test-backed. It is suitable for local,
operator-driven analysis of recorded meetings and verification fixtures. It is
not yet suitable for unattended production ingestion, multi-tenant deployment,
or compliance-sensitive workflows. Known limitations are listed below.

## Why it exists

Most meeting tools stop at transcript or summary. `parler` is aimed at the
higher-value artifact: an explicit record of what was decided, what was
rejected, who owns follow-up, and when commitments are due, with strong support
for French and mixed French/English meetings.

## Who it is for

- Developers building decision-intelligence workflows on top of Mistral/Voxtral
- Operators who want a local CLI/TUI for replayable meeting analysis
- Researchers evaluating multilingual meeting intelligence on realistic fixtures

`parler` is currently a batch tool. It is not a long-running service.

## Quickstart

```bash
uv python install
uv sync --locked --group dev
cp .env.example .env
```

Edit `.env` and set `MISTRAL_API_KEY`. The CLI, TUI, and local E2E runner now
auto-load `.env` from the current project root.

Validate configuration and run a first fixture:

```bash
uv run parler config validate

uv run parler process \
  tests/fixtures/audio/fr_meeting_5min.mp3 \
  --lang fr \
  --participant Pierre \
  --participant Sophie \
  --meeting-date 2026-04-09
```

Launch the TUI:

```bash
uv run parler tui
```

Run the local live-E2E helper:

```bash
uv run parler-e2e
```

## Core workflows

Inspect the CLI surface:

```bash
uv run parler --help
```

Transcribe only:

```bash
uv run parler transcribe meeting.mp3 --format json --output transcript.json
```

Run the full pipeline:

```bash
uv run parler process meeting.mp3 --lang fr,en --output meeting-decisions.md
```

Render from an existing checkpoint without re-calling APIs:

```bash
uv run parler report --from-state .parler-state.json --format html --output report.html
```

Re-extract from a saved checkpoint:

```bash
uv run parler extract --from-state .parler-state.json --format json
```

Inspect local cache entries:

```bash
uv run parler cache list
uv run parler cache show <key>
uv run parler cache clear --yes
```

Estimate spend before the first billable stage:

```bash
uv run parler process meeting.mp3 --cost-estimate
```

The cost estimate is intentionally conservative. It uses current built-in price
assumptions for the supported Mistral/Voxtral models and the configured
extraction limits. Treat it as a safety rail, not as billing truth.

## What the repository contains

The canonical product, design, testing, and delivery contracts live in:

- [SPEC.md](./SPEC.md)
- [SDD.md](./SDD.md)
- [TESTING.md](./TESTING.md)
- [IMPLEMENTATION_PLAN.md](./IMPLEMENTATION_PLAN.md)

Historical component records remain in [rfcs/](./rfcs/), but `SPEC.md` and
`SDD.md` win when documents drift.

## Architecture overview

The runtime package lives under [`parler/`](./parler):

- `audio/`: input validation, probing, and FFmpeg-backed normalization
- `transcription/`: Voxtral adapter, chunk assembly, quality checks, transcript cache
- `attribution/`: speaker resolution and anonymization rules
- `extraction/`: deadline resolution, parsing, cache, and Mistral extraction adapter
- `rendering/`: canonical Markdown/HTML/JSON report generation
- `export/`: isolated Notion/Linear/Jira/Slack adapter surfaces
- `pipeline/`: orchestration, checkpointing, resumability, and cost gating
- `tui/`: the Textual showcase application
- `util/`: hashing, serialization, env loading, retry, and language helpers

High-level flow:

1. Ingest audio and normalize format if needed
2. Transcribe via Voxtral
3. Resolve speakers conservatively
4. Extract structured decisions and commitments
5. Render Markdown/HTML/JSON
6. Optionally export to downstream systems

## Testing and verification

Fast local verification:

```bash
uv run pytest tests/unit tests/integration tests/property -q
uv run ruff check parler tests/smoke_test.py
uv run ruff format --check parler tests/smoke_test.py
uv run mypy parler/
uv build
```

Targeted project-backed slice used in CI:

```bash
uv run pytest \
  tests/unit/test_config_loading.py \
  tests/unit/test_pipeline_config_compat.py \
  tests/unit/test_cli_commands.py \
  tests/unit/test_e2e_runner.py \
  tests/unit/test_report_rendering.py \
  tests/unit/test_pipeline_orchestration.py \
  tests/unit/test_audio_ingestion.py \
  tests/unit/test_chunk_assembly.py \
  tests/unit/test_transcript_quality.py \
  tests/unit/test_speaker_attribution.py \
  tests/unit/test_decision_extraction_parsing.py \
  tests/unit/test_deadline_resolution.py \
  tests/unit/test_deadline_resolution_parametrized.py \
  tests/integration/test_retry_behavior.py \
  tests/integration/test_voxtral_integration.py \
  tests/integration/test_cache_behavior.py \
  tests/integration/test_mistral_extraction.py \
  tests/integration/test_export_integrations.py \
  tests/property/test_deadline_resolver_properties.py \
  tests/property/test_parsing_properties.py \
  -q
```

For detailed test-layer guidance, see [tests/README.md](./tests/README.md) and
[TESTING.md](./TESTING.md).

## Fixtures and dataset provenance

Deterministic golden tests still rely on synthetic fixtures generated from
scripted content under [`tests/fixtures/`](./tests/fixtures).

The repository also includes a small set of public French clips derived from the
[VoxPopuli](https://github.com/facebookresearch/voxpopuli) dataset
(*VoxPopuli: A Large-Scale Multilingual Speech Corpus for Representation
Learning, Semi-Supervised Learning and Interpretation*). The original source
recording and derived clip metadata are kept under
[`tests/fixtures/audio/`](./tests/fixtures/audio/). These clips are present for
manual demos and TUI workflows, not as deterministic contract goldens.

## Security and local-data handling

- `.env`, caches, and checkpoints are local-only artifacts and are ignored by git
- checkpoints may contain transcript text and decision content; treat them as sensitive
- checkpoint/cache JSON writes use restrictive permissions where the host OS allows
- resume now rejects incomplete or mismatched checkpoints instead of continuing in an incoherent state
- no real customer recordings, transcripts, or secrets should be committed to the repository

## Current limitations

- The project is still alpha and uses live vendor APIs for the transcription and extraction path
- Cost estimation is conservative and model-price dependent; verify pricing before relying on it operationally
- Export adapters are thin integration surfaces, not full sync engines
- There is no deployment/runbook surface for multi-user or server operation because this is not yet a service
- FFmpeg-backed normalization uses temporary local artifacts; sustained cleanup policy is still minimal
- CI validates the implemented slice, not every future-facing draft in `tests/` and `features/`

## Contributing

See [CONTRIBUTING.md](./CONTRIBUTING.md). In short:

- use `uv`
- keep changes traceable to `SPEC.md`, `SDD.md`, and `TESTING.md`
- prefer narrow, test-backed vertical slices
- do not commit secrets, real recordings, caches, or checkpoints

## Help and project status

- Issues: <https://github.com/AbdelStark/parler/issues>
- Changelog: [CHANGELOG.md](./CHANGELOG.md)
- Release automation: [`.github/workflows/publish.yml`](./.github/workflows/publish.yml)

## License

MIT
