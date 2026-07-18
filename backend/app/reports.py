"""Deterministic journal, trial-balance, and close-report facts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Iterable

from .domain import JournalLine, JournalProposal, PolicyError
from .reconciliation import ControlFailure


@dataclass(frozen=True)
class AccountingEntry:
    entry_id: str
    account_code: str
    account_name: str
    category: str
    debit: Decimal
    credit: Decimal
    accounting_date: date
    evidence_ids: tuple[str, ...]
    pro_forma: bool = False

    def __post_init__(self) -> None:
        if bool(self.debit) == bool(self.credit) or self.debit < 0 or self.credit < 0:
            raise ControlFailure("accounting entry must have one non-negative side")
        if not self.evidence_ids:
            raise ControlFailure("accounting entry requires evidence")
        if self.category not in {"asset", "liability", "equity", "income", "expense"}:
            raise ControlFailure("unknown account category")


@dataclass(frozen=True)
class TrialBalanceLine:
    account_code: str
    account_name: str
    debit: Decimal
    credit: Decimal
    evidence_ids: tuple[str, ...]


@dataclass(frozen=True)
class TrialBalance:
    lines: tuple[TrialBalanceLine, ...]
    pro_forma: bool = False

    @property
    def total_debit(self) -> Decimal:
        return sum((line.debit for line in self.lines), Decimal("0"))

    @property
    def total_credit(self) -> Decimal:
        return sum((line.credit for line in self.lines), Decimal("0"))

    def validate(self) -> None:
        if self.total_debit != self.total_credit:
            raise ControlFailure("trial balance does not balance")


@dataclass(frozen=True)
class CashReconciliation:
    bank_total: Decimal
    ledger_cash_total: Decimal
    difference: Decimal
    evidence_ids: tuple[str, ...]

    def validate(self) -> None:
        if self.difference != 0:
            raise ControlFailure("cash reconciliation does not tie")


@dataclass(frozen=True)
class CloseReports:
    unadjusted_trial_balance: TrialBalance
    adjusted_trial_balance: TrialBalance
    profit_and_loss: tuple[tuple[str, Decimal], ...]
    balance_sheet: tuple[tuple[str, Decimal], ...]
    cash_reconciliation: CashReconciliation


def build_trial_balance(entries: Iterable[AccountingEntry], *, pro_forma: bool = False) -> TrialBalance:
    grouped: dict[tuple[str, str], list[object]] = {}
    for entry in entries:
        key = (entry.account_code, entry.account_name)
        if key not in grouped:
            grouped[key] = [Decimal("0"), Decimal("0"), set()]
        grouped[key][0] += entry.debit
        grouped[key][1] += entry.credit
        grouped[key][2].update(entry.evidence_ids)
    lines = tuple(
        TrialBalanceLine(code, name, values[0], values[1], tuple(sorted(values[2])))
        for (code, name), values in sorted(grouped.items())
    )
    result = TrialBalance(lines, pro_forma)
    result.validate()
    return result


def build_journal_proposal(
    proposal_id: str,
    journal_date: str,
    narration: str,
    lines: Iterable[JournalLine],
    valid_account_codes: frozenset[str],
) -> JournalProposal:
    lines_tuple = tuple(lines)
    if any(line.account_code not in valid_account_codes for line in lines_tuple):
        raise PolicyError("journal proposal contains an invalid account code")
    return JournalProposal(proposal_id, journal_date, narration, lines_tuple)


def compute_reports(
    unadjusted_entries: Iterable[AccountingEntry],
    pro_forma_entries: Iterable[AccountingEntry],
    *,
    bank_total: Decimal,
    ledger_cash_total: Decimal,
    cash_evidence_ids: tuple[str, ...],
) -> CloseReports:
    unadjusted_source = tuple(unadjusted_entries)
    pro_forma_source = tuple(pro_forma_entries)
    unadjusted = build_trial_balance(unadjusted_source)
    adjusted = build_trial_balance(pro_forma_source, pro_forma=True)
    all_entries = pro_forma_source
    income = sum((entry.credit - entry.debit for entry in all_entries if entry.category == "income"), Decimal("0"))
    expense = sum((entry.debit - entry.credit for entry in all_entries if entry.category == "expense"), Decimal("0"))
    net_income = income - expense
    assets = sum((entry.debit - entry.credit for entry in all_entries if entry.category == "asset"), Decimal("0"))
    liabilities = sum((entry.credit - entry.debit for entry in all_entries if entry.category == "liability"), Decimal("0"))
    equity = sum((entry.credit - entry.debit for entry in all_entries if entry.category == "equity"), Decimal("0"))
    if assets != liabilities + equity + net_income:
        raise ControlFailure("accounting equation does not balance")
    cash = CashReconciliation(bank_total, ledger_cash_total, bank_total - ledger_cash_total, cash_evidence_ids)
    cash.validate()
    return CloseReports(
        unadjusted,
        adjusted,
        (("income", income), ("expense", expense), ("net_income", net_income)),
        (("assets", assets), ("liabilities", liabilities), ("equity", equity), ("net_income", net_income)),
        cash,
    )
