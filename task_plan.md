# LangGraph reliability layer

## Goal

Migrate the current SQLite action-recovery MVP into a LangGraph-based workflow with GitHub side-effect verification, explicit safe retries, persistent checkpoints, and no duplicate issue creation.

## Current context

- Repository: `/home/ubuntu/agent-recovery-runtime`
- Baseline commit: `cedb2ce feat: add safe agent action recovery runtime`
- Current tracked implementation: `agent_recovery/__init__.py`, `tests/test_runtime.py`, `examples/recovery_demo.py`, `README.md`, and `pyproject.toml`.
- Current behavior: six `unittest` tests pass; actions are stored in SQLite; successful actions are idempotent; uncertain outcomes can be inspected and recovered.
- Current limitation: one action key cannot represent a verified-absent retry, no LangGraph integration exists, and the demo uses a fake service rather than a GitHub client.
- Host constraint: `python3` is Python 3.10; `pytest` and `python3-venv` are unavailable. Keep `python3 -m unittest discover -s tests -v` as the canonical test command unless the environment is deliberately provisioned.
- Earlier detailed plan: `.hermes/plans/2026-09-01_055718-langgraph-reliability-migration.md`.
- Do not run live GitHub writes in tests or demos. Use injectable mocked HTTP transport.
- Do not fork LangGraph. Build an adapter around public APIs.

## Architecture

Keep the action journal framework-independent and split it into `core/actions.py`, `core/store.py`, and `core/runtime.py`. Add `langgraph/state.py` and `langgraph/workflow.py` to map action statuses to deterministic graph routes, while LangGraph checkpoints persist graph progress and the action journal remains the source of truth for external side effects. Add `integrations/github.py` with an idempotency marker and read-only inspector, then exercise the complete flow offline.

State transitions:

```text
execute -> success
execute -> unknown -> inspect -> success
execute -> unknown -> inspect -> verified_absent -> explicit retry
execute -> unknown -> inspector error -> human review
```

The model is not required for the first milestone. Reliability behavior must be deterministic and independently testable.

## Phase status

| Phase | Status | Completion condition |
|---|---|---|
| 1. Baseline and package shape | complete | Imports and dependency policy are defined and tested |
| 2. Core attempt and retry model | complete | `unknown`, `verified_absent`, and explicit `retry` are persisted and tested |
| 3. LangGraph adapter | complete | Workflow routes all statuses and survives restart |
| 4. GitHub integration | complete | Mocked client creates and inspects issues by marker |
| 5. Acceptance and documentation | complete | Full failure matrix, demo, checks, and README are green |
| 6. Review and contribution prep | complete | Clean tree, review complete, planning files removed, upstream candidates documented |
| 7. Explicit human approval and restart-safe retry | complete | ApprovalRequired state, Runtime.approve/reject, persisted approval records, single-use retry |
| 8. Verification outcome contracts | complete | VerificationOutcome distinguishes found, absent, unavailable, ambiguous |
| 9. CI, quality, and release | complete | GitHub Actions CI, ruff lint, CHANGELOG, MIT license (workflow file requires web upload) |
| 10. Additional adapters | planned | At least one adapter with a different failure model |
| 11. Observability | planned | OpenTelemetry-compatible events for action lifecycle |
| 12. Production storage | planned | PostgreSQL, concurrent workers, and recovery sweeper |

## Next step

Phase 10: additional adapters. Add a non-GitHub adapter (Stripe recommended) with the same Tool contract and VerificationOutcome contract. Phases 1-9 are implemented, tested, and committed; all 65 tests pass.

## Step-by-step tasks

### Phase 1: Baseline and package shape

#### 1.1 Baseline

Read-only commands:

```bash
cd /home/ubuntu/agent-recovery-runtime
git status --short --branch
git show --stat --oneline HEAD
python3 -m unittest discover -s tests -v
```

Expected: clean `master`, `HEAD` is `cedb2ce`, and six tests finish with `OK`.

