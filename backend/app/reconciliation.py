"""Deterministic bank-to-ledger reconciliation controls."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import Enum
from itertools import combinations
from typing import Iterable
from uuid import uuid4

from .domain import PolicyError


class ReconciliationStatus(str, Enum):
    MATCHED = "matched"
    EXCLUDED_BY_POLICY = "excluded_by_policy"
    EXCEPTION = "exception"


class ControlFailure(PolicyError):
    """Raised when a report or reconciliation invariant cannot be proven."""


@dataclass(frozen=True)
class BankTransaction:
    transaction_id: str
    amount: Decimal
    currency: str
    accounting_date: date
    source_evidence_ids: tuple[str, ...]
    status: str = "posted"
    description: str = ""
    fee_amount: Decimal = Decimal("0")

    def __post_init__(self) -> None:
        if self.fee_amount < 0:
            raise ControlFailure("bank fee cannot be negative")


@dataclass(frozen=True)
class LedgerTransaction:
    transaction_id: str
    amount: Decimal
    currency: str
    accounting_date: date
    source_evidence_ids: tuple[str, ...]
    account_code: str
    description: str = ""


@dataclass(frozen=True)
class ReconciliationConfig:
    date_window_days: int = 3
    allow_pending: bool = False
    max_aggregate_size: int = 3
    fee_tolerance: Decimal = Decimal("0.00")

    def __post_init__(self) -> None:
        if self.date_window_days < 0 or self.max_aggregate_size < 2:
            raise ControlFailure("reconciliation configuration is invalid")
        if self.fee_tolerance < 0:
            raise ControlFailure("fee tolerance cannot be negative")


@dataclass(frozen=True)
class MatchGroup:
    match_id: str
    bank_transaction_ids: tuple[str, ...]
    ledger_transaction_ids: tuple[str, ...]
    kind: str
    amount: Decimal
    currency: str
    evidence_ids: tuple[str, ...]


@dataclass(frozen=True)
class ReconciliationException:
    exception_id: str
    control_code: str
    source_transaction_ids: tuple[str, ...]
    evidence_ids: tuple[str, ...]
    amount: Decimal
    currency: str
    remediation: str
    status: str = "open"


@dataclass(frozen=True)
class ReconciliationResult:
    matches: tuple[MatchGroup, ...]
    exceptions: tuple[ReconciliationException, ...]
    statuses: tuple[tuple[str, ReconciliationStatus], ...]

    def status_for(self, transaction_id: str) -> ReconciliationStatus:
        return dict(self.statuses)[transaction_id]


def _evidence(*transactions: BankTransaction | LedgerTransaction) -> tuple[str, ...]:
    return tuple(dict.fromkeys(evidence_id for item in transactions for evidence_id in item.source_evidence_ids))


def _within_window(left: date, right: date, days: int) -> bool:
    return abs((left - right).days) <= days


def reconcile(
    bank_transactions: Iterable[BankTransaction],
    ledger_transactions: Iterable[LedgerTransaction],
    config: ReconciliationConfig,
) -> ReconciliationResult:
    banks = tuple(bank_transactions)
    ledgers = tuple(ledger_transactions)
    ledger_by_id = {item.transaction_id: item for item in ledgers}
    if len(ledger_by_id) != len(ledgers):
        raise ControlFailure("ledger transaction IDs must be unique")
    used_ledger_ids: set[str] = set()
    matches: list[MatchGroup] = []
    exceptions: list[ReconciliationException] = []
    statuses: dict[str, ReconciliationStatus] = {}

    for bank in banks:
        if bank.transaction_id in statuses:
            raise ControlFailure("bank transaction IDs must be unique")
        if bank.status == "pending" and not config.allow_pending:
            statuses[bank.transaction_id] = ReconciliationStatus.EXCLUDED_BY_POLICY
            exceptions.append(
                ReconciliationException(
                    str(uuid4()),
                    "pending_transaction",
                    (bank.transaction_id,),
                    bank.source_evidence_ids,
                    bank.amount,
                    bank.currency,
                    "Wait for a posted transaction or obtain an explicit policy exception.",
                )
            )
            continue
        def amount_matches(ledger: LedgerTransaction) -> bool:
            return ledger.amount == bank.amount or (
                bank.fee_amount > 0
                and bank.fee_amount <= config.fee_tolerance
                and ledger.amount == bank.amount + bank.fee_amount
            )

        candidates = [
            ledger
            for ledger in ledgers
            if ledger.transaction_id not in used_ledger_ids
            and ledger.currency == bank.currency
            and amount_matches(ledger)
            and _within_window(ledger.accounting_date, bank.accounting_date, config.date_window_days)
        ]
        if len(candidates) == 1:
            ledger = candidates[0]
            used_ledger_ids.add(ledger.transaction_id)
            statuses[bank.transaction_id] = ReconciliationStatus.MATCHED
            statuses[ledger.transaction_id] = ReconciliationStatus.MATCHED
            kind = "fee" if ledger.amount != bank.amount else ("exact" if ledger.accounting_date == bank.accounting_date else "date_window")
            matches.append(
                MatchGroup(
                    str(uuid4()),
                    (bank.transaction_id,),
                    (ledger.transaction_id,),
                    kind,
                    bank.amount,
                    bank.currency,
                    _evidence(bank, ledger),
                )
            )
            continue
        if len(candidates) > 1:
            statuses[bank.transaction_id] = ReconciliationStatus.EXCEPTION
            exceptions.append(
                ReconciliationException(
                    str(uuid4()),
                    "duplicate_candidate",
                    (bank.transaction_id,) + tuple(item.transaction_id for item in candidates),
                    _evidence(bank, *candidates),
                    bank.amount,
                    bank.currency,
                    "Resolve duplicate ledger candidates before close.",
                )
            )
            continue

        remaining = [
            ledger
            for ledger in ledgers
            if ledger.transaction_id not in used_ledger_ids
            and ledger.currency == bank.currency
            and _within_window(ledger.accounting_date, bank.accounting_date, config.date_window_days)
        ]
        aggregate: tuple[LedgerTransaction, ...] | None = None
        for size in range(2, config.max_aggregate_size + 1):
            candidates_by_size = [
                group for group in combinations(remaining, size) if sum((item.amount for item in group), Decimal("0")) == bank.amount
            ]
            if len(candidates_by_size) == 1:
                aggregate = candidates_by_size[0]
                break
            if len(candidates_by_size) > 1:
                statuses[bank.transaction_id] = ReconciliationStatus.EXCEPTION
                exceptions.append(
                    ReconciliationException(
                        str(uuid4()),
                        "duplicate_aggregate_candidate",
                        (bank.transaction_id,),
                        _evidence(bank, *candidates_by_size[0]),
                        bank.amount,
                        bank.currency,
                        "Resolve multiple aggregate ledger candidates before close.",
                    )
                )
                break
        if statuses.get(bank.transaction_id) == ReconciliationStatus.EXCEPTION:
            continue
        if aggregate:
            used_ledger_ids.update(item.transaction_id for item in aggregate)
            statuses[bank.transaction_id] = ReconciliationStatus.MATCHED
            for item in aggregate:
                statuses[item.transaction_id] = ReconciliationStatus.MATCHED
            matches.append(
                MatchGroup(
                    str(uuid4()),
                    (bank.transaction_id,),
                    tuple(item.transaction_id for item in aggregate),
                    "aggregate",
                    bank.amount,
                    bank.currency,
                    _evidence(bank, *aggregate),
                )
            )
            continue
        statuses[bank.transaction_id] = ReconciliationStatus.EXCEPTION
        exceptions.append(
            ReconciliationException(
                str(uuid4()),
                "unmatched_bank",
                (bank.transaction_id,),
                bank.source_evidence_ids,
                bank.amount,
                bank.currency,
                "Investigate the unmatched bank transaction and supporting evidence.",
            )
        )

    for ledger in ledgers:
        if ledger.transaction_id not in used_ledger_ids and ledger.transaction_id not in statuses:
            statuses[ledger.transaction_id] = ReconciliationStatus.EXCEPTION
            exceptions.append(
                ReconciliationException(
                    str(uuid4()),
                    "unmatched_ledger",
                    (ledger.transaction_id,),
                    ledger.source_evidence_ids,
                    ledger.amount,
                    ledger.currency,
                    "Investigate the unmatched ledger transaction and supporting evidence.",
                )
            )
    return ReconciliationResult(tuple(matches), tuple(exceptions), tuple(statuses.items()))
