"""Generate synthetic fixture assets for opt-in Phase 8 verification.

The generated content is always synthetic and repo-safe. The script prefers:

- `gtts` for direct MP3 generation
- macOS `say` + `ffmpeg`
- `espeak` + `ffmpeg`

It also creates a deterministic 30-second silence WAV fixture with only the
stdlib, so at least one fixture path works without any speech backend.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import wave
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent
AUDIO_DIR = ROOT / "audio"
TRANSCRIPTS_DIR = ROOT / "transcripts"
EXTRACTIONS_DIR = ROOT / "extractions"
DECISION_LOGS_DIR = ROOT / "decision_logs"
SILENCE_OUTPUT = "silence_30s.wav"


@dataclass(frozen=True)
class FixtureSpec:
    name: str
    language_hint: str
    output_name: str
    script_lines: tuple[str, ...]


def _build_earnings_call_script() -> tuple[str, ...]:
    metrics = (
        ("ARR", "April twenty second", "May fifteen"),
        ("gross margin", "next Friday", "May fifteen"),
        ("pipeline conversion", "April twenty ninth", "May fifteen"),
        ("renewal rate", "end of month", "May fifteen"),
        ("free cash flow", "May sixth", "May fifteen"),
        ("operating cash flow", "next Wednesday", "May fifteen"),
        ("net retention", "May thirteenth", "May fifteen"),
        ("EBITDA", "May twentieth", "May fifteen"),
    )

    lines: list[str] = []
    for index, (metric, deadline, launch_date) in enumerate(metrics, start=1):
        lines.extend(
            [
                f"Pierre. Welcome back to the quarterly earnings call block {index}.",
                (
                    f"Sophie. Revenue growth remains strong, {metric} improved, "
                    f"and Q {index + 1} demand is holding up."
                ),
                (
                    "Analyst one. Can you comment on margin targets for the next quarter "
                    "and the sequencing of the launch plan?"
                ),
                f"Pierre. We will keep the current guidance and review it again in block {index}.",
                (
                    f"Sophie. I will send the updated investor deck and the investor FAQ by {deadline}."
                ),
                (
                    "Analyst two. Are you changing the launch date for the new platform, "
                    "or are you still committed to the current window?"
                ),
                f"Pierre. No. The launch remains on {launch_date} and that decision stands.",
            ]
        )
    return tuple(lines)


FRENCH_MEETING = FixtureSpec(
    name="fr",
    language_hint="fr",
    output_name="fr_meeting_5min.mp3",
    script_lines=(
        "Pierre. Bonjour a tous. On commence la reunion de lancement produit.",
        "Sophie. Le deploiement est pret. Il reste juste les derniers tests.",
        "Pierre. Tres bien. On part sur le quinze mai pour le lancement. C'est decide.",
        "Sophie. D'accord. Je note le lancement le quinze mai.",
        "Pierre. Sophie, peux-tu revoir la checklist de deploiement ?",
        "Sophie. Oui. Je vais revoir la checklist avant vendredi prochain.",
        "Pierre. Nous ne pouvons pas viser un lancement en mars.",
        "Sophie. Non. Ce n'est pas faisable avec la capacite actuelle de l'equipe.",
        "Pierre. Qui prend la migration de la base de donnees ?",
        "Sophie. On decidera du proprietaire apres la reunion.",
    ),
)

BILINGUAL_MEETING = FixtureSpec(
    name="bilingual",
    language_hint="fr,en",
    output_name="bilingual_meeting_5min.mp3",
    script_lines=(
        "Pierre. Bonjour, on commence la reunion.",
        "Pierre. So, regarding the Python SDK, je pense qu'on devrait l'adopter.",
        "Alice. Agreed. The SDK approach is cleaner than direct API calls.",
        "Pierre. On a decide. We are going with the Python SDK approach.",
        "Pierre. Alice, can you prepare the migration guide by E O W ?",
        "Alice. Sure. I'll have it ready by Friday.",
    ),
)

EARNINGS_CALL = FixtureSpec(
    name="earnings-call",
    language_hint="en,fr",
    output_name="earnings_call_45min.mp3",
    script_lines=_build_earnings_call_script(),
)

FIXTURES = {
    "fr": FRENCH_MEETING,
    "bilingual": BILINGUAL_MEETING,
    "earnings-call": EARNINGS_CALL,
}


def _ensure_dirs() -> None:
    for path in (AUDIO_DIR, TRANSCRIPTS_DIR, EXTRACTIONS_DIR, DECISION_LOGS_DIR):
        path.mkdir(parents=True, exist_ok=True)


def _write_script_manifest(spec: FixtureSpec) -> None:
    manifest_path = ROOT / f"{spec.output_name}.script.txt"
    manifest_path.write_text("\n".join(spec.script_lines) + "\n", encoding="utf-8")


def _require_command(name: str) -> str | None:
    return shutil.which(name)


def _select_tts_language(spec: FixtureSpec) -> str:
    return "fr" if spec.language_hint.startswith("fr") else "en"


def _generate_with_gtts(spec: FixtureSpec, output_path: Path) -> bool:
    try:
        from gtts import gTTS  # type: ignore[import-not-found]
    except ImportError:
        return False
    tts = gTTS(text=" ".join(spec.script_lines), lang=_select_tts_language(spec))
    tts.save(str(output_path))
    return True


def _generate_with_say(spec: FixtureSpec, output_path: Path) -> bool:
    say = _require_command("say")
    ffmpeg = _require_command("ffmpeg")
    if say is None or ffmpeg is None:
        return False
    intermediate = output_path.with_suffix(".aiff")
    subprocess.run([say, "-o", str(intermediate), " ".join(spec.script_lines)], check=True)
    subprocess.run(
        [ffmpeg, "-y", "-i", str(intermediate), str(output_path)],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    intermediate.unlink(missing_ok=True)
    return True


def _generate_with_espeak(spec: FixtureSpec, output_path: Path) -> bool:
    espeak = _require_command("espeak")
    ffmpeg = _require_command("ffmpeg")
    if espeak is None or ffmpeg is None:
        return False
    intermediate = output_path.with_suffix(".wav")
    subprocess.run(
        [espeak, "-w", str(intermediate), " ".join(spec.script_lines)],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    subprocess.run(
        [ffmpeg, "-y", "-i", str(intermediate), str(output_path)],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    intermediate.unlink(missing_ok=True)
    return True


def generate_audio(spec: FixtureSpec, output_path: Path) -> None:
    if _generate_with_gtts(spec, output_path):
        return
    if _generate_with_say(spec, output_path):
        return
    if _generate_with_espeak(spec, output_path):
        return
    raise RuntimeError(
        "No supported TTS backend available. Install `gtts`, or ensure `say`/`espeak` and `ffmpeg` exist."
    )


def generate_silence(output_path: Path, *, duration_s: int = 30, sample_rate: int = 16_000) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    frame_count = duration_s * sample_rate
    with wave.open(str(output_path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(b"\x00\x00" * frame_count)


def _write_regression_placeholders() -> None:
    bilingual_expected = DECISION_LOGS_DIR / "bilingual_expected.json"
    if not bilingual_expected.exists():
        bilingual_expected.write_text(
            json.dumps(
                {
                    "_comment": "Expected DecisionLog baseline for bilingual_meeting_5min.mp3.",
                    "_generated": "2026-04-09",
                    "_fixture_audio": "bilingual_meeting_5min.mp3",
                    "decisions": [
                        {
                            "id": "D1",
                            "summary": "Adopt the Python SDK approach",
                            "timestamp_s": 40.0,
                            "speaker": "Pierre",
                            "confirmed_by": ["Alice"],
                            "quote": "We are going with the Python SDK approach.",
                            "confidence": "high",
                            "language": "en",
                        }
                    ],
                    "commitments": [
                        {
                            "id": "C1",
                            "owner": "Alice",
                            "action": "Prepare the migration guide",
                            "deadline": {
                                "raw": "EOW",
                                "resolved_date": "2026-04-11",
                                "is_explicit": False,
                            },
                            "timestamp_s": 65.0,
                            "quote": "I'll have it ready by Friday.",
                            "confidence": "high",
                            "language": "en",
                        }
                    ],
                    "rejected": [],
                    "open_questions": [],
                },
                indent=2,
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate synthetic parler fixture audio.")
    parser.add_argument(
        "--fixture",
        choices=[*sorted(FIXTURES), "silence"],
        action="append",
        help="Fixture key to generate. Repeat for multiple fixtures.",
    )
    parser.add_argument("--all", action="store_true", help="Generate every synthetic fixture.")
    parser.add_argument(
        "--bilingual", action="store_true", help="Shortcut for --fixture bilingual."
    )
    parser.add_argument(
        "--earnings-call",
        action="store_true",
        help="Shortcut for --fixture earnings-call.",
    )
    parser.add_argument("--silence", action="store_true", help="Shortcut for --fixture silence.")
    parser.add_argument(
        "--output",
        type=Path,
        help="Explicit output path for single-fixture generation.",
    )
    args = parser.parse_args()

    _ensure_dirs()
    _write_regression_placeholders()

    requested = list(args.fixture or [])
    if args.all:
        requested.extend([*sorted(FIXTURES), "silence"])
    if args.bilingual:
        requested.append("bilingual")
    if args.earnings_call:
        requested.append("earnings-call")
    if args.silence:
        requested.append("silence")
    if not requested:
        requested = ["fr"]

    ordered = list(dict.fromkeys(requested))

    if args.output is not None and len(ordered) != 1:
        raise SystemExit("--output can only be used when generating exactly one fixture")

    for key in ordered:
        if key == "silence":
            target = args.output if args.output is not None else AUDIO_DIR / SILENCE_OUTPUT
            generate_silence(target)
            print(target)
            continue

        spec = FIXTURES[key]
        target = args.output if args.output is not None else AUDIO_DIR / spec.output_name
        _write_script_manifest(spec)
        generate_audio(spec, target)
        print(target)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
