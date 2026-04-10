# Test Fixtures

Test data for the `parler` verification suite. All fixtures are synthetic. Never
record or commit real meeting audio, real transcripts, or secret-bearing artifacts.

## Directory layout

```text
fixtures/
├── audio/                          # Generated synthetic audio fixtures (.gitkeep by default)
├── transcripts/                    # Optional recorded Voxtral transcript fixtures (.gitkeep by default)
├── extractions/                    # Optional recorded extraction fixtures (.gitkeep by default)
├── decision_logs/                  # Committed expected DecisionLog baselines
│   ├── fr_meeting_5min_expected.json
│   └── bilingual_expected.json
├── generate_fixtures.py            # Synthetic audio + silence generator
├── record_voxtral.py               # Opt-in real API recorder for transcript fixtures
└── record_extraction.py            # Opt-in real API recorder for extraction fixtures
```

Fresh clones only contain the committed baselines, scripts, and placeholder directories.
Audio, transcript, and extraction fixtures are generated or recorded deliberately.

## Generating fixtures

### Synthetic audio fixtures

```bash
# Optional speech backend:
# - `uv add --dev gtts`
# - or make sure `say`/`espeak` and `ffmpeg` exist on PATH

# Generate the French meeting fixture
uv run python tests/fixtures/generate_fixtures.py --fixture fr

# Generate the bilingual fixture
uv run python tests/fixtures/generate_fixtures.py --bilingual

# Generate the long earnings-call fixture
uv run python tests/fixtures/generate_fixtures.py --earnings-call

# Generate the deterministic silence fixture (no TTS backend required)
uv run python tests/fixtures/generate_fixtures.py --silence

# Generate everything
uv run python tests/fixtures/generate_fixtures.py --all
```

The generator also writes `*.script.txt` manifests so the spoken source text stays
reviewable in git even when audio is not committed.

### Transcript fixtures (requires real API key, opt-in)

```bash
MISTRAL_API_KEY=sk-... uv run python tests/fixtures/record_voxtral.py
```

This records real Voxtral responses against synthetic audio into
`tests/fixtures/transcripts/*.json`.

### Extraction fixtures (requires real API key, opt-in)

```bash
MISTRAL_API_KEY=sk-... uv run python tests/fixtures/record_extraction.py
```

This records real extraction responses against synthetic transcripts into
`tests/fixtures/extractions/*.json`.

Recorded transcript and extraction fixtures are real vendor outputs generated from
synthetic audio. Review them before deciding whether to commit them.

## Data policy

- Audio fixtures are synthetic only.
- Text content is fictional business dialogue, not real meeting content.
- Names are fictional placeholders such as `Pierre`, `Sophie`, and `Alice`.
- Transcript and extraction fixtures, if recorded, must come only from synthetic audio.
- Secrets, caches, checkpoints, and user recordings are never valid fixture data.

## Content of `fr_meeting_5min`

A short French meeting about a product launch:

| Time | Speaker | Content |
|------|---------|---------|
| 0:00 | Pierre | Opens meeting, sets agenda |
| 0:30 | Sophie | Reports deployment status |
| 1:20 | Pierre | **Decision: launch on May 15** |
| 1:50 | Sophie | Confirms decision |
| 2:10 | Pierre | Assigns checklist review to Sophie |
| 2:30 | Sophie | **Commitment: checklist by next Friday** |
| 4:00 | Sophie | **Rejection: March launch not feasible** |
| 4:30 | Pierre | **Open question: database migration owner?** |

## Content of `bilingual_meeting_5min`

A short FR/EN code-switching meeting:

| Time | Speaker | Language | Content |
|------|---------|----------|---------|
| 0:00 | Pierre | FR | Opens in French |
| 0:30 | Pierre | FR→EN | Code-switch: Python SDK discussion |
| 1:10 | Alice | EN | English-only response |
| 1:50 | Pierre | EN | **Decision: adopt Python SDK** |
| 2:20 | Pierre | EN | Assigns migration guide to Alice |
| 2:50 | Alice | EN | **Commitment: guide by EOW** |

## Content of `earnings_call_45min`

A synthetic stress fixture that repeats a fictional investor-relations pattern:

- English-led earnings-call framing with French-compatible code paths
- repeated launch-date decisions (`May 15`) to test extraction stability
- repeated investor-deck commitments with a mix of explicit and relative deadlines
- enough dialogue turns to exercise multi-chunk transcription and assembly behavior

The exact spoken lines are written to `tests/fixtures/earnings_call_45min.mp3.script.txt`
whenever the fixture is generated.
