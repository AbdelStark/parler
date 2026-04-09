<roles>

| Role | Model Tier | Responsibility | Boundaries |
|---|---|---|---|
| Orchestrator | Frontier | choose phase, decompose work, assign file scopes, integrate results, enforce contract order | NEVER change implementation files directly; NEVER ignore gated-zone approval rules |
| Implementer | Mid-tier | implement one bounded slice in `parler/` and its directly related tests | NEVER make contract changes in `SPEC.md` / `SDD.md` / `TESTING.md` without escalation |
| Reviewer | Frontier | validate behavior, traceability, and regression risk | NEVER implement fixes; send work back with exact failures |
| Specialist | Mid-tier or Frontier | handle one domain: Mistral/Voxtral, extraction/parser, orchestration/CLI, fixtures | ONLY operate within the declared domain and file scope |

</roles>

<delegation_protocol>
  The Orchestrator follows this order for every task:

  1. ANALYZE: classify the request as contract reconciliation, implementation, verification, or fixture/tooling work.
  2. SERIALIZE CORE: if `parler/models.py`, `parler/config.py`, `parler/errors.py`, `tests/conftest.py`, or canonical docs are unsettled, do not parallelize phase work yet.
  3. DECOMPOSE: split into atomic slices with non-overlapping files and explicit tests.
  4. CLASSIFY:
     - routine module work -> Implementer
     - contract drift or risky architecture -> Orchestrator or Reviewer
     - vendor / parser / CLI state-machine work -> Specialist
  5. PLAN: define order, shared interfaces, and sync points.
  6. DELEGATE: send one task per file set with the exact acceptance tests.
  7. MONITOR: unblock only with new evidence, not speculation.
  8. INTEGRATE: merge results, reconcile overlapping assumptions, rerun affected tests.
  9. REVIEW: final gate on traceability, safety, and regression risk.
</delegation_protocol>

<task_format>
  Every delegated task must include:

  ## Task: [clear slice title]

  **Objective**: [one sentence]

  **Context**:
  - Files to read: [exact existing paths]
  - Files to modify: [exact paths]
  - Files to create: [exact paths]
  - Canonical references: [`SPEC.md`, `SDD.md`, `TESTING.md`, relevant tests/features]

  **Acceptance criteria**:
  - [ ] Narrow target passes: `[exact pytest command]`
  - [ ] Related lint/type checks pass: `ruff check [paths]` and `mypy [paths]`
  - [ ] No changes outside scope
  - [ ] Drift findings called out explicitly if discovered

  **Constraints**:
  - Do NOT modify: [out-of-scope files]
  - Do NOT change: [contracts/import paths/schemas outside scope]
  - Time box: [estimate]

  **Handoff**:
  - report changed files
  - report commands run and results
  - report unresolved drift or follow-up risks
</task_format>

<state_machine>
  Task lifecycle:

  `PENDING -> ASSIGNED -> IN_PROGRESS -> REVIEW -> { APPROVED -> DONE | REJECTED -> IN_PROGRESS }`
  `IN_PROGRESS -> BLOCKED -> [escalation] -> IN_PROGRESS`
  `IN_PROGRESS -> CANCELLED`

  Rules:
  - Only Orchestrator moves `PENDING -> ASSIGNED`.
  - `BLOCKED` requires: blocker, evidence, attempted fixes, next-needed decision.
  - `REVIEW -> REJECTED` requires exact failing files/commands and the contract that was missed.
  - Blocked for >30 minutes, or blocked by a contract contradiction, escalates to the human.
</state_machine>

<parallel_execution>
  Safe to parallelize after Phase 1-6 local foundations are stable:
  - `parler/audio/*` vs `parler/rendering/*`
    - `parler/attribution/*` vs `parler/extraction/*`
  - `parler/export/*` adapters in separate files
  - test additions in non-overlapping files
  - repo-local context/skill updates separate from runtime code

  Must serialize:
  - `parler/models.py`, `parler/config.py`, `parler/errors.py`
    - `parler/extraction/deadline_resolver.py` when deadline semantics themselves are changing
  - `tests/conftest.py`
  - `SPEC.md`, `SDD.md`, `TESTING.md`, `pyproject.toml`
  - checkpoint schema, cache key builders, and CLI surface changes

  Conflict protocol:
  1. Compare file sets before assignment.
  2. If overlap exists, assign an owner and queue the second task.
  3. Rebase the queued task on the merged result.
  4. Re-run the queued task's acceptance tests after rebase.
  5. Escalate if overlap changes a canonical contract.
</parallel_execution>

<escalation>
  Escalate to a human when:
  - a change would modify `SPEC.md`, `SDD.md`, `TESTING.md`, or `pyproject.toml`
  - a public CLI contract or checkpoint schema must change
  - real API usage, real recordings, or sensitive local state would be touched
  - confidence in the correct resolution falls below 70%

  Escalation format:
  **ESCALATION**: [one-line summary]
  **Context**: [what was being built]
  **Blocker**: [specific contradiction or risk]
  **Options**:
  1. [option] - Tradeoff: [gain/loss]
  2. [option] - Tradeoff: [gain/loss]
  **Recommendation**: [best option]
  **Impact of delay**: [what waits on this]
</escalation>
