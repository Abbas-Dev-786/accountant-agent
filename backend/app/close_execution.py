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

from .domain import JournalLine, JournalProposal, PolicyError
from .normalization import provider_accounting_date, provider_currency
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
        return date.fromisoformat(provider_accounting_date(value))
    except (ValueError, PolicyError) as exc:
        raise CloseExecutionError("record has an invalid accounting date") from exc


def _currency(payload: Mapping[str, object], fallback: str | None) -> str:
    value = provider_currency(payload) or fallback
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


def _bank_transactions(
    facts: Sequence[SnapshotFact],
    mapping: Mapping[str, Mapping[str, object]],
    *,
    period_start: date | None = None,
    period_end: date | None = None,
) -> tuple[BankTransaction, ...]:
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
        plaid_status = (_text(fact.payload, "status") or "posted").lower()
        # Plaid's Transactions API uses a boolean rather than a status string
        # for authorizations that have not posted.  Preserve text status for
        # historic normalized records, but the provider boolean wins.
        if fact.payload.get("pending") is True:
            plaid_status = "pending"
        accounting_date = _date(fact.payload, fact.accounting_date)
        if period_start is not None and period_end is not None and not period_start <= accounting_date <= period_end:
            continue
        result.append(
            BankTransaction(
                transaction_id=f"plaid:{transaction_id}",
                amount=amount,
                currency=_currency(fact.payload, fact.currency),
                accounting_date=accounting_date,
                source_evidence_ids=(_evidence_id(fact),),
                status=plaid_status,
                description=_text(fact.payload, "name", "merchant_name", "description") or "",
                fee_amount=fee,
            )
        )
    return tuple(result)


def _ledger_transactions(
    facts: Sequence[SnapshotFact],
    *,
    period_start: date | None = None,
    period_end: date | None = None,
) -> tuple[LedgerTransaction, ...]:
    """Project only explicit Xero cash/bank transaction shapes.

    Invoices are intentionally not treated as settled cash. A source snapshot
    needs a Xero BankTransaction, payment, or journal line with an explicit
    amount and account before it can match a Plaid transaction.
    """
    result: list[LedgerTransaction] = []
    accounts_by_code, accounts_by_id = _xero_account_metadata(facts)
    for fact in facts:
        if fact.provider != "xero":
            continue
        payload = fact.payload
        xero_status = (_text(payload, "Status", "status") or "").upper()
        if xero_status in {"DELETED", "VOIDED"}:
            # Provider query filtering is not enough for historical frozen
            # snapshots; never reconcile a voided/deleted Xero cash record.
            continue
        record_type = (_text(payload, "Type", "type", "record_type") or "").lower()
        account_code = _xero_account_code(payload, accounts_by_id)
        explicit_amount = any(name in payload for name in ("amount", "Amount", "BankAmount", "Total", "TotalAmount"))
        is_cash_record = any(name in payload for name in ("BankTransactionID", "PaymentID", "JournalID", "ManualJournalID"))
        if explicit_amount and (account_code or is_cash_record or "bank" in record_type or "payment" in record_type):
            amount = _xero_cash_amount(payload)
            if amount is None:
                # An unknown Xero transaction direction cannot be safely
                # matched. Leave it out so reconciliation produces an
                # explicit exception instead of inventing a sign.
                continue
            transaction_id = _text(payload, "BankTransactionID", "PaymentID", "JournalID", "id", "transaction_id") or fact.provider_record_id
            account = accounts_by_code.get(account_code or "", {})
            accounting_date = _date(payload, fact.accounting_date)
            if period_start is not None and period_end is not None and not period_start <= accounting_date <= period_end:
                continue
            result.append(
                LedgerTransaction(
                    transaction_id=f"xero:{transaction_id}",
                    amount=amount,
                    currency=_currency(payload, provider_currency(account) or fact.currency),
                    accounting_date=accounting_date,
                    source_evidence_ids=(_evidence_id(fact),),
                    account_code=account_code or "unmapped",
                    description=_text(payload, "Narration", "Reference", "description") or "",
                )
            )
    return tuple(result)


