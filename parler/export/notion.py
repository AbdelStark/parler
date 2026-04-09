"""Notion export adapter."""

from __future__ import annotations

from typing import Any

import requests

from ..models import DecisionLog
from .result import ExportResult

NOTION_VERSION = "2022-06-28"


def _default_title(decision_log: DecisionLog, title: str | None) -> str:
    if title:
        return title
    meeting_date = decision_log.metadata.meeting_date
    if meeting_date is not None:
        return f"Meeting {meeting_date.isoformat()}"
    return "Decision Log"


class NotionExporter:
    def __init__(self, api_token: str, database_id: str, *, timeout_s: int = 30):
        self.api_token = api_token
        self.database_id = database_id
        self.timeout_s = timeout_s

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_token}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
        }

    def _text_block(self, text: str) -> dict[str, Any]:
        return {
            "object": "block",
            "type": "paragraph",
            "paragraph": {
                "rich_text": [{"type": "text", "text": {"content": text[:2000]}}],
            },
        }

    def _heading_block(self, text: str) -> dict[str, Any]:
        return {
            "object": "block",
            "type": "heading_2",
            "heading_2": {
                "rich_text": [{"type": "text", "text": {"content": text[:2000]}}],
            },
        }

    def build_payload(
        self, decision_log: DecisionLog, *, title: str | None = None
    ) -> dict[str, Any]:
        page_title = _default_title(decision_log, title)
        children: list[dict[str, Any]] = [
            self._heading_block("Decisions"),
        ]
        if decision_log.decisions:
            children.extend(
                self._text_block(f"{item.id}: {item.summary}") for item in decision_log.decisions
            )
        else:
            children.append(self._text_block("No decisions recorded."))
        children.append(self._heading_block("Commitments"))
        if decision_log.commitments:
            for item in decision_log.commitments:
                deadline = (
                    item.deadline.resolved_date.isoformat()
                    if item.deadline and item.deadline.resolved_date is not None
                    else item.deadline.raw
                    if item.deadline
                    else "-"
                )
                children.append(
                    self._text_block(f"{item.id}: {item.owner} -> {item.action} ({deadline})")
                )
        else:
            children.append(self._text_block("No commitments recorded."))

        return {
            "parent": {"database_id": self.database_id},
            "properties": {
                "Name": {
                    "title": [{"type": "text", "text": {"content": page_title[:2000]}}],
                },
            },
            "children": children,
        }

    def export(self, decision_log: DecisionLog, *, title: str | None = None) -> ExportResult:
        payload = self.build_payload(decision_log, title=title)
        try:
            response = requests.post(
                "https://api.notion.com/v1/pages",
                headers=self._headers(),
                json=payload,
                timeout=self.timeout_s,
            )
        except requests.RequestException as exc:
            return ExportResult(success=False, url=None, error=str(exc))

        if response.status_code >= 400:
            message = ""
            try:
                body = response.json()
                message = str(body.get("message", "")) if isinstance(body, dict) else ""
            except ValueError:
                message = response.text
            error = f"Notion export failed ({response.status_code})"
            if message:
                error = f"{error}: {message}"
            return ExportResult(success=False, url=None, error=error)

        data = response.json()
        return ExportResult(success=True, url=data.get("url"), error=None)


__all__ = ["NotionExporter"]
