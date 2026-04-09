"""Slack export adapter."""

from __future__ import annotations

import requests

from ..models import DecisionLog
from .result import ExportResult


def _default_title(decision_log: DecisionLog, title: str | None) -> str:
    if title:
        return title
    meeting_date = decision_log.metadata.meeting_date
    if meeting_date is not None:
        return f"Meeting {meeting_date.isoformat()}"
    return "Decision Log"


class SlackExporter:
    def __init__(self, webhook_url: str, *, timeout_s: int = 15):
        self.webhook_url = webhook_url
        self.timeout_s = timeout_s

    def build_payload(
        self, decision_log: DecisionLog, *, title: str | None = None
    ) -> dict[str, object]:
        meeting_title = _default_title(decision_log, title)
        summary = (
            f"{meeting_title}: {len(decision_log.decisions)} decisions, "
            f"{len(decision_log.commitments)} commitments, "
            f"{len(decision_log.open_questions)} open questions"
        )
        commitment_lines = [
            f"- {item.owner}: {item.action}" for item in decision_log.commitments
        ] or ["- No commitments recorded"]
        decision_lines = [f"- {item.id}: {item.summary}" for item in decision_log.decisions] or [
            "- No decisions recorded"
        ]
        text = "\n".join(
            [summary, "", "Decisions:", *decision_lines, "", "Commitments:", *commitment_lines]
        )
        return {
            "text": text,
            "blocks": [
                {"type": "section", "text": {"type": "mrkdwn", "text": f"*{summary}*"}},
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": "*Decisions*\n" + "\n".join(decision_lines)},
                },
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": "*Commitments*\n" + "\n".join(commitment_lines),
                    },
                },
            ],
        }

    def export(self, decision_log: DecisionLog, *, title: str | None = None) -> ExportResult:
        payload = self.build_payload(decision_log, title=title)
        try:
            response = requests.post(self.webhook_url, json=payload, timeout=self.timeout_s)
        except requests.RequestException as exc:
            return ExportResult(success=False, url=None, error=str(exc))

        if response.status_code >= 400:
            return ExportResult(
                success=False,
                url=None,
                error=f"Slack export failed ({response.status_code}): {response.text}",
            )
        return ExportResult(success=True, url=self.webhook_url, error=None)


__all__ = ["SlackExporter"]