Record output in `progress.md`. No commit.

#### 1.2 Package boundaries

Create:

```text
agent_recovery/core/__init__.py
agent_recovery/core/actions.py
agent_recovery/core/runtime.py
agent_recovery/core/store.py
agent_recovery/integrations/__init__.py
agent_recovery/integrations/github.py
agent_recovery/langgraph/__init__.py
agent_recovery/langgraph/state.py
agent_recovery/langgraph/workflow.py
tests/test_package_layout.py
```

TDD cycle:

1. Write `tests/test_package_layout.py` importing `agent_recovery.core.actions`, `agent_recovery.integrations.github`, and `agent_recovery.langgraph.workflow`.
2. Run `python3 -m unittest tests.test_package_layout -v`; it must fail before the modules exist.
3. Add package markers and empty modules.
4. Run the same command; it must pass.
5. Commit:

```bash
git add agent_recovery tests/test_package_layout.py
git commit -m "chore: create reliability package boundaries"
```

#### 1.3 Dependency policy

Inspect the environment and available package metadata:

```bash
python3 --version
python3 -m pip index versions langgraph
python3 -m pip index versions langgraph-checkpoint-sqlite
```

Update `pyproject.toml` to use a Python requirement compatible with the host and selected LangGraph release. Add LangGraph, its SQLite checkpointer package, and one HTTP client only if needed. Remove the stale pytest extra if tests remain standard-library `unittest`.

TDD cycle:

1. Add `tests/test_environment.py` checking the declared Python requirement and dependency names using `importlib.metadata` or direct TOML parsing.
2. Run `python3 -m unittest tests.test_environment -v`; it must fail before metadata is updated.
3. Update `pyproject.toml`.
4. Run the focused test, then `python3 -m unittest discover -s tests -v`; both must pass.
5. Commit:

```bash
git add pyproject.toml tests/test_environment.py
git commit -m "build: define LangGraph runtime dependencies"
```

### Phase 2: Core attempt and retry model

#### 2.1 Extract domain types

Move public types from `agent_recovery/__init__.py` to `agent_recovery/core/actions.py`. Preserve these imports:

```python
from agent_recovery import Runtime, Tool, UnknownOutcome
```

Add the status `verified_absent` and `attempt: int = 1` to `ActionResult`. Re-export public names from `agent_recovery/__init__.py`.

TDD cycle:

1. Add import and status assertions to `tests/test_runtime.py`.
2. Run `python3 -m unittest tests.test_runtime -v`; it must fail before extraction.
3. Move definitions and add re-exports.
4. Run the focused suite; all existing tests must pass.
5. Commit:

```bash
git add agent_recovery tests/test_runtime.py
git commit -m "refactor: isolate action domain types"
```

#### 2.2 Split storage from runtime policy

Implement `agent_recovery/core/store.py` for SQLite schema and queries, and `agent_recovery/core/runtime.py` for tool registration and transitions. Keep behavior unchanged while moving code.

Use an attempt-aware schema in `store.py`:

```sql
CREATE TABLE actions (
    action_id TEXT PRIMARY KEY,
    tool_name TEXT NOT NULL,
    arguments_json TEXT NOT NULL,
    arguments_hash TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    status TEXT NOT NULL,
    result_json TEXT,
    error TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (tool_name, idempotency_key, attempt)
);

CREATE INDEX actions_latest_key
ON actions (tool_name, idempotency_key, attempt DESC);
```

Retain the existing `events` table. Store arguments with stable JSON encoding and compare the hash when a key is reused.

TDD cycle:

1. Add a test proving the same key with different arguments raises `ValueError`.
2. Run the focused test; it must fail before the hash check exists.
3. Implement the store split and hash validation.
4. Run `python3 -m unittest tests.test_runtime -v`; all tests must pass.
5. Commit:

```bash
git add agent_recovery tests/test_runtime.py
git commit -m "refactor: separate action storage from runtime policy"
```

