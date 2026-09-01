# Agent recovery runtime

A dependency-free Python MVP for a specific agent reliability problem: an external side effect may have succeeded even when the tool response is lost.

The runtime records actions in SQLite, requires idempotency keys for side effects, and verifies an `unknown` action before anyone retries it.

## Current contract

- `Tool.execute(arguments, idempotency_key)` performs the side effect.
- Raise `UnknownOutcome` when the request may have succeeded but its response is unavailable.
- `Tool.inspect(arguments, idempotency_key)` checks external state without causing a side effect.
- `Runtime.recover(action_id)` calls the inspector and marks the action `success` or `failed`.
- Repeating a successful action with the same tool and idempotency key returns the stored result without executing the tool again.

## Run the tests

This project intentionally uses only the Python standard library for the first slice:

```bash
python3 -m unittest discover -s tests -v
```

## Run the demo

```bash
python3 -m examples.recovery_demo
```

The demo simulates a GitHub-like issue API that creates an issue and then loses its response. Recovery finds the existing issue and avoids a duplicate create call.

## Deliberately out of scope for this slice

- LLM planning
- Framework integrations
- Automatic retries
- Browser automation
- A web dashboard
- Model-based verification

The next slice should add an explicit retry operation for actions that were verified as absent, plus a GitHub adapter and a CLI for inspecting action history.
