"""Record extraction fixtures from committed transcript fixtures.

This is an opt-in Phase 8 script. It requires:
- transcript fixtures under `tests/fixtures/transcripts/`
- `MISTRAL_API_KEY`
"""

from __future__ import annotations

import argparse
import os
from datetime import date
from pathlib import Path

from parler.extraction.extractor import DecisionExtractor
from parler.models import Transcript, TranscriptSegment
from parler.util.serialization import read_json, write_json_atomic

ROOT = Path(__file__).resolve().parent
TRANSCRIPTS_DIR = ROOT / "transcripts"
EXTRACTIONS_DIR = ROOT / "extractions"


def _transcript_from_dict(data: dict[str, object]) -> Transcript:
    segments = tuple(
        TranscriptSegment(
            id=int(item["id"]),  # type: ignore[index]
            start_s=float(item["start_s"]),  # type: ignore[index]
            end_s=float(item["end_s"]),  # type: ignore[index]
            text=str(item["text"]),  # type: ignore[index]
            language=str(item["language"]),  # type: ignore[index]
            speaker_id=item.get("speaker_id"),  # type: ignore[union-attr]
            speaker_confidence=item.get("speaker_confidence"),  # type: ignore[union-attr]
            confidence=float(item.get("confidence", 1.0)),  # type: ignore[union-attr]
            no_speech_prob=float(item.get("no_speech_prob", 0.0)),  # type: ignore[union-attr]
            code_switch=bool(item.get("code_switch", False)),  # type: ignore[union-attr]
            words=None,
        )
        for item in data.get("segments", [])  # type: ignore[union-attr]
    )
    return Transcript(
        text=str(data.get("text", "")),
        language=str(data.get("language", "")),
        duration_s=float(data.get("duration_s", 0.0)),
        segments=segments,
        detected_languages=tuple(data.get("detected_languages", [])),  # type: ignore[arg-type]
        model=str(data.get("model", "")),
        content_hash=str(data.get("content_hash", "")),
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Record extraction responses for transcript fixtures."
    )
    parser.add_argument("--fixture", help="Single transcript fixture stem to record.")
    parser.add_argument(
        "--model",
        default="mistral-large-latest",
        help="Extraction model to use.",
    )
    parser.add_argument(
        "--prompt-version",
        default="v1.0",
        help="Prompt version to use.",
    )
    args = parser.parse_args()

    api_key = os.environ.get("MISTRAL_API_KEY")
    if not api_key:
        raise SystemExit("MISTRAL_API_KEY is required")

    EXTRACTIONS_DIR.mkdir(parents=True, exist_ok=True)
    extractor = DecisionExtractor(
        api_key=api_key,
        model=args.model,
        prompt_version=args.prompt_version,
    )

    transcript_files = (
        [TRANSCRIPTS_DIR / f"{args.fixture}.json"]
        if args.fixture
        else sorted(TRANSCRIPTS_DIR.glob("*.json"))
    )
    for transcript_path in transcript_files:
        if not transcript_path.exists():
            raise SystemExit(f"Transcript fixture not found: {transcript_path}")
        raw = read_json(transcript_path)
        transcript_payload = raw.get("transcript", raw)
        transcript = _transcript_from_dict(transcript_payload)
        decision_log = extractor.extract(transcript, meeting_date=date(2026, 4, 9))
        write_json_atomic(EXTRACTIONS_DIR / transcript_path.name, decision_log)
        print(EXTRACTIONS_DIR / transcript_path.name)


if __name__ == "__main__":
    main()
