# Progress

## 2026-09-01

- Restored the project context and confirmed no root planning files existed.
- Confirmed the repository was clean at commit `cedb2ce` before implementation.
- Created `task_plan.md` and `findings.md` with the LangGraph migration plan, TDD checkpoints, exact paths, commands, risks, and acceptance predicates.
- Baseline passed: six tests at `cedb2ce`.
- Added package boundaries and committed `dcb2320`.
- Added dependency metadata and committed `7af84f0`.
- Extracted action types and committed `5183578`.
- Isolated SQLite storage and committed `7e099ce`.
- Added verified-absence retry policy and committed `ca7c072`.
- Added pure LangGraph routing and committed `35dc8fa`.
- Added executable graph paths and committed `3e9290e`.
- Added persistent LangGraph checkpoints and committed `d66e65e`.
- Added the mocked GitHub adapter and committed `f0d6b24`.
- Added GitHub outcome classification and committed `ebf2505`.
- Wired GitHub through the graph and committed `64ae22a`.
- Added the eight-case acceptance matrix; it passes independently.
- Added the README command contract, runbook, offline demo, and executable check script.
- Added environment-backed GitHub credential loading and final review checks.
- Review found and fixed automatic graph retry after `verified_absent` and same-key re-execution after normal `failed`.
- Review found and fixed legacy SQLite migration, persisted `running` recovery, and GitHub pull-request false matches in `05da46a`.

## Phase 7: Explicit human approval and restart-safe retry

Status: planning.

Problem: `verified_absent` currently ends at an `await_retry` terminal node. There is no persistent approval record, no operator identity, and no protection against duplicate approvals across process restarts. A real recovery workflow pauses here and waits for a human.

Next milestone: `v0.2.0` — human approval and restart-safe retry.

Acceptance:

- GitHub create request commits but the response is lost.
- Issue inspection finds nothing.
- Runtime pauses at `await_retry` and persists an approval request.
- Process restarts.
- Approval is submitted with operator and reason.
- Exactly one retry executes.
- A second approval submission for the same action is rejected.

Planned changes:

1. Core runtime: add `ApprovalRequired` state and `Runtime.approve()` and `Runtime.reject()` APIs.
2. Storage: persist approvals in SQLite with `action_id`, `operator`, `timestamp`, `reason`, and a uniqueness constraint to prevent duplicates.
3. Runtime recovery: on restart, classify persisted `running` actions by their approval and verification state.
4. LangGraph: use interrupt/resume so the worker pauses at the retry gate and resumes on approval.
5. Tests (TDD): approve grants one retry; reject ends the action; restart-while-awaiting resumes correctly; duplicate approval is rejected.


## Phase 8: Verification outcome contracts

Problem: inspection currently returns found, absent, or failure. A temporary GitHub API error should not look like a safe absence.

Planned outcomes:

- found
- verified_absent
- ambiguous
- unavailable

Policy:

- found -> success
- absent -> approval required
- ambiguous -> human review
- unavailable -> remain unknown

## Phase 9: CI, quality, and release

- Add GitHub Actions across Python 3.10, 3.11, 3.12.
- Run unit tests, compilation, secret scan, and diff check.
- Add linter.
- Add CHANGELOG and MIT license.
- Publish v0.1.0 and a contributor guide.

## Phase 10: Additional adapters

Add one adapter with a different failure model: Stripe, Slack, email, or Jira.

## Phase 11: Observability

Emit OpenTelemetry-compatible events for action lifecycle, approval, and retry. No dashboard.

## Phase 12: Production storage

Add PostgreSQL, concurrent workers, a lease field for running actions, and a recovery sweeper. Keep SQLite for local use.

## Current phase

Phase 10: additional adapters. Phases 1-9 are implemented, tested, and committed; all 65 tests pass.

## Next action

Implement Phase 10: add a non-GitHub adapter (Stripe recommended) implementing the Tool contract with VerificationOutcome. Claude Code's Phase 9 run added GitHub Actions CI, ruff lint config, CHANGELOG, and MIT license. The CI workflow file is untracked (gitignored) and requires GitHub web editor upload due to token scope.