def _xero_cash_amount(payload: Mapping[str, object]) -> Decimal | None:
    """Map Xero's unsigned cash amounts to Plaid's signed cash convention.

    Plaid represents an outflow as positive; Xero provides an absolute amount
    plus a transaction/payment direction. Payments use BankAmount because it
    is denominated in the bank account currency that must reconcile to Plaid.
    """
    amount = abs(_amount(payload, "bank_amount", "BankAmount", "amount", "Amount", "Total", "TotalAmount"))
    transaction_type = (_text(payload, "Type", "type") or "").upper()
    if transaction_type.startswith("SPEND"):
        return amount
    if transaction_type.startswith("RECEIVE"):
        return -amount
    payment_type = (_text(payload, "PaymentType", "payment_type") or "").upper()
    if payment_type in {"ACCPAYPAYMENT", "ARCREDITPAYMENT", "AROVERPAYMENTPAYMENT", "ARPREPAYMENTPAYMENT"}:
        return amount
    if payment_type in {"ACCRECPAYMENT", "APCREDITPAYMENT", "APOVERPAYMENTPAYMENT", "APPREPAYMENTPAYMENT"}:
        return -amount
    return None


def _xero_account_metadata(
    facts: Sequence[SnapshotFact],
) -> tuple[Mapping[str, Mapping[str, object]], Mapping[str, Mapping[str, object]]]:
    """Index source-snapshotted Xero Accounts by stable id and account code."""
    by_code: dict[str, Mapping[str, object]] = {}
    by_id: dict[str, Mapping[str, object]] = {}
    for fact in facts:
        if fact.provider != "xero" or not isinstance(fact.payload, Mapping):
            continue
        payload = fact.payload
        record_type = (_text(payload, "record_type") or "").lower()
        account_id = _text(payload, "AccountID", "account_id")
        code = _text(payload, "Code", "AccountCode", "code", "account_code")
        if record_type != "account" and not account_id:
            continue
        if account_id:
            by_id[account_id] = payload
        if code:
            by_code[code] = payload
    return by_code, by_id


def _xero_account_code(
    payload: Mapping[str, object],
    accounts_by_id: Mapping[str, Mapping[str, object]] | None = None,
) -> str | None:
    direct = _text(payload, "AccountCode", "account_code")
    if direct:
        return direct
    for key in ("BankAccount", "bank_account", "Account", "account"):
        nested = payload.get(key)
        if isinstance(nested, Mapping):
            code = _text(nested, "Code", "AccountCode", "code", "account_code")
            if code:
                return code
            account_id = _text(nested, "AccountID", "account_id", "id")
            account = (accounts_by_id or {}).get(account_id or "")
            if account:
                code = _text(account, "Code", "AccountCode", "code", "account_code")
                if code:
                    return code
    return None


def _entries(facts: Sequence[SnapshotFact]) -> tuple[tuple[AccountingEntry, ...], tuple[str, ...]]:
    result: list[AccountingEntry] = []
    unsupported_facts: list[str] = []
    accounts_by_code, _ = _xero_account_metadata(facts)
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
            account = accounts_by_code.get(code or "", {})
            account_name = _text(line, "AccountName", "account_name") or _text(account, "Name", "name") or code
            account_type = (
                _text(line, "AccountType", "account_type") or _text(account, "Type", "AccountType", "type", "account_type") or ""
            ).replace(" ", "").lower()
            category = category_by_type.get(account_type)
            if not code or not account_name:
                raise CloseExecutionError("Xero journal line has no supported account metadata")
            if category is None:
                # ManualJournal responses omit AccountType. Do not fabricate a
                # financial-statement classification or abort cash controls;
                # the report is marked unavailable below until account metadata
                # is enriched from an authoritative source.
                unsupported_facts.append(fact.version_id)
                continue
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
    return tuple(result), tuple(dict.fromkeys(unsupported_facts))


def _provider_reports(facts: Sequence[SnapshotFact]) -> Mapping[str, Mapping[str, object]]:
    reports: dict[str, Mapping[str, object]] = {}
    for fact in facts:
        if fact.provider != "xero" or _text(fact.payload, "record_type") != "report":
            continue
        kind = _text(fact.payload, "report_kind")
        payload = fact.payload.get("report_payload")
        if kind not in {"trial_balance", "profit_and_loss", "balance_sheet"} or not isinstance(payload, Mapping):
            raise CloseExecutionError("frozen Xero report record is malformed")
        reports[kind] = payload
    return reports


