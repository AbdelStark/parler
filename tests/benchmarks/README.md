# Benchmark Baselines

Phase 8 benchmark runs are opt-in and should be recorded deliberately.

## Run locally

```bash
uv run pytest tests/benchmarks/test_performance.py --benchmark-only
uv run pytest tests/benchmarks/test_performance.py --benchmark-only \
  --benchmark-json /tmp/parler-benchmark-raw.json
uv run python tests/benchmarks/update_baseline.py \
  /tmp/parler-benchmark-raw.json \
  tests/benchmarks/baseline.json
```

For manual CI verification, use `.github/workflows/phase8-verification.yml` with
`run-benchmarks=true`. The workflow uploads both the committed baseline and the current run.

## Policy

- Save a fresh raw benchmark JSON and refresh the committed summary baseline when a performance-sensitive change lands.
- Commit the compact reviewed summary in `tests/benchmarks/baseline.json`, not the raw machine dump.
- Review deltas instead of treating a single machine reading as absolute truth.
- Use CI artifacts or local benchmark JSON as the review record.
- Keep benchmark baselines tied to synthetic data only.
