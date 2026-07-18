from decimal import Decimal
import unittest

from app.actions import (
    ActionPolicyError,
    ReviewPackage,
    XeroActionStatus,
    XeroDraftRecord,
    XeroPolicyGateway,
)
from app.domain import JournalLine, JournalProposal


def proposal():
    return JournalProposal(
        "proposal-1",
        "2026-07-31",
        "Accrual adjustment",
        (
            JournalLine("610", Decimal("100"), Decimal("0"), ("e-1",)),
            JournalLine("200", Decimal("0"), Decimal("100"), ("e-1",)),
        ),
    )


def package():
    return ReviewPackage.freeze("run-1", "snapshot-1", "snapshot-hash", (proposal(),))


class XeroClient:
    def __init__(self, *, tamper=False, unknown=False, crash_after_create=False):
        self.records = []
        self.tamper = tamper
        self.unknown = unknown
        self.crash_after_create = crash_after_create
        self.create_count = 0

    def search_manual_journals(self, marker):
        if self.unknown:
            return None
        return [record for record in self.records if marker in record.narration]

    def create_draft_manual_journal(self, request):
        self.create_count += 1
        record = XeroDraftRecord(
            "xero-1",
            "DRAFT",
            request.narration if not self.tamper else "tampered",
            request.journal_date,
            request.lines,
            request.request_hash,
        )
        self.records.append(record)
        if self.crash_after_create:
            raise TimeoutError("response lost")
        return record

    def get_manual_journal(self, journal_id):
        return next(record for record in self.records if record.journal_id == journal_id)


class ActionTests(unittest.TestCase):
    def test_controller_approval_and_exact_readback_create_one_draft(self):
        client = XeroClient()
        gateway = XeroPolicyGateway(client, "controller-1")
        frozen = package()
        decision = gateway.record_approval(frozen, "controller-1", frozen.package_hash)
        execution = gateway.create_draft(frozen, "proposal-1")
        self.assertEqual(decision.package_hash, frozen.package_hash)
        self.assertEqual(execution.status, XeroActionStatus.SUCCEEDED)
        self.assertEqual(execution.xero_journal_id, "xero-1")
        self.assertEqual(client.create_count, 1)
        self.assertEqual(gateway.create_draft(frozen, "proposal-1").xero_journal_id, "xero-1")
        self.assertEqual(client.create_count, 1)

    def test_wrong_controller_or_changed_package_cannot_write(self):
        gateway = XeroPolicyGateway(XeroClient(), "controller-1")
        frozen = package()
        with self.assertRaises(ActionPolicyError):
            gateway.record_approval(frozen, "other-user", frozen.package_hash)
        with self.assertRaises(ActionPolicyError):
            gateway.record_approval(frozen, "controller-1", "changed")
        with self.assertRaises(ActionPolicyError):
            gateway.create_draft(frozen, "proposal-1")

    def test_tampered_draft_fails_and_unknown_search_stops(self):
        frozen = package()
        tampered = XeroPolicyGateway(XeroClient(tamper=True), "controller-1")
        tampered.record_approval(frozen, "controller-1", frozen.package_hash)
        self.assertEqual(tampered.create_draft(frozen, "proposal-1").status, XeroActionStatus.FAILED)
        unknown = XeroPolicyGateway(XeroClient(unknown=True), "controller-1")
        unknown.record_approval(frozen, "controller-1", frozen.package_hash)
        self.assertEqual(unknown.create_draft(frozen, "proposal-1").status, XeroActionStatus.OUTCOME_UNKNOWN)

    def test_crash_after_create_is_reconciled_by_marker(self):
        frozen = package()
        client = XeroClient(crash_after_create=True)
        gateway = XeroPolicyGateway(client, "controller-1")
        gateway.record_approval(frozen, "controller-1", frozen.package_hash)
        execution = gateway.create_draft(frozen, "proposal-1")
        self.assertEqual(execution.status, XeroActionStatus.SUCCEEDED)
        self.assertEqual(client.create_count, 1)

    def test_gateway_has_no_posting_surface(self):
        self.assertFalse(hasattr(XeroPolicyGateway, "post"))
        self.assertFalse(hasattr(XeroPolicyGateway, "delete"))


if __name__ == "__main__":
    unittest.main()
