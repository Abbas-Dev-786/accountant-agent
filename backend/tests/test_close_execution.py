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
            SnapshotFact("plaid:t-2:v1", "plaid", "t-2", {"transaction_id": "t-2", "account_id": "account-1", "amount": "-500.00", "date": "2026-07-02", "iso_currency_code": "USD", "name": "Customer"}, "2026-07-02", "USD"),
            # Xero's wire dates are not ISO and its values are unsigned.  A
            # SPEND is a Plaid-positive outflow; a RECEIVE is a negative
            # inflow.  Both assertions prevent a sign-inverted cash match.
            SnapshotFact("xero:x-1:v1", "xero", "x-1", {"BankTransactionID": "x-1", "Amount": "12.50", "Date": "/Date(1782950400000+0000)/", "CurrencyCode": "USD", "BankAccount": {"Code": "1000"}, "Type": "SPEND", "record_type": "bank_transaction"}, "2026-07-02", "USD"),
            SnapshotFact("xero:x-2:v1", "xero", "x-2", {"BankTransactionID": "x-2", "Amount": "500.00", "Date": "/Date(1782950400000+0000)/", "CurrencyCode": "USD", "BankAccount": {"Code": "1000"}, "Type": "RECEIVE", "record_type": "bank_transaction"}, "2026-07-02", "USD"),
        )
        first = derive_close_execution(facts, configuration())
        second = derive_close_execution(facts, configuration())
        self.assertEqual(len(first.reconciliation.matches), 2)
        self.assertEqual(first.reconciliation.matches[0].match_id, second.reconciliation.matches[0].match_id)
        self.assertEqual(first.reconciliation.exceptions, ())
        self.assertEqual(first.report_control_status, "unavailable")

    def test_payment_uses_bank_amount_direction_and_account_metadata(self):
        facts = (
            SnapshotFact("plaid:t-1:v1", "plaid", "t-1", {"transaction_id": "t-1", "account_id": "account-1", "amount": "50.00", "date": "2026-07-02", "iso_currency_code": "USD"}, "2026-07-02", "USD"),
            SnapshotFact(
                "xero:payment-1:v1", "xero", "payment-1",
                {
                    "PaymentID": "payment-1", "BankAmount": "50.00", "Amount": "49.00",
                    "Date": "/Date(1782950400000+0000)/", "PaymentType": "ACCPAYPAYMENT",
                    "BankAccount": {"AccountID": "account-1000"},
                    "CurrencyRate": "1.02",
                },
                "2026-07-02", None,
            ),
            SnapshotFact(
                "xero:account-1000:v1", "xero", "account-1000",
                {"AccountID": "account-1000", "Code": "1000", "Name": "Checking", "CurrencyCode": "USD", "Type": "BANK", "record_type": "account"},
                None, "USD",
            ),
        )
        execution = derive_close_execution(facts, configuration())
        self.assertEqual(len(execution.reconciliation.matches), 1)
        self.assertEqual(execution.reconciliation.exceptions, ())

    def test_manual_journal_without_account_type_preserves_cash_controls(self):
        facts = (
            SnapshotFact("plaid:t-1:v1", "plaid", "t-1", {"transaction_id": "t-1", "account_id": "account-1", "amount": "12.50", "date": "2026-07-02", "iso_currency_code": "USD"}, "2026-07-02", "USD"),
            SnapshotFact("xero:x-1:v1", "xero", "x-1", {"BankTransactionID": "x-1", "Amount": "12.50", "Date": "/Date(1782950400000+0000)/", "CurrencyCode": "USD", "BankAccount": {"Code": "1000"}, "Type": "SPEND"}, "2026-07-02", "USD"),
            SnapshotFact("xero:journal-1:v1", "xero", "journal-1", {"ManualJournalID": "journal-1", "Date": "/Date(1782950400000+0000)/", "JournalLines": [{"AccountCode": "1000", "LineAmount": "12.50"}, {"AccountCode": "2000", "LineAmount": "-12.50"}]}, "2026-07-02", "USD"),
        )
        execution = derive_close_execution(facts, configuration())
        self.assertEqual(len(execution.reconciliation.matches), 1)
        self.assertEqual(execution.reconciliation.exceptions, ())
        self.assertEqual(execution.report_control_status, "unavailable")
        self.assertIn("without account-type metadata", execution.report["reason"])

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

    def test_plaid_pending_boolean_is_never_reconciled_as_posted(self):
        facts = (
            SnapshotFact("plaid:t-1:v1", "plaid", "t-1", {"transaction_id": "t-1", "account_id": "account-1", "amount": "12.50", "date": "2026-07-02", "iso_currency_code": "USD", "pending": True}, "2026-07-02", "USD"),
            SnapshotFact("xero:x-1:v1", "xero", "x-1", {"BankTransactionID": "x-1", "Amount": "12.50", "Date": "/Date(1782950400000+0000)/", "CurrencyCode": "USD", "BankAccount": {"Code": "1000"}, "Type": "SPEND"}, "2026-07-02", "USD"),
        )
        execution = derive_close_execution(facts, configuration())
        self.assertEqual(execution.reconciliation.matches, ())
        self.assertEqual([item.control_code for item in execution.reconciliation.exceptions], ["pending_transaction", "unmatched_ledger"])

    def test_voided_xero_cash_record_cannot_match_a_live_bank_transaction(self):
        facts = (
            SnapshotFact("plaid:t-1:v1", "plaid", "t-1", {"transaction_id": "t-1", "account_id": "account-1", "amount": "12.50", "date": "2026-07-02", "iso_currency_code": "USD"}, "2026-07-02", "USD"),
            SnapshotFact("xero:x-1:v1", "xero", "x-1", {"BankTransactionID": "x-1", "Amount": "12.50", "Date": "/Date(1782950400000+0000)/", "CurrencyCode": "USD", "BankAccount": {"Code": "1000"}, "Type": "SPEND", "Status": "VOIDED"}, "2026-07-02", "USD"),
        )
        execution = derive_close_execution(facts, configuration())
        self.assertEqual(execution.reconciliation.matches, ())
        self.assertEqual([item.control_code for item in execution.reconciliation.exceptions], ["unmatched_bank"])


if __name__ == "__main__":
    unittest.main()
