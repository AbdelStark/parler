"""Linear export adapter."""

from __future__ import annotations

from typing import Any

import requests

from ..models import Commitment, DecisionLog
from .result import ExportResult

LINEAR_ISSUE_CREATE_MUTATION = """
mutation IssueCreate($input: IssueCreateInput!) {
  issueCreate(input: $input) {
    issue {
      id
      url
    }
  }
}
""".strip()


def _default_title(decision_log: DecisionLog, title: str | None) -> str:
    if title:
        return title
    meeting_date = decision_log.metadata.meeting_date
    if meeting_date is not None:
        return f"Meeting {meeting_date.isoformat()}"
    return "Decision Log"


class LinearExporter:
    def __init__(self, api_key: str, team_id: str, *, timeout_s: int = 30):
        self.api_key = api_key
        self.team_id = team_id
        self.timeout_s = timeout_s

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": self.api_key,
            "Content-Type": "application/json",
        }

    def _issue_input(
        self,
        commitment: Commitment,
        *,
        title: str,
    ) -> dict[str, Any]:
        description_lines = [
            f"Generated from {title}.",
            "",
            f"Owner: {commitment.owner}",
        ]
        if commitment.quote:
            description_lines.extend(["", f'Context: "{commitment.quote}"'])
        issue_input: dict[str, Any] = {
            "teamId": self.team_id,
            "title": f"[{title}] {commitment.action}",
            "description": "\n".join(description_lines),
        }
        if commitment.deadline and commitment.deadline.resolved_date is not None:
            issue_input["dueDate"] = commitment.deadline.resolved_date.isoformat()
        return issue_input

    def export(self, decision_log: DecisionLog, *, title: str | None = None) -> list[ExportResult]:
        meeting_title = _default_title(decision_log, title)
        results: list[ExportResult] = []
        for commitment in decision_log.commitments:
            payload = {
                "query": LINEAR_ISSUE_CREATE_MUTATION,
                "variables": {"input": self._issue_input(commitment, title=meeting_title)},
            }
            try:
                response = requests.post(
                    "https://api.linear.app/graphql",
                    headers=self._headers(),
                    json=payload,
                    timeout=self.timeout_s,
                )
            except requests.RequestException as exc:
                results.append(ExportResult(success=False, url=None, error=str(exc)))
                continue

            if response.status_code >= 400:
                message = ""
                try:
                    body = response.json()
                    errors = body.get("errors", []) if isinstance(body, dict) else []
                    if errors:
                        message = str(errors[0].get("message", ""))
                except ValueError:
                    message = response.text
                error = f"Linear export failed ({response.status_code})"
                if message:
                    error = f"{error}: {message}"
                results.append(ExportResult(success=False, url=None, error=error))
                continue

            body = response.json()
            issue = body.get("data", {}).get("issueCreate", {}).get("issue", {})
            results.append(ExportResult(success=True, url=issue.get("url"), error=None))
        return results


__all__ = ["LinearExporter"]
