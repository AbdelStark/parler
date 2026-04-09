---
name: rendering-and-export
description: Implement canonical report rendering and isolated export adapters. Activate this skill for Markdown/HTML/JSON output work, report layout changes, HTML safety, or any Notion/Linear/Jira/Slack adapter task. Use it even for "small" report tweaks because renderer/export drift is easy to introduce.
prerequisites: pytest, ruff, mypy, requests
---

# Rendering And Export

<purpose>
Extend or debug the Phase 6 reporting surface without mixing canonical rendering logic with third-party side effects. This skill covers local report artifacts and the adapter layer that turns a `DecisionLog` into export payloads.
</purpose>

<context>
- `parler/rendering/renderer.py` is the canonical local output layer.
- Local artifacts render from `DecisionLog`, never from raw LLM payloads.
- `parler/export/` contains isolated adapters for Notion, Linear, Jira, Slack, plus the shared `ExportResult`.
- Export failures are non-fatal. A remote failure must not invalidate local Markdown/HTML/JSON output.
- Fast verification for this domain is currently:
  - `uv run pytest tests/unit/test_report_rendering.py tests/integration/test_export_integrations.py -q`
  - `uv run ruff check parler tests/smoke_test.py`
  - `uv run mypy parler/`
</context>

<procedure>
1. Read `IMPLEMENTATION_PLAN.md` Phase 6 and `RFC-0005` if the task changes report/export behavior.
2. Start from `DecisionLog` and confirm which local format or exporter is affected.
3. If changing rendering:
   - keep Markdown, HTML, and JSON derived from the same canonical data
   - preserve self-contained HTML with no external CSS/JS/fonts
   - escape hostile content before inserting it into HTML
4. If changing an exporter:
   - keep payload construction inside the adapter file
   - return `ExportResult` or `list[ExportResult]`, never raw responses
   - catch auth/network/API failures and degrade to an error result
5. Keep renderer code and export code separate. Do not import exporters into the renderer.
6. Run the narrow rendering/export tests first.
7. Re-run `ruff`, `mypy`, smoke, and `uv build` before closing the slice.
</procedure>

<patterns>
<do>
  - Render deadlines from resolved dates when available; fall back to raw text with uncertainty markers only when unresolved.
  - Keep HTML self-contained with inline CSS and escaped content.
  - Use one adapter file per external system and keep payload builders small and inspectable.
  - Return explicit URLs on success and explicit error strings on failure via `ExportResult`.
</do>
<dont>
  - Don't render from raw extraction JSON -> normalize into `DecisionLog` first.
  - Don't let a Notion/Slack/Linear/Jira failure erase local report generation -> keep side effects isolated.
  - Don't share vendor-specific payload code across renderer logic -> the renderer is local-output-only.
  - Don't add external CSS, fonts, or JS to the HTML report -> it must work from `file://`.
</dont>
</patterns>

<examples>
Example: rendering verification

```bash
uv run pytest tests/unit/test_report_rendering.py -q
uv run ruff check parler/rendering/renderer.py
uv run mypy parler/rendering/renderer.py
```

Example: export adapter verification

```bash
uv run pytest tests/integration/test_export_integrations.py -q
uv run ruff check parler/export
uv run mypy parler/export
```
</examples>

<troubleshooting>
| Symptom | Cause | Fix |
|---|---|---|
| Raw `<script>` appears in HTML output | missing escaping path in renderer | escape inserted text with `html.escape` before interpolation |
| Export test fails on auth or network branches | adapter raised instead of degrading | catch `requests.RequestException` and return `ExportResult(success=False, ...)` |
| Renderer and exporter disagree on counts or wording | exporter rebuilt its own interpretation from partial data | derive both from the same `DecisionLog` input |
</troubleshooting>

<references>
- `parler/rendering/renderer.py`: canonical local report formats
- `parler/export/result.py`: shared export result contract
- `parler/export/notion.py`: Notion page payload mapping
- `parler/export/linear.py`: Linear issue creation mapping
- `parler/export/jira.py`: Jira issue mapping
- `parler/export/slack.py`: Slack webhook payload mapping
- `tests/unit/test_report_rendering.py`: rendering contract
- `tests/integration/test_export_integrations.py`: adapter contract
</references>