#### 2.3 Implement verified absence and explicit retry

Files: `agent_recovery/core/runtime.py`, `agent_recovery/core/store.py`, `tests/test_runtime.py`.

Required public methods:

```python
def recover(self, action_id: str) -> ActionResult: ...
def retry(self, action_id: str) -> ActionResult: ...
```

Rules:

- `recover` accepts only `unknown` actions.
- Inspector returns a value: transition to `success`.
- Inspector returns `None`: transition to `verified_absent`.
- Inspector raises: leave status `unknown` and record the verification error.
- `retry` accepts only `verified_absent`, creates attempt two with the same idempotency key and arguments hash, and executes once.
- `unknown` never retries automatically.
- A successful action replay with the same key returns the stored result without executing the tool again.

TDD cycle:

1. Add tests for verified absence, explicit retry, inspector failure, and restart persistence.
2. Run each focused test with `python3 -m unittest tests.test_runtime.RuntimeTests.<test_name> -v`; new tests must fail first.
3. Implement one transition at a time.
4. Run `python3 -m unittest discover -s tests -v`; all tests must pass.
5. Commit:

```bash
git add agent_recovery tests/test_runtime.py
git commit -m "feat: add safe verified-absence retries"
```

### Phase 3: LangGraph adapter

#### 3.1 Define state and pure routing

Files: `agent_recovery/langgraph/state.py`, `agent_recovery/langgraph/workflow.py`, `tests/test_langgraph_workflow.py`.

Use this state shape:

```python
class RecoveryState(TypedDict, total=False):
    title: str
    body: str
    idempotency_key: str
    action_id: str
    action_status: Literal[
        "running", "success", "failed", "unknown", "verified_absent"
    ]
    action_result: Any
    error: str
    route: Literal["success", "verify", "retry", "human_review", "failed"]
```

Implement and test pure functions:

```python
def route_after_execute(state: RecoveryState) -> str: ...
def route_after_verify(state: RecoveryState) -> str: ...
```

Expected mappings:

```text
success -> success
unknown -> verify
verified_absent -> retry
failed -> failed
missing or ambiguous -> human_review
```

TDD cycle:

1. Write one test per branch.
2. Run `python3 -m unittest tests.test_langgraph_workflow -v`; it must fail before implementation.
3. Implement the functions.
4. Run the focused tests; all branches must pass.
5. Commit:

```bash
git add agent_recovery/langgraph tests/test_langgraph_workflow.py
git commit -m "feat: add explicit recovery graph routing"
```

#### 3.2 Build the executable graph

File: `agent_recovery/langgraph/workflow.py`.

Implement:

```python
def build_recovery_graph(runtime: Runtime, tool_name: str): ...
```

Nodes:

```text
execute_action
inspect_action
retry_action
success
failed
human_review
```

Use only the selected LangGraph release's public `StateGraph`, `START`, `END`, and compile interfaces. Nodes must call the existing `Runtime`; they must not duplicate SQLite logic.

TDD cycle:

1. Add a fake-tool test for normal success and one side-effect call.
2. Run the focused test; it must fail before the graph exists.
3. Implement the nodes and conditional edges.
4. Run the focused test and the complete suite; both must pass.
5. Commit:

```bash
git add agent_recovery/langgraph/workflow.py tests/test_langgraph_workflow.py
git commit -m "feat: execute recovery policy through LangGraph"
```

#### 3.3 Add persistent graph checkpoints

Files: `agent_recovery/langgraph/workflow.py`, `tests/test_langgraph_workflow.py`, `pyproject.toml`.

Build the graph with a persistent SQLite checkpointer. Use a deterministic thread config:

```python
config = {"configurable": {"thread_id": "customer-123"}}
```

Test sequence:

```text
invoke until action is unknown
close the graph/checkpointer
build a new graph against the same checkpoint database
resume the same thread
inspect the external state
finish with route=success
assert the create call count is one
```