def _report_payload(
    entries: Sequence[AccountingEntry],
    banks: Sequence[BankTransaction],
    ledgers: Sequence[LedgerTransaction],
    mapping: Mapping[str, Mapping[str, object]],
    provider_reports: Mapping[str, Mapping[str, object]],
    material_exception_count: int,
    unsupported_journal_facts: Sequence[str] = (),
) -> tuple[Mapping[str, object], str]:
    bank_total = sum((item.amount for item in banks), Decimal("0"))
    cash_codes = {str(item["xero_account_code"]) for item in mapping.values()}
    ledger_cash_total = sum((item.amount for item in ledgers if item.account_code in cash_codes), Decimal("0"))
    required_reports = {"trial_balance", "profit_and_loss", "balance_sheet"}
    missing_reports = sorted(required_reports - set(provider_reports))
    cash = {
        "bank_total": str(bank_total),
        "ledger_cash_total": str(ledger_cash_total),
        "difference": str(bank_total - ledger_cash_total),
        "evidence_ids": [item.source_evidence_ids[0] for item in banks],
    }
    if missing_reports:
        return (
            {
                "status": "unavailable",
                "reason": f"frozen snapshot is missing Xero reports: {', '.join(missing_reports)}",
                "cash_reconciliation": cash,
            },
            "unavailable",
        )
    status = "passed" if bank_total == ledger_cash_total and material_exception_count == 0 else "exception"
    reason = None
    if material_exception_count:
        reason = f"{material_exception_count} material reconciliation exception(s) remain open"
    elif bank_total != ledger_cash_total:
        reason = "cash reconciliation does not tie"
    payload: dict[str, object] = {
        "status": status,
        "provider_reports": dict(provider_reports),
        "cash_reconciliation": cash,
        "material_exception_count": material_exception_count,
    }
    if reason:
        payload["reason"] = reason
    # Manual-journal line checks remain useful, but they are explicitly
    # supplemental. The three provider reports above are the complete ledger
    # source, rather than treating this application's manual journals as a GL.
    if unsupported_journal_facts:
        payload["manual_journal_metadata_warnings"] = list(unsupported_journal_facts)
    if entries:
        try:
            local_controls: CloseReports = compute_reports(
                entries,
                entries,
                bank_total=bank_total,
                ledger_cash_total=ledger_cash_total,
                cash_evidence_ids=tuple(item.source_evidence_ids[0] for item in banks),
            )
            payload["manual_journal_control_totals"] = {
                "debits": str(local_controls.unadjusted_trial_balance.total_debit),
                "credits": str(local_controls.unadjusted_trial_balance.total_credit),
            }
        except Exception as exc:
            payload["manual_journal_control_warning"] = str(exc)
    return payload, status


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
    *,
    period_start: date | None = None,
    period_end: date | None = None,
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
    if (period_start is None) != (period_end is None) or (period_start and period_end and period_end < period_start):
        raise CloseExecutionError("close period bounds are invalid")
    all_banks = _bank_transactions(facts, mapping, period_start=period_start, period_end=period_end)
    # Pending records are always retained. An exclusion is persisted as an
    # explicit ignored policy exception rather than removed from review.
    banks = all_banks
    ledgers = _ledger_transactions(facts, period_start=period_start, period_end=period_end)
    raw_reconciliation = reconcile(banks, ledgers, config)
    materiality_threshold = _decimal(rules.get("materiality_threshold", "0"), "materiality threshold")
    normalized_exceptions = tuple(
        replace(
            item,
            status="ignored" if pending_policy == "exclude" and item.control_code == "pending_transaction" else item.status,
        )
        for item in raw_reconciliation.exceptions
    )
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
            for item in normalized_exceptions
        ),
        raw_reconciliation.statuses,
    )
    entries, unsupported_journal_facts = _entries(facts)
    material_exceptions = tuple(
        item
        for item in reconciliation.exceptions
        if item.status == "open" and abs(item.amount) > materiality_threshold
    )
    report, report_status = _report_payload(
        entries,
        banks,
        ledgers,
        mapping,
        _provider_reports(facts),
        len(material_exceptions),
        unsupported_journal_facts,
    )
    report = {**report, "materiality_threshold": str(materiality_threshold)}
    input_hash = sha256(
        _canonical(
            {
                "facts": [{"id": item.version_id, "provider": item.provider, "payload": item.payload} for item in facts],
                "configuration": configuration,
                "period_start": period_start.isoformat() if period_start else None,
                "period_end": period_end.isoformat() if period_end else None,
            }
        ).encode()
    ).hexdigest()
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
        for exception in material_exceptions
        if (proposal := _proposal_for_exception(exception, bank_by_id, bank_account_by_id, mapping, configuration)) is not None
    )
    return DerivedCloseExecution(reconciliation, input_hash, report, report_hash, report_status, proposals, exception_facts)
