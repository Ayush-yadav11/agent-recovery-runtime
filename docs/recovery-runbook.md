# Recovery runbook

This runbook covers a side-effecting agent action whose response may be lost after the external service accepts the request.

## Status meanings

| Status | Meaning | Operator action |
|---|---|---|
| `running` | The action record exists and execution has started. | Do not start a second create with the same key. |
| `success` | The side effect returned normally or was found during inspection. | Continue the workflow. |
| `failed` | The side effect returned a normal error, or no inspector exists. | Fix the input or integration, then start a new action with a deliberate key. |
| `unknown` | The external outcome is not known. | Run read-only verification. Do not retry yet. |
| `verified_absent` | Read-only verification found no matching side effect. | A human or policy may call `Runtime.retry(action_id)`. |

## Normal recovery

1. Persist the returned `action_id` with the workflow request.
2. If `Runtime.execute` returns `success`, use its stored result.
3. If it returns `unknown`, call `Runtime.recover(action_id)`.
4. If recovery returns `success`, use the matched external object.
5. If recovery returns `verified_absent`, review the arguments and call `Runtime.retry(action_id)` only when a retry is justified.
6. If recovery remains `unknown`, stop and send the action to human review.

The runtime never retries an `unknown` action automatically.

## Restart recovery

Use the same action database and LangGraph thread ID after a process restart:

```python
from langgraph.checkpoint.sqlite import SqliteSaver

with SqliteSaver.from_conn_string("checkpoints.db") as checkpointer:
    graph = build_recovery_graph(
        runtime,
        "create_issue",
        checkpointer=checkpointer,
    )
    result = graph.invoke(input_state, config={"configurable": {"thread_id": request_id}})
```

If the process stops after the side effect and before inspection, rebuild the graph with the same checkpoint database and invoke the same thread. The action journal and checkpointer preserve the decision point. The inspector must remain read-only.

## GitHub-specific procedure

`GitHubClient` adds this exact body marker:

```text
<!-- agent-recovery:idempotency-key=<key> -->
```

When a GitHub create response is lost:

1. Keep the original owner, repository, title, body, and idempotency key.
2. Inspect issues with `find_issue_by_idempotency_key`.
3. Treat a matching issue as `success`.
4. Treat no match as `verified_absent`, not as proof that the first request never committed outside the inspected result set.
5. Retry only through `Runtime.retry` after reviewing the absence.

## Safety checks before retry

- The idempotency key is unchanged.
- The arguments hash matches the original action.
- The inspector searched the correct owner and repository.
- The inspector covered both open and closed issues.
- The latest action for the key is the `verified_absent` action being retried.
- No operator or workflow already retried that latest action.

## Failure injection checks

Run the complete suite before changing recovery policy:

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q agent_recovery tests examples
```

The tests cover response loss, inspector timeout, normal HTTP failure, restart resume, duplicate prevention, and explicit retry gating.
