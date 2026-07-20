import unittest
from decimal import Decimal

from app.close_execution import SnapshotFact, derive_close_execution


def configuration(*, offset=None):
    value = {
        "xero_tenant_id": "tenant-1",
        "bank_mappings": [{"plaid_account_id": "account-1", "xero_account_code": "1000", "xero_account_name": "Checking"}],
        "matching_rules": {"date_window_days": 3, "fee_tolerance": "0", "materiality_threshold": "0", "pending_policy": "exception", "max_aggregate_size": 3},
        "permitted_journal_account_codes": ["1000", "2000"],
    }
    if offset:
        value["journal_adjustment_account_code"] = offset
    return value


class CloseExecutionTests(unittest.TestCase):
    def test_reconciles_frozen_normalized_records_with_stable_ids(self):
        facts = (
            SnapshotFact("plaid:t-1:v1", "plaid", "t-1", {"transaction_id": "t-1", "account_id": "account-1", "amount": "12.50", "date": "2026-07-02", "iso_currency_code": "USD", "name": "Vendor"}, "2026-07-02", "USD"),
            SnapshotFact("xero:x-1:v1", "xero", "x-1", {"BankTransactionID": "x-1", "Amount": "12.50", "Date": "2026-07-02", "CurrencyCode": "USD", "BankAccount": {"Code": "1000"}, "Type": "RECEIVE", "record_type": "bank_transaction"}, "2026-07-02", "USD"),
        )
        first = derive_close_execution(facts, configuration())
        second = derive_close_execution(facts, configuration())
        self.assertEqual(len(first.reconciliation.matches), 1)
        self.assertEqual(first.reconciliation.matches[0].match_id, second.reconciliation.matches[0].match_id)
        self.assertEqual(first.reconciliation.exceptions, ())
        self.assertEqual(first.report_control_status, "unavailable")

    def test_unmatched_bank_generates_only_explicit_offset_proposal(self):
        facts = (
            SnapshotFact("plaid:t-1:v1", "plaid", "t-1", {"transaction_id": "t-1", "account_id": "account-1", "amount": "12.50", "date": "2026-07-02", "iso_currency_code": "USD"}, "2026-07-02", "USD"),
        )
        without_offset = derive_close_execution(facts, configuration())
        with_offset = derive_close_execution(facts, configuration(offset="2000"))
        self.assertEqual(len(without_offset.reconciliation.exceptions), 1)
        self.assertEqual(without_offset.proposals, ())
        self.assertEqual(len(with_offset.proposals), 1)
        proposal = with_offset.proposals[0]
        self.assertEqual({line.account_code for line in proposal.lines}, {"1000", "2000"})
        self.assertEqual(sum((line.debit for line in proposal.lines), Decimal("0")), sum((line.credit for line in proposal.lines), Decimal("0")))


if __name__ == "__main__":
    unittest.main()
