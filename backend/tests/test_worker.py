from datetime import datetime, timedelta, timezone
import unittest

from app.worker import (
    CloseRunWorkflow,
    FailureClass,
    TaskDefinition,
    TaskState,
    WebhookVerifier,
    WorkflowError,
    canonical_webhook_payload,
)


class WorkerTests(unittest.TestCase):
    def test_dag_claims_in_order_and_releases_dependents(self):
        workflow = CloseRunWorkflow("run-1", lease_seconds=60)
        workflow.add_task(TaskDefinition("sync"))
        workflow.add_task(TaskDefinition("snapshot", ("sync",)))
        first = workflow.claim_ready("worker-1")
        self.assertEqual(first.definition.task_key, "sync")
        self.assertIsNone(workflow.claim_ready("worker-2"))
        workflow.succeed("sync", "worker-1")
        second = workflow.claim_ready("worker-2")
        self.assertEqual(second.definition.task_key, "snapshot")

    def test_retryable_failure_is_bounded_and_blocker_stops_dependents(self):
        workflow = CloseRunWorkflow("run-1", lease_seconds=60)
        workflow.add_task(TaskDefinition("sync", max_attempts=2))
        workflow.add_task(TaskDefinition("report", ("sync",)))
        task = workflow.claim_ready("worker-1")
        workflow.fail("sync", "worker-1", FailureClass.RETRYABLE, "timeout")
        self.assertEqual(task.state, TaskState.READY)
        task = workflow.claim_ready("worker-1")
        workflow.fail("sync", "worker-1", FailureClass.RETRYABLE, "timeout again")
        self.assertEqual(task.state, TaskState.FAILED)
        self.assertEqual(workflow.tasks["report"].state, TaskState.BLOCKED)

    def test_lease_expiry_requeues_once_and_event_replay_is_cursor_based(self):
        base = datetime(2026, 7, 18, tzinfo=timezone.utc)
        workflow = CloseRunWorkflow("run-1", lease_seconds=60)
        workflow.add_task(TaskDefinition("sync", max_attempts=2))
        workflow.claim_ready("worker-1", now=base)
        workflow.claim_ready("worker-2", now=base + timedelta(seconds=61))
        self.assertEqual(workflow.tasks["sync"].attempt, 2)
        events = workflow.replay_events(after_cursor=1, limit=2)
        self.assertEqual(events[0].event_type, "task_claimed")

    def test_cancellation_stops_ready_work_and_running_worker_can_finish_safely(self):
        workflow = CloseRunWorkflow("run-1")
        workflow.add_task(TaskDefinition("sync"))
        workflow.add_task(TaskDefinition("report", ("sync",)))
        workflow.claim_ready("worker-1")
        workflow.request_cancel()
        self.assertEqual(workflow.tasks["sync"].state, TaskState.CANCELLATION_REQUESTED)
        workflow.succeed("sync", "worker-1")
        self.assertEqual(workflow.tasks["sync"].state, TaskState.CANCELLED)
        self.assertEqual(workflow.tasks["report"].state, TaskState.CANCELLED)

    def test_webhook_verification_is_idempotent_but_payload_reuse_blocks(self):
        verifier = WebhookVerifier(b"webhook-secret")
        payload = canonical_webhook_payload({"event": "updated", "id": "evt-1"})
        signature = verifier.signature(payload)
        self.assertTrue(verifier.receive("plaid", "evt-1", payload, signature))
        self.assertFalse(verifier.receive("plaid", "evt-1", payload, signature))
        with self.assertRaises(WorkflowError):
            verifier.receive("plaid", "evt-1", b'{"event":"different"}', signature)


if __name__ == "__main__":
    unittest.main()
