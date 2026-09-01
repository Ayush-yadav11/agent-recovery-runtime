# Agent recovery runtime

A small Python runtime for side-effecting agent tools when the tool response can disappear after the external service commits the action.

The runtime combines an action journal, idempotency-key protection, explicit verification, safe retry rules, and a LangGraph workflow with SQLite checkpoints.

## Contract

A side-effecting `Tool` has two boundaries:

```python
Tool(
    name="create_issue",
    execute=...,  # may mutate external state
    inspect=...,  # read-only verification
)
```

- `Runtime.execute(...)` requires an idempotency key.
- A tool raises `UnknownOutcome` when the request may have committed but its response was lost.
- `unknown` actions are verified through `Runtime.recover(action_id)`.
- A matching inspection result marks the action `success`.
- A missing result marks the action `verified_absent`.
- Inspector failures leave the action `unknown`.
- Only a `verified_absent` action can be retried, through `Runtime.retry(action_id)`.
- Reusing a key with different arguments is rejected.
- Reusing a key after success returns the stored result without executing again.

The LangGraph adapter routes these statuses explicitly and can persist graph state with `SqliteSaver`.

## GitHub adapter

`GitHubClient` uses `httpx`, appends this marker to created issue bodies, and searches all issue states without mutating them. For live use, load credentials from the environment:

```python
from agent_recovery.integrations.github import GitHubClient

client = GitHubClient.from_env()  # reads GITHUB_TOKEN
```

```text
<!-- agent-recovery:idempotency-key=<key> -->
```

A create transport or response-decoding failure becomes `UnknownOutcome`. HTTP status failures remain ordinary HTTP errors. Inspector failures propagate to the runtime, which preserves `unknown`.

## Install

Python 3.10 or newer is supported.

```bash
python3 -m pip install --user -e .
```

Pinned runtime dependencies:

- `langgraph==1.2.11`
- `langgraph-checkpoint-sqlite==3.1.1`
- `httpx==0.28.1`

## Run checks

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q agent_recovery tests examples
```

## Run the demo

```bash
python3 -m examples.recovery_demo
```

The demo simulates a GitHub-like API that commits an issue, loses the response, and then verifies the issue by idempotency key. The external create call runs once.

## Recovery workflow

```text
execute
  success          -> done
  failed           -> failed
  unknown         -> inspect

inspect
  success          -> done
  verified_absent  -> explicit retry
  unknown          -> human review

retry
  success          -> done
  failed           -> failed
  unknown          -> inspect
```

See [`docs/recovery-runbook.md`](docs/recovery-runbook.md) for the operator procedure.

## Scope

Included:

- Durable SQLite action records and lifecycle events
- Argument-hash idempotency protection
- LangGraph execution and SQLite checkpoint restart recovery
- Mockable GitHub issue integration
- Failure-injection tests

Not included:

- LLM planning
- Automatic retries
- Browser automation
- A web dashboard
- Model-based verification
