# Skill Registry

Last updated: 2026-04-09

Observed existing repo-local skills before this pass: none.

| Skill | File | Triggers | Priority |
|---|---|---|---|
| Contract Reconciliation | `contract-reconciliation.md` | spec drift, import drift, canonical docs vs tests, RFC mismatch, dependency mismatch | Core |
| Vertical Slice Implementation | `vertical-slice-implementation.md` | create `parler/`, phase work, bootstrap modules, test-backed build-out | Core |
| Test-Driven Delivery | `test-driven-delivery.md` | pytest, BDD, property tests, coverage, mutation, benchmark, fixture use | Core |
| Mistral Pipeline | `mistral-pipeline.md` | Voxtral, Mistral, transcription, extraction, cache, retry, parser, quality | Core |
| Orchestrator and CLI | `orchestrator-and-cli.md` | `ProcessingState`, checkpoint, resume, exit codes, `parler process`, `parler cache` | Core |

## Activation Order

1. Use `contract-reconciliation.md` first when sources disagree.
2. Use `vertical-slice-implementation.md` when creating or extending runtime code.
3. Load a domain skill (`mistral-pipeline.md` or `orchestrator-and-cli.md`) for implementation details.
4. Load `test-driven-delivery.md` before widening verification or adding tests.

## Current Gap Analysis

High-priority gaps addressed in this pass:
- No repo-local guidance for spec-vs-test drift resolution
- No guidance for growing the missing `parler/` package from the phase plan
- No reusable workflow for the layered pytest/BDD/property/E2E suite
- No domain guidance for Mistral/Voxtral adapters, parser normalization, cache semantics, and checkpoint/CLI behavior

Lower-priority recommended skills not scaffolded yet:
- [ ] `rendering-and-export.md` — add when Phase 6 work begins in earnest
- [ ] `fixture-generation.md` — add if synthetic fixture generation becomes recurring work
- [ ] `release-ci.md` — add after `.github/workflows/` or release automation exists
- [ ] `security-review.md` — add before handling real transcript/checkpoint data

## Known Baseline Risks

- Phase 1 exists, but later domains are still missing: audio ingestion, transcription, attribution, extraction, exports, retry, and cache logic.
- E2E fixture assets listed in `tests/fixtures/README.md` are not committed yet.
- Tests and docs still drift on module names: `assembly` vs `assembler`, `attribution` vs `transcription.attributor`, and `util` vs `utils`.
- `PipelineConfig` compatibility is currently provided by aliasing `ParlerConfig`.
- Some runtime dependencies are implied by tests but not yet declared in `pyproject.toml` (`requests`, YAML parser) [inference].
- The current interpreter does not have `pytest` installed [verify], so formal test execution may require dependency bootstrapping first.
