from datetime import date
from decimal import Decimal
import unittest

from app.reconciliation import (
    BankTransaction,
    ControlFailure,
    LedgerTransaction,
    ReconciliationConfig,
    ReconciliationStatus,
    reconcile,
)
from app.reports import AccountingEntry, build_journal_proposal, compute_reports
from app.domain import JournalLine, PolicyError


DAY = date(2026, 7, 10)


def bank(transaction_id, amount, *, status="posted", fee_amount=Decimal("0")):
    return BankTransaction(transaction_id, Decimal(amount), "USD", DAY, (f"bank:{transaction_id}",), status, fee_amount=fee_amount)


def ledger(transaction_id, amount, accounting_date=DAY):
    return LedgerTransaction(transaction_id, Decimal(amount), "USD", accounting_date, (f"ledger:{transaction_id}",), "100")


class ReconciliationTests(unittest.TestCase):
    def test_exact_date_window_aggregate_and_fee_matches_are_explicit(self):
        result = reconcile(
            [
                bank("b-exact", "100"),
                bank("b-window", "50"),
                bank("b-group", "30"),
                bank("b-fee", "88", fee_amount=Decimal("2")),
            ],
            [
                ledger("l-exact", "100"),
                ledger("l-window", "50", date(2026, 7, 12)),
                ledger("l-a", "10"),
                ledger("l-b", "20"),
                ledger("l-fee", "90"),
            ],
            ReconciliationConfig(date_window_days=3, fee_tolerance=Decimal("2")),
        )
        self.assertEqual(len(result.exceptions), 0)
        self.assertEqual({match.kind for match in result.matches}, {"exact", "date_window", "aggregate", "fee"})
        self.assertEqual(result.status_for("b-group"), ReconciliationStatus.MATCHED)

    def test_pending_and_duplicate_candidates_remain_exceptions(self):
        result = reconcile(
            [bank("pending", "10", status="pending"), bank("duplicate", "20")],
            [ledger("l-1", "20"), ledger("l-2", "20")],
            ReconciliationConfig(),
        )
        self.assertEqual(result.status_for("pending"), ReconciliationStatus.EXCLUDED_BY_POLICY)
        self.assertEqual(result.status_for("duplicate"), ReconciliationStatus.EXCEPTION)
        self.assertEqual({item.control_code for item in result.exceptions}, {"pending_transaction", "duplicate_candidate", "unmatched_ledger"})


class ReportTests(unittest.TestCase):
    def entries(self):
        return (
            AccountingEntry("a", "100", "Cash", "asset", Decimal("100"), Decimal("0"), DAY, ("e-cash",)),
            AccountingEntry("b", "200", "Payables", "liability", Decimal("0"), Decimal("40"), DAY, ("e-ap",)),
            AccountingEntry("c", "300", "Equity", "equity", Decimal("0"), Decimal("50"), DAY, ("e-equity",)),
            AccountingEntry("d", "400", "Revenue", "income", Decimal("0"), Decimal("30"), DAY, ("e-revenue",)),
            AccountingEntry("e", "500", "Expense", "expense", Decimal("20"), Decimal("0"), DAY, ("e-expense",)),
        )

    def test_reports_enforce_trial_balance_equation_and_cash(self):
        reports = compute_reports(self.entries(), self.entries(), bank_total=Decimal("100"), ledger_cash_total=Decimal("100"), cash_evidence_ids=("e-cash",))
        self.assertEqual(reports.adjusted_trial_balance.total_debit, Decimal("120"))
        self.assertEqual(dict(reports.profit_and_loss)["net_income"], Decimal("10"))

    def test_unbalanced_cash_or_accounting_equation_fails_closed(self):
        with self.assertRaises(ControlFailure):
            compute_reports(self.entries(), self.entries(), bank_total=Decimal("99"), ledger_cash_total=Decimal("100"), cash_evidence_ids=("e-cash",))
        invalid = self.entries()[:-1]
        with self.assertRaises(ControlFailure):
            compute_reports(invalid, invalid, bank_total=Decimal("100"), ledger_cash_total=Decimal("100"), cash_evidence_ids=("e-cash",))

    def test_journal_proposal_requires_current_account_codes(self):
        lines = (JournalLine("610", Decimal("10"), Decimal("0"), ("e-1",)), JournalLine("200", Decimal("0"), Decimal("10"), ("e-1",)))
        proposal = build_journal_proposal("p-1", "2026-07-31", "Accrual", lines, frozenset({"610", "200"}))
        self.assertEqual(proposal.proposal_id, "p-1")
        with self.assertRaises(PolicyError):
            build_journal_proposal("p-2", "2026-07-31", "Accrual", lines, frozenset({"610"}))


if __name__ == "__main__":
    unittest.main()
