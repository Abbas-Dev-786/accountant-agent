"""Deterministic execution over a frozen normalized source snapshot.

The ingestion layer deliberately preserves provider payloads rather than
silently manufacturing an accounting model.  This module is the narrow,
auditable projection used by the durable worker: it accepts only supported
field shapes, produces facts for reconciliation, and leaves unsupported Xero
records out of accounting controls rather than guessing their meaning.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from json import dumps
from typing import Mapping, Sequence

from .domain import JournalLine, JournalProposal
from .reconciliation import (
    BankTransaction,
    LedgerTransaction,
    ReconciliationConfig,
    ReconciliationException,
    ReconciliationResult,
    reconcile,
)
from .reports import AccountingEntry, CloseReports, compute_reports


class CloseExecutionError(ValueError):
    """A persisted record cannot be safely projected into an accounting fact."""


@dataclass(frozen=True)
class SnapshotFact:
    version_id: str
    provider: str
    provider_record_id: str
    payload: Mapping[str, object]
    accounting_date: str | None
    currency: str | None


@dataclass(frozen=True)
class DerivedCloseExecution:
    reconciliation: ReconciliationResult
    input_hash: str
    report: Mapping[str, object]
    report_hash: str
    report_control_status: str
    proposals: tuple[JournalProposal, ...]
    exception_facts: Mapping[str, tuple[Mapping[str, str], ...]]


def _decimal(value: object, label: str) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise CloseExecutionError(f"{label} is not a decimal") from exc


def _text(payload: Mapping[str, object], *names: str) -> str | None:
    for name in names:
        value = payload.get(name)
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


def _date(payload: Mapping[str, object], fallback: str | None) -> date:
    value = _text(payload, "accounting_date", "date", "Date", "transaction_date", "TransactionDate") or fallback
    if not value:
        raise CloseExecutionError("record has no accounting date")
    try:
        return date.fromisoformat(value[:10])
    except ValueError as exc:
        raise CloseExecutionError("record has an invalid accounting date") from exc


def _currency(payload: Mapping[str, object], fallback: str | None) -> str:
    value = _text(payload, "currency", "iso_currency_code", "CurrencyCode") or fallback
    if not value or len(value) != 3:
        raise CloseExecutionError("record has no ISO currency")
    return value.upper()


def _amount(payload: Mapping[str, object], *names: str) -> Decimal:
    for name in names:
        if name in payload and payload[name] is not None:
            return _decimal(payload[name], name)
    raise CloseExecutionError("record has no supported amount")


def _canonical(value: object) -> str:
    return dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _evidence_id(fact: SnapshotFact) -> str:
    return f"source:{fact.version_id}"


def _mapping_by_plaid_account(configuration: Mapping[str, object]) -> Mapping[str, Mapping[str, object]]:
    raw = configuration.get("bank_mappings")
    if not isinstance(raw, list):
        raise CloseExecutionError("persisted close mapping has no bank mappings")
    result: dict[str, Mapping[str, object]] = {}
    for item in raw:
        if not isinstance(item, Mapping):
            raise CloseExecutionError("persisted close mapping has an invalid bank mapping")
        account_id = _text(item, "plaid_account_id")
        code = _text(item, "xero_account_code")
        if not account_id or not code or account_id in result:
            raise CloseExecutionError("persisted close mapping has an invalid bank mapping")
        result[account_id] = item
    return result


def _bank_transactions(facts: Sequence[SnapshotFact], mapping: Mapping[str, Mapping[str, object]]) -> tuple[BankTransaction, ...]:
    result: list[BankTransaction] = []
    for fact in facts:
        if fact.provider != "plaid" or fact.payload.get("removed") is True:
            continue
        account_id = _text(fact.payload, "account_id")
        if account_id not in mapping:
            # Source sync rejects this in production; retaining the check here
            # prevents a malformed historic snapshot from entering controls.
            raise CloseExecutionError("snapshot contains a Plaid transaction outside its approved mapping")
        transaction_id = _text(fact.payload, "transaction_id", "id") or fact.provider_record_id
        amount = _amount(fact.payload, "amount")
        fee = _decimal(fact.payload.get("fee_amount", "0"), "fee_amount")
        result.append(
            BankTransaction(
                transaction_id=f"plaid:{transaction_id}",
                amount=amount,
                currency=_currency(fact.payload, fact.currency),
                accounting_date=_date(fact.payload, fact.accounting_date),
                source_evidence_ids=(_evidence_id(fact),),
                status=(_text(fact.payload, "status") or "posted").lower(),
                description=_text(fact.payload, "name", "merchant_name", "description") or "",
                fee_amount=fee,
            )
        )
    return tuple(result)


def _ledger_transactions(facts: Sequence[SnapshotFact]) -> tuple[LedgerTransaction, ...]:
    """Project only explicit Xero cash/bank transaction shapes.

    Invoices are intentionally not treated as settled cash. A source snapshot
    needs a Xero BankTransaction, payment, or journal line with an explicit
    amount and account before it can match a Plaid transaction.
    """
    result: list[LedgerTransaction] = []
    for fact in facts:
        if fact.provider != "xero":
            continue
        payload = fact.payload
        record_type = (_text(payload, "Type", "type", "record_type") or "").lower()
        account_code = _text(payload, "AccountCode", "account_code")
        explicit_amount = any(name in payload for name in ("amount", "Amount", "Total", "TotalAmount"))
        if explicit_amount and (account_code or "bank" in record_type or "payment" in record_type):
            transaction_id = _text(payload, "BankTransactionID", "PaymentID", "JournalID", "id", "transaction_id") or fact.provider_record_id
            result.append(
                LedgerTransaction(
                    transaction_id=f"xero:{transaction_id}",
                    amount=_amount(payload, "amount", "Amount", "Total", "TotalAmount"),
                    currency=_currency(payload, fact.currency),
                    accounting_date=_date(payload, fact.accounting_date),
                    source_evidence_ids=(_evidence_id(fact),),
                    account_code=account_code or "unmapped",
                    description=_text(payload, "Narration", "Reference", "description") or "",
                )
            )
    return tuple(result)


def _entries(facts: Sequence[SnapshotFact]) -> tuple[AccountingEntry, ...]:
    result: list[AccountingEntry] = []
    category_by_type = {
        "asset": "asset", "bank": "asset", "currentasset": "asset", "liability": "liability",
        "currentliability": "liability", "equity": "equity", "revenue": "income", "income": "income",
        "expense": "expense", "directcosts": "expense",
    }
    for fact in facts:
        if fact.provider != "xero":
            continue
        lines = fact.payload.get("JournalLines") or fact.payload.get("journal_lines")
        if not isinstance(lines, list):
            continue
        for index, line in enumerate(lines, start=1):
            if not isinstance(line, Mapping):
                raise CloseExecutionError("Xero journal contains an invalid line")
            amount = _amount(line, "LineAmount", "line_amount", "amount")
            code = _text(line, "AccountCode", "account_code")
            account_name = _text(line, "AccountName", "account_name") or code
            account_type = (_text(line, "AccountType", "account_type") or "").replace(" ", "").lower()
            category = category_by_type.get(account_type)
            if not code or not account_name or category is None:
                raise CloseExecutionError("Xero journal line has no supported account metadata")
            result.append(
                AccountingEntry(
                    entry_id=f"{fact.version_id}:{index}",
                    account_code=code,
                    account_name=account_name,
                    category=category,
                    debit=amount if amount > 0 else Decimal("0"),
                    credit=-amount if amount < 0 else Decimal("0"),
                    accounting_date=_date(fact.payload, fact.accounting_date),
                    evidence_ids=(_evidence_id(fact),),
                )
            )
    return tuple(result)


def _report_payload(
    entries: Sequence[AccountingEntry],
    banks: Sequence[BankTransaction],
    ledgers: Sequence[LedgerTransaction],
    mapping: Mapping[str, Mapping[str, object]],
) -> tuple[Mapping[str, object], str]:
    bank_total = sum((item.amount for item in banks), Decimal("0"))
    cash_codes = {str(item["xero_account_code"]) for item in mapping.values()}
    ledger_cash_total = sum((item.amount for item in ledgers if item.account_code in cash_codes), Decimal("0"))
    if not entries:
        return ({"status": "unavailable", "reason": "frozen snapshot has no supported Xero journal-line facts", "cash": {"bank_total": str(bank_total), "ledger_cash_total": str(ledger_cash_total)}}, "unavailable")
    try:
        reports: CloseReports = compute_reports(
            entries,
            entries,
            bank_total=bank_total,
            ledger_cash_total=ledger_cash_total,
            cash_evidence_ids=tuple(item.source_evidence_ids[0] for item in banks),
        )
    except Exception as exc:
        return ({"status": "exception", "reason": str(exc), "cash": {"bank_total": str(bank_total), "ledger_cash_total": str(ledger_cash_total)}}, "exception")
    def trial_balance(balance):
        return [{"account_code": item.account_code, "account_name": item.account_name, "debit": str(item.debit), "credit": str(item.credit), "evidence_ids": list(item.evidence_ids)} for item in balance.lines]
    return ({
        "status": "passed",
        "unadjusted_trial_balance": trial_balance(reports.unadjusted_trial_balance),
        "adjusted_trial_balance": trial_balance(reports.adjusted_trial_balance),
        "profit_and_loss": dict((name, str(value)) for name, value in reports.profit_and_loss),
        "balance_sheet": dict((name, str(value)) for name, value in reports.balance_sheet),
        "cash_reconciliation": {"bank_total": str(reports.cash_reconciliation.bank_total), "ledger_cash_total": str(reports.cash_reconciliation.ledger_cash_total), "difference": str(reports.cash_reconciliation.difference), "evidence_ids": list(reports.cash_reconciliation.evidence_ids)},
    }, "passed")


def _proposal_for_exception(
    exception: ReconciliationException,
    bank_by_id: Mapping[str, BankTransaction],
    bank_account_by_id: Mapping[str, str],
    mapping: Mapping[str, Mapping[str, object]],
    configuration: Mapping[str, object],
) -> JournalProposal | None:
    """Create a balanced proposal only when the controller supplied an offset.

    The offset is an explicit accountant decision, never an inferred suspense
    account. Without it, the exception stays visible but no journal is created.
    """
    if exception.control_code not in {"unmatched_bank", "unmatched_ledger"}:
        return None
    offset = _text(configuration, "journal_adjustment_account_code")
    permitted = {str(item) for item in configuration.get("permitted_journal_account_codes", [])}
    if not offset or offset not in permitted:
        return None
    bank_id = next((item for item in exception.source_transaction_ids if item.startswith("plaid:")), None)
    bank = bank_by_id.get(bank_id or "")
    if bank is None:
        return None
    account_id = bank_account_by_id.get(bank.transaction_id)
    bank_mapping = mapping.get(account_id or "")
    bank_code = _text(bank_mapping or {}, "xero_account_code")
    if not bank_code or bank_code not in permitted:
        return None
    amount = abs(bank.amount)
    if amount == 0:
        return None
    # Plaid's normalized amount convention is positive for an outflow. The
    # controller chooses the offset account; this routine only mirrors that
    # amount into a balanced DRAFT-only proposal.
    if bank.amount > 0:
        lines = (
            JournalLine(offset, amount, Decimal("0"), exception.evidence_ids),
            JournalLine(bank_code, Decimal("0"), amount, exception.evidence_ids),
        )
    else:
        lines = (
            JournalLine(bank_code, amount, Decimal("0"), exception.evidence_ids),
            JournalLine(offset, Decimal("0"), amount, exception.evidence_ids),
        )
    proposal_id = f"proposal-{sha256(exception.exception_id.encode()).hexdigest()[:24]}"
    return JournalProposal(
        proposal_id,
        bank.accounting_date.isoformat(),
        f"Proposed adjustment for {exception.control_code} {bank.transaction_id}",
        lines,
    )


def derive_close_execution(
    facts: Sequence[SnapshotFact],
    configuration: Mapping[str, object],
) -> DerivedCloseExecution:
    mapping = _mapping_by_plaid_account(configuration)
    rules = configuration.get("matching_rules")
    if not isinstance(rules, Mapping):
        raise CloseExecutionError("persisted close mapping has invalid matching rules")
    pending_policy = _text(rules, "pending_policy") or "exception"
    config = ReconciliationConfig(
        date_window_days=int(rules.get("date_window_days", 0)),
        allow_pending=False,
        max_aggregate_size=max(2, int(rules.get("max_aggregate_size", 2))),
        fee_tolerance=_decimal(rules.get("fee_tolerance", "0"), "fee tolerance"),
    )
    all_banks = _bank_transactions(facts, mapping)
    # "Exclude" means exclude pending transactions from close controls; it
    # never means silently treating them as posted/matched.
    banks = tuple(item for item in all_banks if not (pending_policy == "exclude" and item.status == "pending"))
    ledgers = _ledger_transactions(facts)
    raw_reconciliation = reconcile(banks, ledgers, config)
    reconciliation = ReconciliationResult(
        tuple(
            replace(
                item,
                match_id=f"match-{sha256('|'.join((*item.bank_transaction_ids, *item.ledger_transaction_ids, item.kind)).encode()).hexdigest()[:24]}",
            )
            for item in raw_reconciliation.matches
        ),
        tuple(
            replace(
                item,
                exception_id=f"exception-{sha256('|'.join((item.control_code, *item.source_transaction_ids)).encode()).hexdigest()[:24]}",
            )
            for item in raw_reconciliation.exceptions
        ),
        raw_reconciliation.statuses,
    )
    entries = _entries(facts)
    report, report_status = _report_payload(entries, banks, ledgers, mapping)
    input_hash = sha256(_canonical([{"id": item.version_id, "provider": item.provider, "payload": item.payload} for item in facts]).encode()).hexdigest()
    report_hash = sha256(_canonical(report).encode()).hexdigest()
    bank_by_id = {item.transaction_id: item for item in banks}
    exception_facts: dict[str, tuple[Mapping[str, str], ...]] = {}
    for exception in reconciliation.exceptions:
        selected = [bank_by_id[item] for item in exception.source_transaction_ids if item in bank_by_id]
        values: list[Mapping[str, str]] = []
        for item in selected:
            values.extend((
                {"evidence_id": item.source_evidence_ids[0], "field": "bank.amount", "value": str(item.amount)},
                {"evidence_id": f"{item.source_evidence_ids[0]}:date", "field": "bank.date", "value": item.accounting_date.isoformat()},
                {"evidence_id": f"{item.source_evidence_ids[0]}:description", "field": "bank.description", "value": item.description},
            ))
        # ExplanationContext requires unique evidence IDs. The source fact is
        # always preserved, while each derived field receives a stable suffix.
        exception_facts[exception.exception_id] = tuple(values) or tuple(
            {"evidence_id": evidence_id, "field": "source", "value": evidence_id}
            for evidence_id in exception.evidence_ids
        )
    bank_account_by_id = {
        f"plaid:{(_text(item.payload, 'transaction_id', 'id') or item.provider_record_id)}": str(item.payload["account_id"])
        for item in facts
        if item.provider == "plaid" and item.payload.get("removed") is not True and isinstance(item.payload.get("account_id"), str)
    }
    # Journal proposals deliberately require a configured, accountant-approved
    # adjustment offset. It is optional in legacy mappings, so historical runs
    # stay safe and simply contain no auto-proposal.
    proposals = tuple(
        proposal
        for exception in reconciliation.exceptions
        if (proposal := _proposal_for_exception(exception, bank_by_id, bank_account_by_id, mapping, configuration)) is not None
    )
    return DerivedCloseExecution(reconciliation, input_hash, report, report_hash, report_status, proposals, exception_facts)
