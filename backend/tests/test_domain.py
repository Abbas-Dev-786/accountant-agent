from datetime import datetime, timezone
from decimal import Decimal
import unittest

from app.domain import (
    ActionStatus,
    CloseService,
    DeploymentConfig,
    JournalLine,
    JournalProposal,
    PolicyError,
    RunState,
    SourceBatch,
    SourceRecordVersion,
)


def demo_deployment() -> DeploymentConfig:
    return DeploymentConfig("demo-us", "demo", "synthetic", "US", "USD", "controller-1")


def ready_batch() -> SourceBatch:
    record = SourceRecordVersion(
        "version-1", "plaid", "transaction-1", "hash-1", datetime.now(timezone.utc)
    )
    return SourceBatch(
        "batch-1", "plaid", "sandbox", "cursor-1", datetime.now(timezone.utc), (record,)
    )


def proposal() -> JournalProposal:
    return JournalProposal(
        "proposal-1",
        "2026-07-31",
        "Accrual adjustment",
        (
            JournalLine("610", Decimal("100"), Decimal("0"), ("evidence-1",)),
            JournalLine("200", Decimal("0"), Decimal("100"), ("evidence-1",)),
        ),
    )


class DeploymentGuardTests(unittest.TestCase):
    def test_demo_cannot_use_live_data(self) -> None:
        with self.assertRaises(PolicyError):
            DeploymentConfig("demo-us", "demo", "live", "US", "USD", "controller-1")

    def test_production_cannot_use_synthetic_data(self) -> None:
        with self.assertRaises(PolicyError):
            DeploymentConfig("us", "production", "synthetic", "US", "USD", "controller-1")


class CloseWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = CloseService(demo_deployment())
        self.run = self.service.create_run("org-1", "2026-07-01", "2026-07-31")
        self.service.begin_sync(self.run)
        self.service.build_snapshot(self.run, [ready_batch()])
        self.package_hash = self.service.prepare_for_review(self.run, [proposal()])

    def test_snapshot_is_bound_to_demo_deployment(self) -> None:
        self.assertEqual(self.run.snapshot.data_class, "synthetic")
        self.assertEqual(self.run.snapshot.source_batch_ids, ("batch-1",))
        self.assertEqual(self.run.state, RunState.AWAITING_APPROVAL)

    def test_snapshot_rejects_a_production_source_in_demo(self) -> None:
        service = CloseService(demo_deployment())
        run = service.create_run("org-1", "2026-07-01", "2026-07-31")
        service.begin_sync(run)
        production_batch = SourceBatch(
            "batch-prod", "xero", "production", "watermark", datetime.now(timezone.utc), ()
        )
        with self.assertRaises(PolicyError):
            service.build_snapshot(run, [production_batch])

    def test_snapshot_rejects_duplicate_provider_records(self) -> None:
        service = CloseService(demo_deployment())
        run = service.create_run("org-1", "2026-07-01", "2026-07-31")
        service.begin_sync(run)
        duplicate = SourceRecordVersion(
            "version-2", "plaid", "transaction-1", "hash-2", datetime.now(timezone.utc)
        )
        duplicate_batch = SourceBatch(
            "batch-2", "plaid", "sandbox", "cursor-2", datetime.now(timezone.utc), (duplicate,)
        )
        with self.assertRaises(PolicyError):
            service.build_snapshot(run, [ready_batch(), duplicate_batch])

    def test_only_configured_controller_can_approve(self) -> None:
        with self.assertRaises(PolicyError):
            self.service.approve(self.run, "someone-else", self.package_hash)

    def test_package_without_journals_completes_on_approval(self) -> None:
        service = CloseService(demo_deployment())
        run = service.create_run("org-1", "2026-07-01", "2026-07-31")
        service.begin_sync(run)
        service.build_snapshot(run, [ready_batch()])
        package_hash = service.prepare_for_review(run, [])
        service.approve(run, "controller-1", package_hash)
        self.assertEqual(run.state, RunState.APPROVED)

    def test_action_requires_exact_read_back(self) -> None:
        self.service.approve(self.run, "controller-1", self.package_hash)
        action = self.service.prepare_xero_action(self.run, proposal())
        result = self.service.reconcile_xero_action(
            self.run, action.action_id, "xero-123", action.expected_narration, action.request_hash
        )
        self.assertEqual(result.status, ActionStatus.SUCCEEDED)
        self.assertEqual(self.run.state, RunState.APPROVED)

    def test_ambiguous_outcome_cannot_be_retried_as_success(self) -> None:
        self.service.approve(self.run, "controller-1", self.package_hash)
        action = self.service.prepare_xero_action(self.run, proposal())
        result = self.service.reconcile_xero_action(self.run, action.action_id, None, None, None)
        self.assertEqual(result.status, ActionStatus.OUTCOME_UNKNOWN)
        self.assertEqual(self.run.state, RunState.ACTION_FAILED)

    def test_tampered_xero_draft_is_never_accepted(self) -> None:
        self.service.approve(self.run, "controller-1", self.package_hash)
        action = self.service.prepare_xero_action(self.run, proposal())
        result = self.service.reconcile_xero_action(
            self.run, action.action_id, "xero-123", "manually edited narration", action.request_hash
        )
        self.assertEqual(result.status, ActionStatus.FAILED)
        self.assertEqual(self.run.state, RunState.ACTION_FAILED)

    def test_journal_must_balance(self) -> None:
        with self.assertRaises(PolicyError):
            JournalProposal(
                "bad", "2026-07-31", "Bad adjustment",
                (
                    JournalLine("610", Decimal("100"), Decimal("0"), ("evidence-1",)),
                    JournalLine("200", Decimal("0"), Decimal("50"), ("evidence-1",)),
                ),
            )


if __name__ == "__main__":
    unittest.main()
