# Findings

## Baseline

- Repository path: `/home/ubuntu/agent-recovery-runtime`.
- Baseline commit: `cedb2ce feat: add safe agent action recovery runtime`.
- The current MVP has six passing `unittest` tests.
- The current runtime stores actions and events in SQLite and supports idempotent success plus recovery from `unknown`.
- The current schema has one unique row per `(tool_name, idempotency_key)`, so it cannot represent explicit retry attempts.
- The current demo is offline and uses a fake issue service.

## Foundation decision

Use LangGraph as the workflow foundation rather than forking it. Keep the side-effect action journal framework-independent. LangGraph owns graph state and checkpoint/resume behavior; the reliability layer owns action status, idempotency, verification, and retry policy.

## Integration decision

Use GitHub as the first real adapter, but keep all tests offline with an injectable HTTP transport. Add an exact issue-body marker so the inspector can read external state without creating another issue.

## Environment

- Host Python is 3.10.
- `pytest` is not installed.
- `python3-venv` and `ensurepip` are unavailable.
- Standard library `unittest` remains the reliable test runner.
- Installed versions: `langgraph==1.2.11`, `langgraph-checkpoint-sqlite==3.1.1`, and `httpx==0.28.1`.
- The checkpointer import is `from langgraph.checkpoint.sqlite import SqliteSaver`; the distribution name is not the Python import name.
- The current LangGraph public graph imports are `StateGraph`, `START`, and `END` from `langgraph.graph`.
- `SqliteSaver.from_conn_string(path)` is the public lifecycle factory; `setup()` initializes its tables; `compile(checkpointer=..., interrupt_before=[...])` supports restart testing.
- `GitHubClient` uses `httpx` with an injectable `MockTransport`, appends `<!-- agent-recovery:idempotency-key=<key> -->`, and searches all issue pages with `state=all`.
- `GitHubClient.from_env()` reads `GITHUB_TOKEN`; tests inject tokens and transports instead of using live credentials.

## Safety decisions

- `unknown` must never trigger automatic retry.
- `verified_absent` routes to an explicit `await_retry` gate; the graph never calls retry on its own.
- Inspector errors leave the action `unknown` and should route to human review.
- Only `verified_absent` permits explicit retry.
- Normal `failed` actions require a new idempotency key before another attempt.
- `Runtime.execute` rejects normal execution after `verified_absent`; callers must use `Runtime.retry`.
- Only the latest `verified_absent` action for a key can be retried.
- Tests must never use live GitHub credentials or perform live writes.

## Review resolutions

- Legacy SQLite rows are migrated to the attempt-aware schema with computed argument hashes.
- Persisted `running` actions can be moved to `unknown` and inspected after restart.
- GitHub marker lookup excludes records containing pull-request metadata.

## Follow-up issues

- Human approval for ambiguous inspector results.
- PostgreSQL action store for concurrent production workers.
- OpenTelemetry spans and Langfuse or Phoenix export.
- Temporal backend for distributed workflows.
