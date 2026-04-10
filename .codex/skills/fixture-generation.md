---
name: fixture-generation
description: Generate and manage synthetic verification fixtures for Phase 8 work. Use when touching `tests/fixtures/`, preparing E2E runs, recording vendor outputs against synthetic audio, or updating benchmark baselines and live verification workflow assets.
prerequisites: uv, pytest, synthetic-only fixture policy, optional gtts or say/espeak+ffmpeg for speech audio
---

# Fixture Generation

<purpose>
Use this skill when work crosses from pure runtime code into verification assets. It keeps E2E and benchmark preparation deliberate, synthetic-only, and reviewable.
</purpose>

<context>
- `tests/fixtures/generate_fixtures.py` creates synthetic audio and a deterministic silence fixture.
- `tests/fixtures/record_voxtral.py` and `tests/fixtures/record_extraction.py` are opt-in scripts for real vendor outputs generated from synthetic inputs only.
- `tests/benchmarks/baseline.json` is the reviewed benchmark reference; `.github/workflows/phase8-verification.yml` is the manual live verification entrypoint.
- Fresh clones may only contain placeholder directories plus committed decision-log baselines.
</context>

<procedure>
1. Confirm the task is synthetic-fixture or verification-asset work, not product runtime logic.
2. Check `tests/fixtures/README.md` and the relevant E2E or benchmark contract before generating anything.
3. For audio fixture work:
   - use `uv run python tests/fixtures/generate_fixtures.py --fixture <name>`
   - use `--all` only when you actually need the whole set
4. For live transcript or extraction fixture recording:
   - confirm `MISTRAL_API_KEY` is set
   - generate synthetic audio first
   - run the recorder script against a bounded fixture set
5. Review generated artifacts before committing them.
6. For benchmark work:
   - run `uv run pytest tests/benchmarks/test_performance.py -q -m benchmark --benchmark-json /tmp/parler-benchmark-raw.json`
   - condense it with `uv run python tests/benchmarks/update_baseline.py /tmp/parler-benchmark-raw.json tests/benchmarks/baseline.json`
   - treat the committed summary as a reviewed baseline, not an absolute truth
7. Update docs/context when fixture policy or verification workflow changes.
</procedure>

<patterns>
<do>
  - Keep fixture source text synthetic and reviewable via `*.script.txt` manifests.
  - Commit baselines and scripts deliberately; leave ad hoc caches and checkpoints out of git.
  - Use placeholder directories plus clear docs when binary assets are intentionally absent from a fresh clone.
</do>
<dont>
  - Don't commit real meetings, real transcripts, or API-key-bearing artifacts -> use synthetic inputs only.
  - Don't widen CI to live E2E or benchmarks by default -> keep those behind manual or scheduled opt-in gates.
  - Don't treat one benchmark JSON from one machine as a universal performance truth -> review trends, not single numbers.
</dont>
</patterns>

<examples>
Example: prepare the synthetic fixture set

```bash
uv run python tests/fixtures/generate_fixtures.py --all
```

Example: refresh the benchmark baseline

```bash
uv run pytest tests/benchmarks/test_performance.py -q -m benchmark \
  --benchmark-json /tmp/parler-benchmark-raw.json
uv run python tests/benchmarks/update_baseline.py \
  /tmp/parler-benchmark-raw.json \
  tests/benchmarks/baseline.json
```
</examples>

<troubleshooting>
| Symptom | Cause | Fix |
|---|---|---|
| `No supported TTS backend available` | neither `gtts` nor `say`/`espeak`+`ffmpeg` is available | install `gtts` or provide a supported local speech backend |
| E2E skips immediately | `MISTRAL_API_KEY` is missing | export the key and rerun only the needed fixture |
| Benchmark JSON changed unexpectedly | machine/load differences or real performance drift | rerun locally, compare to the committed baseline, and review before committing |
</troubleshooting>

<references>
- `tests/fixtures/README.md`: fixture policy and commands
- `tests/fixtures/generate_fixtures.py`: synthetic audio generator
- `tests/fixtures/record_voxtral.py`: transcript recorder
- `tests/fixtures/record_extraction.py`: extraction recorder
- `tests/benchmarks/README.md`: benchmark baseline policy
- `.github/workflows/phase8-verification.yml`: manual live verification workflow
</references>
