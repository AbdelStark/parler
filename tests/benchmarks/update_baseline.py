"""Condense pytest-benchmark JSON into a reviewable committed baseline."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path


def _to_ns(value: object) -> int:
    return round(float(value) * 1_000_000_000)


def build_summary(raw: dict[str, object]) -> dict[str, object]:
    machine_info = raw.get("machine_info", {})
    commit_info = raw.get("commit_info", {})
    benchmarks = raw.get("benchmarks", [])

    summary_benchmarks = []
    for benchmark in benchmarks:
        entry = benchmark if isinstance(benchmark, dict) else {}
        stats = entry.get("stats", {})
        stats_dict = stats if isinstance(stats, dict) else {}
        summary_benchmarks.append(
            {
                "name": entry.get("name"),
                "group": entry.get("group"),
                "mean_ns": _to_ns(stats_dict.get("mean", 0)),
                "median_ns": _to_ns(stats_dict.get("median", 0)),
                "min_ns": _to_ns(stats_dict.get("min", 0)),
                "max_ns": _to_ns(stats_dict.get("max", 0)),
                "ops": round(float(stats_dict.get("ops", 0.0)), 4),
                "rounds": int(stats_dict.get("rounds", 0)),
                "iterations": int(stats_dict.get("iterations", 0)),
            }
        )

    summary_benchmarks.sort(key=lambda item: str(item["name"]))

    return {
        "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "source": "pytest-benchmark summary",
        "python_version": machine_info.get("python_version"),
        "system": machine_info.get("system"),
        "machine": machine_info.get("machine"),
        "commit": commit_info.get("id"),
        "benchmarks": summary_benchmarks,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Create a compact benchmark baseline summary.")
    parser.add_argument("raw_json", type=Path, help="Raw pytest-benchmark JSON input")
    parser.add_argument("output_json", type=Path, help="Summary baseline output path")
    args = parser.parse_args()

    raw = json.loads(args.raw_json.read_text(encoding="utf-8"))
    summary = build_summary(raw)
    args.output_json.write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":
    main()
