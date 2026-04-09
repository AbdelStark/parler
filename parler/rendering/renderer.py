"""Render canonical decision logs into output formats."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from html import escape
import json

from ..models import Commitment, Decision, DecisionLog, OpenQuestion, Rejection
from ..util.serialization import to_jsonable


class OutputFormat(str, Enum):
    MARKDOWN = "markdown"
    HTML = "html"
    JSON = "json"
    TERMINAL = "terminal"


@dataclass(frozen=True)
class RenderConfig:
    format: OutputFormat = OutputFormat.MARKDOWN


def _format_timestamp(value: float | None) -> str:
    if value is None:
        return "-"
    minutes, seconds = divmod(int(value), 60)
    return f"{minutes:02d}:{seconds:02d}"


def _format_deadline(commitment: Commitment) -> str:
    if commitment.deadline is None:
        return "-"
    if commitment.deadline.resolved_date is not None:
        return commitment.deadline.resolved_date.isoformat()
    return commitment.deadline.raw or "-"


class ReportRenderer:
    """Minimal but valid renderer for the canonical decision log."""

    def render(self, decision_log: DecisionLog, config: RenderConfig) -> str:
        fmt = config.format if isinstance(config.format, OutputFormat) else OutputFormat(str(config.format))
        if fmt == OutputFormat.JSON:
            return json.dumps(to_jsonable(decision_log), ensure_ascii=False, indent=2)
        if fmt == OutputFormat.HTML:
            return self._render_html(decision_log)
        return self._render_markdown(decision_log)

    def _render_markdown(self, decision_log: DecisionLog) -> str:
        lines = ["# Decision Log", ""]
        if decision_log.is_empty:
            lines.extend(["No decisions recorded.", ""])
        lines.extend(
            [
                "## Decisions",
                "",
                "| ID | Summary | Owner | Timestamp | Confidence |",
                "| --- | --- | --- | --- | --- |",
            ]
        )
        if decision_log.decisions:
            for item in decision_log.decisions:
                lines.append(
                    f"| {item.id} | {item.summary} | {item.speaker or '-'} | "
                    f"{_format_timestamp(item.timestamp_s)} | {item.confidence} |"
                )
                if item.quote:
                    lines.append(f"> {item.quote}")
        else:
            lines.append("| - | No decisions recorded | - | - | - |")
        lines.extend(["", "## Commitments", "", "| ID | Owner | Action | Deadline | Confidence |", "| --- | --- | --- | --- | --- |"])
        if decision_log.commitments:
            for item in decision_log.commitments:
                lines.append(
                    f"| {item.id} | {item.owner} | {item.action} | {_format_deadline(item)} | {item.confidence} |"
                )
                if item.quote:
                    lines.append(f"> {item.quote}")
        else:
            lines.append("| - | - | No commitments recorded | - | - |")
        if decision_log.rejected:
            lines.extend(["", "## Rejected", ""])
            for item in decision_log.rejected:
                lines.append(f"- {item.id}: {item.summary} ({_format_timestamp(item.timestamp_s)})")
                if item.quote:
                    lines.append(f"  Quote: {item.quote}")
        if decision_log.open_questions:
            lines.extend(["", "## Open Questions", ""])
            for item in decision_log.open_questions:
                lines.append(f"- {item.id}: {item.question}")
                if item.asked_by:
                    lines.append(f"  Asked by: {item.asked_by}")
        lines.extend(
            [
                "",
                "## Metadata",
                "",
                f"- Model: {decision_log.metadata.model}",
                f"- Prompt version: {decision_log.metadata.prompt_version}",
            ]
        )
        return "\n".join(lines).strip() + "\n"

    def _render_html(self, decision_log: DecisionLog) -> str:
        def decision_row(item: Decision) -> str:
            return (
                "<tr>"
                f"<td>{escape(item.id)}</td>"
                f"<td>{escape(item.summary)}</td>"
                f"<td>{escape(item.speaker or '-')}</td>"
                f"<td>{escape(_format_timestamp(item.timestamp_s))}</td>"
                f"<td>{escape(item.confidence)}</td>"
                "</tr>"
            )

        def commitment_row(item: Commitment) -> str:
            return (
                "<tr>"
                f"<td>{escape(item.id)}</td>"
                f"<td>{escape(item.owner)}</td>"
                f"<td>{escape(item.action)}</td>"
                f"<td>{escape(_format_deadline(item))}</td>"
                f"<td>{escape(item.confidence)}</td>"
                "</tr>"
            )

        rejected_html = "".join(
            f"<li>{escape(item.id)}: {escape(item.summary)}</li>" for item in decision_log.rejected
        ) or "<li>None</li>"
        questions_html = "".join(
            f"<li>{escape(item.id)}: {escape(item.question)}</li>" for item in decision_log.open_questions
        ) or "<li>None</li>"
        empty_banner = "<p>No decisions recorded.</p>" if decision_log.is_empty else ""
        return (
            "<!DOCTYPE html>"
            "<html lang='en'>"
            "<head>"
            "<meta charset='utf-8'>"
            "<title>parler report</title>"
            "<style>"
            "body{font-family:system-ui,sans-serif;margin:2rem;line-height:1.5;}"
            "table{border-collapse:collapse;width:100%;margin-bottom:1.5rem;}"
            "th,td{border:1px solid #d0d0d0;padding:.5rem;text-align:left;vertical-align:top;}"
            ".timeline{padding:1rem;border:1px solid #d0d0d0;background:#fafafa;margin-bottom:1.5rem;}"
            "</style>"
            "</head>"
            "<body>"
            "<h1>Decision Log</h1>"
            f"{empty_banner}"
            "<section class='timeline'><h2>Timeline</h2><p>timeline</p></section>"
            "<h2>Decisions</h2>"
            "<table><thead><tr><th>ID</th><th>Summary</th><th>Owner</th><th>Timestamp</th><th>Confidence</th></tr></thead><tbody>"
            f"{''.join(decision_row(item) for item in decision_log.decisions) or '<tr><td>-</td><td>No decisions recorded</td><td>-</td><td>-</td><td>-</td></tr>'}"
            "</tbody></table>"
            "<h2>Commitments</h2>"
            "<table><thead><tr><th>ID</th><th>Owner</th><th>Action</th><th>Deadline</th><th>Confidence</th></tr></thead><tbody>"
            f"{''.join(commitment_row(item) for item in decision_log.commitments) or '<tr><td>-</td><td>-</td><td>No commitments recorded</td><td>-</td><td>-</td></tr>'}"
            "</tbody></table>"
            f"<h2>Rejected</h2><ul>{rejected_html}</ul>"
            f"<h2>Open Questions</h2><ul>{questions_html}</ul>"
            "</body></html>"
        )
