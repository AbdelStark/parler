"""Jira export adapter."""

from __future__ import annotations

from typing import Any

import requests
from requests.auth import HTTPBasicAuth

from ..models import Commitment, DecisionLog
from .result import ExportResult


def _default_title(decision_log: DecisionLog, title: str | None) -> str:
    if title:
        return title
    meeting_date = decision_log.metadata.meeting_date
    if meeting_date is not None:
        return f"Meeting {meeting_date.isoformat()}"
    return "Decision Log"


class JiraExporter:
    def __init__(
        self,
        server_url: str,
        email: str,
        api_token: str,
        project_key: str,
        *,
        timeout_s: int = 30,
    ):
        self.server_url = server_url.rstrip("/")
        self.email = email
        self.api_token = api_token
        self.project_key = project_key
        self.timeout_s = timeout_s

    def _headers(self) -> dict[str, str]:
        return {"Accept": "application/json", "Content-Type": "application/json"}

    def _issue_payload(self, commitment: Commitment, *, title: str) -> dict[str, Any]:
        description = (
            f"Generated from {title}.\n\nOwner: {commitment.owner}\nAction: {commitment.action}"
        )
        fields: dict[str, Any] = {
            "project": {"key": self.project_key},
            "summary": f"[{title}] {commitment.action}",
            "description": description,
            "issuetype": {"name": "Task"},
        }
        if commitment.deadline and commitment.deadline.resolved_date is not None:
            fields["duedate"] = commitment.deadline.resolved_date.isoformat()
        return {"fields": fields}

    def export(self, decision_log: DecisionLog, *, title: str | None = None) -> list[ExportResult]:
        meeting_title = _default_title(decision_log, title)
        results: list[ExportResult] = []
        for commitment in decision_log.commitments:
            try:
                response = requests.post(
                    f"{self.server_url}/rest/api/3/issue",
                    headers=self._headers(),
                    auth=HTTPBasicAuth(self.email, self.api_token),
                    json=self._issue_payload(commitment, title=meeting_title),
                    timeout=self.timeout_s,
                )
            except requests.RequestException as exc:
                results.append(ExportResult(success=False, url=None, error=str(exc)))
                continue

            if response.status_code >= 400:
                results.append(
                    ExportResult(
                        success=False,
                        url=None,
                        error=f"Jira export failed ({response.status_code}): {response.text}",
                    )
                )
                continue

            body = response.json()
            issue_key = body.get("key")
            issue_url = f"{self.server_url}/browse/{issue_key}" if issue_key else None
            results.append(ExportResult(success=True, url=issue_url, error=None))
        return results


__all__ = ["JiraExporter"]
