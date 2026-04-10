# Changelog

All notable user-visible changes to `parler` will be documented in this file.

The format is based on Keep a Changelog. Version numbers follow the project tag
history.

## [Unreleased]

### Added

- A first-class Textual TUI (`parler tui` / `parler-tui`) with fixture presets,
  live stage progress, cache browsing, transcript/report preview, and result
  tables.
- A committed VoxPopuli-derived French fixture set for manual demos and TUI
  workflows, with provenance notes in `tests/fixtures/audio/`.
- Automatic `.env` loading for the TUI and local E2E runner.
- `.env.example` for local setup.
- `parler doctor` for local readiness checks, `parler runs {list,show}` for
  inspecting `.parler-runs/`, and `parler cleanup` for pruning stale local run
  bundles and normalized temp audio.
- Per-run local trace bundles under `.parler-runs/<trace_id>/` with `run.json`
  summaries and `events.jsonl` stage streams for `process`, `transcribe`, and
  TUI-driven runs.

### Changed

- Checkpoint payloads now include serialized audio metadata in addition to the
  audio hash.
- Resume validation now rejects incomplete or internally inconsistent
  checkpoints instead of failing later with assertion-style errors.
- Local checkpoint and cache JSON writes now use restrictive permissions where
  the host platform allows them.
- Audio normalization writes converted files into a dedicated temporary area
  instead of polluting the input directory.
- `--cost-estimate` and the orchestrator cost gate now use a conservative
  preflight estimator instead of the previous `0.0` stub, and `cost.max_usd`
  is enforced before the first billable stage.
- Normalized temp-audio cleanup and run-artifact retention are now explicit
  operator workflows instead of ad hoc filesystem cleanup.

### Fixed

- TUI setup copy now explains that API keys can come from `.env`,
  `MISTRAL_API_KEY`, `PARLER_API_KEY`, or `parler.toml`.
- FFmpeg / ffprobe failures are now surfaced as typed user-facing errors rather
  than raw subprocess exceptions.

## [0.1.0] - 2026-04-10

### Added

- Initial alpha release of the `parler` runtime package.
- Local-first audio ingestion, Voxtral transcription, speaker attribution,
  decision extraction, rendering, export adapters, checkpointing, cache
  management, and CLI commands.
- Fast unit/integration/property verification slice, opt-in live E2E runner,
  benchmark baseline flow, and GitHub Actions CI/publish workflows.