TDD cycle:

1. Write the restart test and run it; it must fail before checkpoint persistence exists.
2. Check the installed LangGraph package API and implement the public SQLite checkpointer interface.
3. Run the focused restart test; it must pass.
4. Run the complete suite.
5. Commit:

```bash
git add pyproject.toml agent_recovery/langgraph tests/test_langgraph_workflow.py
git commit -m "feat: persist LangGraph recovery checkpoints"
```

### Phase 4: GitHub integration

#### 4.1 Implement an injectable GitHub client

Files: `agent_recovery/integrations/github.py`, `tests/test_github.py`.

Implement this seam:

```python
class GitHubClient:
    def __init__(self, token: str, transport: Transport | None = None) -> None: ...

    def create_issue(
        self,
        owner: str,
        repository: str,
        title: str,
        body: str,
        idempotency_key: str,
    ) -> dict[str, Any]: ...

    def find_issue_by_idempotency_key(
        self,
        owner: str,
        repository: str,
        idempotency_key: str,
    ) -> dict[str, Any] | None: ...
```

Append this exact marker to issue bodies:

```text
<!-- agent-recovery:idempotency-key=<key> -->
```

The inspector searches open and closed issues and never mutates data.

TDD cycle:

1. Add mocked transport tests for the POST payload, marker, matching issue, and absent issue.
2. Run `python3 -m unittest tests.test_github -v`; it must fail before the client exists.
3. Implement the client and injectable transport.
4. Run the focused tests and the complete suite.
5. Commit:

```bash
git add agent_recovery/integrations/github.py tests/test_github.py
git commit -m "feat: add idempotent GitHub issue adapter"
```

#### 4.2 Map GitHub errors to runtime outcomes

Files: `agent_recovery/integrations/github.py`, `tests/test_github.py`.

Required mapping:

```text
HTTP 2xx                              -> result
HTTP 4xx auth or invalid input        -> normal failed exception
POST timeout after dispatch           -> UnknownOutcome
inspector timeout or parse failure    -> inspector exception, remain unknown
```

Never infer commit status from a client timeout. Recovery inspection must provide evidence.

TDD cycle: add one test per mapping, run each red, implement the mapping, run the GitHub suite and full suite, then commit:

```bash
git add agent_recovery/integrations/github.py tests/test_github.py
git commit -m "feat: classify GitHub uncertain outcomes"
```

#### 4.3 Replace the demo with the offline LangGraph flow

Files: `examples/recovery_demo.py`, `tests/test_demo.py`.

Simulate a POST that commits an issue then raises `UnknownOutcome`, an inspector that finds it by marker, and a workflow that finishes successfully with exactly one create call.

Command:

```bash
python3 -m examples.recovery_demo
```

Expected output includes:

```text
initial status:  unknown
recovered status: success
create calls:     1
```

TDD cycle: write the stdout test, run it red, update the demo, run the focused test and command green, then commit:

```bash
git add examples/recovery_demo.py tests/test_demo.py
git commit -m "docs: demonstrate LangGraph GitHub recovery"
```

### Phase 5: Acceptance and documentation

#### 5.1 End-to-end failure matrix

File: `tests/test_acceptance.py`.

Cover these cases using fake transports and temporary SQLite files:

```text
normal success                         -> success, one POST
failure before commit                  -> failed, one POST
commit then lost response              -> unknown then success, one POST
unknown with issue absent              -> verified_absent, no inspect-side write
verified absence then explicit retry   -> success, two POST attempts
inspector failure                      -> unknown, no retry
restart after unknown                 -> success, one POST
same key with different arguments      -> ValueError
```

TDD cycle: add tests, run `python3 -m unittest tests.test_acceptance -v` to see red, fix one behavior at a time, run the full suite, and commit:

```bash
git add tests/test_acceptance.py agent_recovery
git commit -m "test: cover end-to-end side-effect recovery matrix"
```

