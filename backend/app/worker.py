"""Deterministic close-run task orchestration and webhook recovery primitives.

The state machine is storage-agnostic. The in-memory implementation is used by
tests and local development; the same transitions can be persisted by the
Supabase repository without changing retry, lease, or cancellation semantics.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Mapping, Sequence


class WorkflowError(RuntimeError):
    """Raised when a task or webhook transition violates the workflow policy."""


class TaskState(str, Enum):
    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    BLOCKED = "blocked"
    FAILED = "failed"
    CANCELLATION_REQUESTED = "cancellation_requested"
    CANCELLED = "cancelled"


class FailureClass(str, Enum):
    RETRYABLE = "retryable"
    BLOCKED = "blocked"
    FATAL = "fatal"


@dataclass(frozen=True)
class TaskDefinition:
    task_key: str
    dependencies: tuple[str, ...] = ()
    max_attempts: int = 3

    def __post_init__(self) -> None:
        if not self.task_key or len(set(self.dependencies)) != len(self.dependencies):
            raise WorkflowError("task keys and dependencies must be unique and non-empty")
        if self.task_key in self.dependencies:
            raise WorkflowError("a task cannot depend on itself")
        if self.max_attempts < 1:
            raise WorkflowError("max_attempts must be positive")


@dataclass
class TaskRecord:
    definition: TaskDefinition
    state: TaskState = TaskState.PENDING
    attempt: int = 0
    lease_owner: str | None = None
    lease_expires_at: datetime | None = None
    last_error: str | None = None


@dataclass(frozen=True)
class WorkflowEvent:
    cursor: int
    run_id: str
    event_type: str
    payload: Mapping[str, object]
    created_at: datetime


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class CloseRunWorkflow:
    """A bounded task DAG with leases, retries, cancellation, and event replay."""

    def __init__(self, run_id: str, *, lease_seconds: int = 60) -> None:
        if not run_id or lease_seconds < 1:
            raise WorkflowError("run_id and positive lease duration are required")
        self.run_id = run_id
        self.lease_seconds = lease_seconds
        self.tasks: dict[str, TaskRecord] = {}
        self.events: list[WorkflowEvent] = []
        self.cancel_requested = False

    def add_task(self, definition: TaskDefinition) -> TaskRecord:
        if definition.task_key in self.tasks:
            raise WorkflowError("task key already exists")
        missing = set(definition.dependencies) - set(self.tasks)
        if missing:
            raise WorkflowError(f"task dependencies must be declared first: {sorted(missing)}")
        record = TaskRecord(definition)
        self.tasks[definition.task_key] = record
        self._refresh_ready()
        self._emit("task_created", {"task_key": definition.task_key})
        return record

    def _emit(self, event_type: str, payload: Mapping[str, object], now: datetime | None = None) -> None:
        self.events.append(WorkflowEvent(len(self.events) + 1, self.run_id, event_type, dict(payload), now or utcnow()))

    def _refresh_ready(self) -> None:
        for record in self.tasks.values():
            if record.state != TaskState.PENDING:
                continue
            dependencies = [self.tasks[key].state for key in record.definition.dependencies]
            if any(state in (TaskState.BLOCKED, TaskState.FAILED, TaskState.CANCELLED) for state in dependencies):
                record.state = TaskState.BLOCKED
                record.last_error = "dependency did not succeed"
            elif all(state == TaskState.SUCCEEDED for state in dependencies):
                record.state = TaskState.CANCELLED if self.cancel_requested else TaskState.READY

    def claim_ready(self, worker_id: str, *, now: datetime | None = None) -> TaskRecord | None:
        if not worker_id:
            raise WorkflowError("worker_id is required")
        current = now or utcnow()
        self._expire_leases(current)
        self._refresh_ready()
        for record in self.tasks.values():
            if record.state != TaskState.READY:
                continue
            record.state = TaskState.RUNNING
            record.attempt += 1
            record.lease_owner = worker_id
            record.lease_expires_at = current + timedelta(seconds=self.lease_seconds)
            self._emit("task_claimed", {"task_key": record.definition.task_key, "worker_id": worker_id, "attempt": record.attempt}, current)
            return record
        return None

    def _expire_leases(self, now: datetime) -> None:
        for record in self.tasks.values():
            if record.state not in (TaskState.RUNNING, TaskState.CANCELLATION_REQUESTED):
                continue
            if record.lease_expires_at is None or record.lease_expires_at > now:
                continue
            if record.attempt < record.definition.max_attempts and not self.cancel_requested:
                record.state = TaskState.READY
                record.lease_owner = None
                record.lease_expires_at = None
                record.last_error = "lease expired; retry scheduled"
                self._emit("task_lease_expired", {"task_key": record.definition.task_key}, now)
            else:
                record.state = TaskState.CANCELLED if self.cancel_requested else TaskState.FAILED
                record.lease_owner = None
                record.lease_expires_at = None
                record.last_error = "lease expired after retry limit" if not self.cancel_requested else "cancelled after lease expiry"
                self._emit("task_terminal", {"task_key": record.definition.task_key, "state": record.state.value}, now)

    def heartbeat(self, task_key: str, worker_id: str, *, now: datetime | None = None) -> TaskRecord:
        record = self._owned_running(task_key, worker_id)
        current = now or utcnow()
        record.lease_expires_at = current + timedelta(seconds=self.lease_seconds)
        self._emit("task_heartbeat", {"task_key": task_key, "worker_id": worker_id}, current)
        return record

    def succeed(self, task_key: str, worker_id: str, *, now: datetime | None = None) -> TaskRecord:
        record = self._owned_running(task_key, worker_id)
        record.state = TaskState.CANCELLED if self.cancel_requested else TaskState.SUCCEEDED
        record.lease_owner = None
        record.lease_expires_at = None
        self._emit("task_completed", {"task_key": task_key, "state": record.state.value}, now)
        self._refresh_ready()
        return record

    def fail(self, task_key: str, worker_id: str, failure: FailureClass, message: str, *, now: datetime | None = None) -> TaskRecord:
        if not message:
            raise WorkflowError("failure message is required")
        record = self._owned_running(task_key, worker_id)
        record.last_error = message
        record.lease_owner = None
        record.lease_expires_at = None
        if failure == FailureClass.RETRYABLE and record.attempt < record.definition.max_attempts and not self.cancel_requested:
            record.state = TaskState.READY
        elif failure == FailureClass.BLOCKED:
            record.state = TaskState.BLOCKED
        else:
            record.state = TaskState.CANCELLED if self.cancel_requested else TaskState.FAILED
        self._emit("task_failed", {"task_key": task_key, "class": failure.value, "state": record.state.value, "message": message}, now)
        self._refresh_ready()
        return record

    def request_cancel(self, *, now: datetime | None = None) -> None:
        self.cancel_requested = True
        for record in self.tasks.values():
            if record.state in (TaskState.PENDING, TaskState.READY):
                record.state = TaskState.CANCELLED
            elif record.state == TaskState.RUNNING:
                record.state = TaskState.CANCELLATION_REQUESTED
        self._emit("run_cancellation_requested", {"run_id": self.run_id}, now)

    def replay_events(self, after_cursor: int = 0, *, limit: int = 100) -> tuple[WorkflowEvent, ...]:
        if after_cursor < 0 or limit < 1:
            raise WorkflowError("event cursor and limit must be valid")
        return tuple(event for event in self.events if event.cursor > after_cursor)[:limit]

    def _owned_running(self, task_key: str, worker_id: str) -> TaskRecord:
        record = self.tasks.get(task_key)
        if record is None or record.state not in (TaskState.RUNNING, TaskState.CANCELLATION_REQUESTED):
            raise WorkflowError("task is not running")
        if record.lease_owner != worker_id:
            raise WorkflowError("worker does not own the task lease")
        if record.lease_expires_at is not None and record.lease_expires_at <= utcnow():
            raise WorkflowError("task lease has expired")
        return record


class WebhookVerifier:
    """HMAC verifier and replay-safe event receiver for provider webhooks."""

    def __init__(self, secret: bytes) -> None:
        if not secret:
            raise WorkflowError("webhook secret is required")
        self.secret = secret
        self.receipts: dict[tuple[str, str], str] = {}

    def signature(self, payload: bytes) -> str:
        return hmac.new(self.secret, payload, hashlib.sha256).hexdigest()

    def receive(self, provider: str, event_id: str, payload: bytes, signature: str) -> bool:
        if not provider or not event_id or not hmac.compare_digest(self.signature(payload), signature):
            raise WorkflowError("webhook signature or identity is invalid")
        payload_hash = hashlib.sha256(payload).hexdigest()
        key = (provider, event_id)
        previous = self.receipts.get(key)
        if previous is not None:
            if previous != payload_hash:
                raise WorkflowError("webhook event id was reused with a different payload")
            return False
        self.receipts[key] = payload_hash
        return True


def canonical_webhook_payload(value: Mapping[str, object]) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
