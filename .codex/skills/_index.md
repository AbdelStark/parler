# Skill Registry

Last updated: 2026-04-09

Observed existing repo-local skills before this pass: none.

| Skill | File | Triggers | Priority |
|---|---|---|---|
| Contract Reconciliation | `contract-reconciliation.md` | spec drift, import drift, canonical docs vs tests, RFC mismatch, dependency mismatch | Core |
| Vertical Slice Implementation | `vertical-slice-implementation.md` | create `parler/`, phase work, bootstrap modules, test-backed build-out | Core |
| Test-Driven Delivery | `test-driven-delivery.md` | pytest, BDD, property tests, coverage, mutation, benchmark, fixture use | Core |
| Fixture Generation | `fixture-generation.md` | synthetic audio, E2E fixtures, benchmark baseline, live verification assets | Core |
| Mistral Pipeline | `mistral-pipeline.md` | Voxtral, Mistral, transcription, extraction, cache, retry, parser, quality | Core |
| Rendering and Export | `rendering-and-export.md` | report rendering, html, markdown, json, notion, linear, jira, slack | Core |
| Orchestrator and CLI | `orchestrator-and-cli.md` | `ProcessingState`, checkpoint, resume, exit codes, `parler process`, `parler cache` | Core |

## Activation Order

1. Use `contract-reconciliation.md` first when sources disagree.
2. Use `vertical-slice-implementation.md` when creating or extending runtime code.
3. Load `fixture-generation.md` for synthetic asset creation, vendor recording, or benchmark-baseline work.
4. Load a domain skill (`mistral-pipeline.md`, `rendering-and-export.md`, or `orchestrator-and-cli.md`) for implementation details.
5. Load `test-driven-delivery.md` before widening verification or adding tests.

## Current Gap Analysis

High-priority gaps addressed in this pass:
- No repo-local guidance for spec-vs-test drift resolution
- No guidance for growing the runtime package from the phase plan
- No reusable workflow for the layered pytest/BDD/property/E2E suite
- No domain guidance for Mistral/Voxtral adapters, parser normalization, cache semantics, and checkpoint/CLI behavior
- No release/packaging baseline for `uv`-based development and publishing

Lower-priority recommended skills not scaffolded yet:
- [ ] `security-review.md` — add before handling real transcript/checkpoint data

## Known Baseline Risks

- Phase 1 through Phase 7 core runtime surfaces exist and the Phase 8 verification scaffold is now present. The remaining later domains are live fixture provisioning, reviewed vendor-output commits if desired, and any CLI/export edge-case wiring uncovered by those higher-level runs.
- Fresh clones still may not contain generated audio or recorded vendor outputs even though the fixture scripts, placeholder directories, and decision-log baselines are committed.
- Tests and docs still drift on module names: `assembly` vs `assembler` and `util` vs `utils`; `attribution` vs `transcription.attributor` is now covered by a compatibility shim.
- `ruff check tests/` still surfaces a wider backlog in untouched benchmark/E2E/integration files; current CI/fast verification intentionally stays scoped to `parler` plus `tests/smoke_test.py`.
- `PipelineConfig` compatibility is provided by a dedicated legacy wrapper that normalizes into `ParlerConfig`.
- `uv`, `uv_build`, `PyYAML`, and `requests` are now declared and validated; keep future tooling changes inside the same packaging model.
- The implemented fast slice, compatibility layer, and benchmarks are green locally; widen CI deliberately rather than pointing it at the whole `tests/` tree.