#### 5.2 Documentation and reproducible checks

Files:

```text
README.md
scripts/check.sh
```

README must explain LangGraph's role, this project's action journal, all statuses, the unknown versus verified-absent distinction, installation, test command, offline demo, and live GitHub credential safety. Keep LLM planning, browser automation, dashboards, and multi-agent orchestration out of scope.

Create `scripts/check.sh`:

```bash
#!/usr/bin/env bash
set -euo pipefail

python3 -m unittest discover -s tests -v
python3 -m compileall -q agent_recovery examples tests
printf 'checks: ok\n'
```

TDD cycle:

1. Add `tests/test_readme_commands.py` checking the canonical commands are documented.
2. Run it red.
3. Update README and the check script.
4. Run:

```bash
chmod +x scripts/check.sh
./scripts/check.sh
python3 -m examples.recovery_demo
git diff-tree --check --no-commit-id -r HEAD
git status --short --branch
```

Expected: tests pass, `checks: ok`, demo shows one create call, diff check is clean, and the tree is clean.
5. Commit:

```bash
git add README.md scripts/check.sh tests/test_readme_commands.py
git commit -m "docs: document LangGraph reliability workflow"
```

### Phase 6: Review and contribution prep

Inspect all changed files for:

- No automatic retry from `unknown`.
- No writes in inspectors.
- Canonical argument hash stored with the idempotency key.
- Restart recovery from both graph checkpoint and action journal.
- No stuck `running` state after a caught tool exception.
- No private LangGraph APIs.
- No secrets in tracked files.

Command:

```bash
git grep -nE 'gh''p_|github''_pat_|BEGIN ''RSA PRIVATE KEY|ANTHROPIC''_API_KEY|OPENAI''_API_KEY' -- . ':!.git'
```

Expected: no matches.

Run the full verification commands from Phase 5.2, then document follow-up issues for human approval:

1. Human approval for ambiguous inspector results.
2. PostgreSQL action store.
3. OpenTelemetry spans.
4. Langfuse or Phoenix export.
5. Temporal backend for distributed workflows.

## Tests and validation

Every code task follows this exact loop:

```text
write one behavior test
run the focused test and confirm red
implement the smallest behavior
run the focused test and confirm green
run the full suite at a stable seam
commit the coherent change
```

Final acceptance predicate:

> Given a GitHub-like API that commits an issue and loses its response, the LangGraph workflow resumes from a persistent checkpoint, finds the issue by its deterministic marker, finishes with `route == "success"`, and makes exactly one create request.

Safe retry predicate:

> If inspection proves the side effect absent, an explicit retry may create attempt two. If inspection is uncertain, no retry occurs.

## Risks and tradeoffs

- LangGraph and its SQLite checkpointer API may change. Resolve and pin compatible versions, and use public APIs only.
- SQLite is for the local demo, not concurrent production workers.
- GitHub search may be eventually consistent or incomplete. If inspection cannot provide strong evidence, remain `unknown` and route to human review.
- LangGraph checkpoints and the action journal are separate stores initially. This duplicates some state but keeps side-effect evidence independent.
- LangGraph is preferred over Temporal for the first implementation because it is faster to integrate with an agent workflow. Temporal remains a later production backend option.
- Do not add a generic decorator until two real integrations prove that the abstraction is stable. Explicit nodes are the first interface.

## Errors encountered

| Error | Attempt | Resolution |
|---|---:|---|
| `pytest` unavailable | baseline MVP | Use standard-library `unittest` as canonical runner |
| `python3 -m venv` failed because `ensurepip` is unavailable | baseline MVP | Do not require a virtualenv for the first slice; resolve environment before dependency installation |
| Demo import failed when run as a script | baseline MVP | Run the demo as `python3 -m examples.recovery_demo` |
| Git author identity missing | baseline MVP | Created the baseline commit with explicit local commit identity; do not alter global Git config |
