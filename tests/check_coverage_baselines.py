"""Enforce per-module line/branch coverage baselines from a coverage JSON report."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

BASELINES: dict[str, tuple[float, float]] = {
    "parler.audio.ingester": (81.0, 68.0),
    "parler.transcription.transcriber": (91.0, 79.0),
    "parler.attribution.attributor": (87.0, 82.0),
    "parler.extraction.extractor": (82.0, 63.0),
    "parler.extraction.deadline_resolver": (95.0, 86.0),
    "parler.rendering.renderer": (93.0, 88.0),
    "parler.pipeline.orchestrator": (96.0, 79.0),
    "parler.cli": (76.0, 59.0),
}


def _module_path(module: str) -> str:
    return f"{module.replace('.', '/')}.py"


def _load_report(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "report",
        nargs="?",
        default="coverage.json",
        type=Path,
        help="Path to a coverage.py JSON report.",
    )
    args = parser.parse_args(argv)

    report = _load_report(args.report)
    files = report.get("files")
    if not isinstance(files, dict):
        print(f"Invalid coverage report: missing files map in {args.report}", file=sys.stderr)
        return 1

    failures: list[str] = []
    for module, (line_floor, branch_floor) in BASELINES.items():
        module_report = files.get(_module_path(module))
        if not isinstance(module_report, dict):
            failures.append(f"{module}: missing from coverage report")
            continue

        summary = module_report.get("summary")
        if not isinstance(summary, dict):
            failures.append(f"{module}: missing summary in coverage report")
            continue

        line_pct = float(summary.get("percent_statements_covered", 0.0))
        branch_pct = float(summary.get("percent_branches_covered", 0.0))

        if line_pct < line_floor:
            failures.append(f"{module}: line coverage {line_pct:.1f}% < baseline {line_floor:.1f}%")
        if branch_pct < branch_floor:
            failures.append(
                f"{module}: branch coverage {branch_pct:.1f}% < baseline {branch_floor:.1f}%"
            )

    if failures:
        print("Coverage baseline check failed:", file=sys.stderr)
        for failure in failures:
            print(f"- {failure}", file=sys.stderr)
        return 1

    print(f"Coverage baselines satisfied for {len(BASELINES)} modules.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
