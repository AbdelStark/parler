"""Record Voxtral transcript fixtures from synthetic audio.

This is an opt-in Phase 8 script. It requires:
- synthetic audio already generated under `tests/fixtures/audio/`
- `MISTRAL_API_KEY`
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from parler.audio.ingester import AudioIngester
from parler.transcription.transcriber import VoxtralTranscriber
from parler.util.serialization import write_json_atomic

ROOT = Path(__file__).resolve().parent
AUDIO_DIR = ROOT / "audio"
TRANSCRIPTS_DIR = ROOT / "transcripts"

FIXTURE_LANGUAGES: dict[str, list[str]] = {
    "fr_meeting_5min.mp3": ["fr"],
    "bilingual_meeting_5min.mp3": ["fr", "en"],
    "earnings_call_45min.mp3": ["en", "fr"],
}


def main() -> None:
    parser = argparse.ArgumentParser(description="Record Voxtral responses for synthetic fixtures.")
    parser.add_argument("--fixture", help="Single fixture filename to record.")
    parser.add_argument(
        "--model",
        default="voxtral-mini-latest",
        help="Voxtral model to use.",
    )
    args = parser.parse_args()

    api_key = os.environ.get("MISTRAL_API_KEY")
    if not api_key:
        raise SystemExit("MISTRAL_API_KEY is required")

    TRANSCRIPTS_DIR.mkdir(parents=True, exist_ok=True)
    ingester = AudioIngester()
    transcriber = VoxtralTranscriber(api_key=api_key, model=args.model)

    fixture_names = [args.fixture] if args.fixture else sorted(FIXTURE_LANGUAGES)
    for name in fixture_names:
        audio_path = AUDIO_DIR / name
        if not audio_path.exists():
            raise SystemExit(f"Fixture audio not found: {audio_path}")
        audio_file = ingester.ingest(audio_path)
        transcript = transcriber.transcribe(audio_file, languages=FIXTURE_LANGUAGES.get(name))
        write_json_atomic(TRANSCRIPTS_DIR / f"{audio_path.stem}.json", transcript)
        print(TRANSCRIPTS_DIR / f"{audio_path.stem}.json")


if __name__ == "__main__":
    main()
