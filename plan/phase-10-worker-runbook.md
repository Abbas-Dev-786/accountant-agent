# Phase 10 Worker and Recovery Runbook

Phase 10 adds the close-run task state machine before it is connected to a
Supabase-backed worker process. The state layer is deterministic and storage
agnostic, so a restart can recover from persisted task rows without inventing
new transitions.

## State guarantees

- Tasks become `ready` only after every declared dependency succeeds.
- Claims increment an attempt and carry a 60-second lease by default.
- Heartbeats extend only the current worker's lease.
- Retryable failures return to `ready` until `max_attempts`; blockers and fatal
  failures never silently retry.
- Cancellation marks queued work cancelled and lets a running worker finish
  into a cancelled terminal state; it does not create a new external action.
- Events have monotonic cursors and can be replayed after an SSE reconnect.
- Webhook HMAC verification and `(provider,event_id)` receipts make duplicate
  delivery idempotent and payload reuse a blocker.

## Verification

```sh
cd backend
.venv/bin/python -m unittest discover -s tests -v
```

`app/worker.py` is currently an in-memory reference implementation. Phase 10
is not accepted until its state transitions are persisted through the
Supabase `workflow.tasks`, `workflow.task_dependencies`, and audit tables and
the worker restart/lease drills are captured.
