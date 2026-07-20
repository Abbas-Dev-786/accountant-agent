"""Versioned, accountant-approved close configuration.

The mapping is deliberately a first-class persisted object rather than a set of
environment variables.  It records the exact bank-to-ledger choices and control
rules that a close run is allowed to use, without ever storing provider tokens
or source records.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Mapping, Sequence

from .domain import PolicyError


def _clean(value: str, label: str, *, max_length: int = 300) -> str:
    cleaned = value.strip()
    if not cleaned or len(cleaned) > max_length:
        raise PolicyError(f"{label} is required and must be at most {max_length} characters")
    return cleaned


def _money(value: Decimal | str | int | float, label: str) -> Decimal:
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise PolicyError(f"{label} must be a decimal amount") from exc
    if amount < 0:
        raise PolicyError(f"{label} must not be negative")
    return amount


@dataclass(frozen=True)
class BankLedgerMapping:
    plaid_account_id: str
    xero_account_code: str
    xero_account_name: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "plaid_account_id", _clean(self.plaid_account_id, "Plaid account ID"))
        object.__setattr__(self, "xero_account_code", _clean(self.xero_account_code, "Xero account code", max_length=80))
        object.__setattr__(self, "xero_account_name", _clean(self.xero_account_name, "Xero account name"))

    def as_dict(self) -> dict[str, str]:
        return {
            "plaid_account_id": self.plaid_account_id,
            "xero_account_code": self.xero_account_code,
            "xero_account_name": self.xero_account_name,
        }


@dataclass(frozen=True)
class MatchingRules:
    date_window_days: int
    fee_tolerance: Decimal
    materiality_threshold: Decimal
    pending_policy: str
    max_aggregate_size: int

    def __post_init__(self) -> None:
        if not 0 <= self.date_window_days <= 60:
            raise PolicyError("date window must be between 0 and 60 days")
        object.__setattr__(self, "fee_tolerance", _money(self.fee_tolerance, "fee tolerance"))
        object.__setattr__(self, "materiality_threshold", _money(self.materiality_threshold, "materiality threshold"))
        if self.pending_policy not in {"exclude", "exception"}:
            raise PolicyError("pending policy must be exclude or exception")
        if not 1 <= self.max_aggregate_size <= 100:
            raise PolicyError("maximum aggregate size must be between 1 and 100")

    def as_dict(self) -> dict[str, object]:
        return {
            "date_window_days": self.date_window_days,
            "fee_tolerance": str(self.fee_tolerance),
            "materiality_threshold": str(self.materiality_threshold),
            "pending_policy": self.pending_policy,
            "max_aggregate_size": self.max_aggregate_size,
        }


@dataclass(frozen=True)
class EvidenceConfiguration:
    drive_folder_ids: tuple[str, ...]
    gmail_mailbox: str
    gmail_labels: tuple[str, ...]
    allowed_recipients: tuple[str, ...]
    retention_policy_version: str

    def __post_init__(self) -> None:
        folders = tuple(dict.fromkeys(_clean(value, "Drive folder ID") for value in self.drive_folder_ids))
        labels = tuple(dict.fromkeys(_clean(value, "Gmail label", max_length=120) for value in self.gmail_labels))
        recipients = tuple(dict.fromkeys(_clean(value, "allowed recipient") for value in self.allowed_recipients))
        if not folders or not labels or not recipients:
            raise PolicyError("evidence configuration needs folders, labels, and allowed recipients")
        if "@" not in self.gmail_mailbox:
            raise PolicyError("Gmail mailbox must be an email address")
        if any("@" not in value for value in recipients):
            raise PolicyError("allowed recipients must be email addresses")
        object.__setattr__(self, "drive_folder_ids", folders)
        object.__setattr__(self, "gmail_labels", labels)
        object.__setattr__(self, "allowed_recipients", recipients)
        object.__setattr__(self, "gmail_mailbox", _clean(self.gmail_mailbox, "Gmail mailbox"))
        object.__setattr__(self, "retention_policy_version", _clean(self.retention_policy_version, "retention policy version"))

    def as_dict(self) -> dict[str, object]:
        return {
            "drive_folder_ids": list(self.drive_folder_ids),
            "gmail_mailbox": self.gmail_mailbox,
            "gmail_labels": list(self.gmail_labels),
            "allowed_recipients": list(self.allowed_recipients),
            "retention_policy_version": self.retention_policy_version,
        }


@dataclass(frozen=True)
class CloseMappingDraft:
    xero_tenant_id: str
    bank_mappings: tuple[BankLedgerMapping, ...]
    matching_rules: MatchingRules
    permitted_journal_account_codes: tuple[str, ...]
    evidence: EvidenceConfiguration
    journal_adjustment_account_code: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "xero_tenant_id", _clean(self.xero_tenant_id, "Xero tenant ID"))
        mappings = tuple(self.bank_mappings)
        if not mappings:
            raise PolicyError("at least one Plaid-to-Xero bank mapping is required")
        account_ids = [item.plaid_account_id for item in mappings]
        if len(account_ids) != len(set(account_ids)):
            raise PolicyError("each Plaid account may appear only once in a close mapping")
        codes = tuple(dict.fromkeys(_clean(value, "permitted journal account code", max_length=80) for value in self.permitted_journal_account_codes))
        if not codes:
            raise PolicyError("at least one permitted journal account code is required")
        offset = self.journal_adjustment_account_code
        if offset is not None:
            offset = _clean(offset, "journal adjustment account code", max_length=80)
            if offset not in codes:
                raise PolicyError("journal adjustment account code must be one of the permitted journal account codes")
        object.__setattr__(self, "bank_mappings", mappings)
        object.__setattr__(self, "permitted_journal_account_codes", codes)
        object.__setattr__(self, "journal_adjustment_account_code", offset)

    def as_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "xero_tenant_id": self.xero_tenant_id,
            "bank_mappings": [item.as_dict() for item in self.bank_mappings],
            "matching_rules": self.matching_rules.as_dict(),
            "permitted_journal_account_codes": list(self.permitted_journal_account_codes),
            "evidence": self.evidence.as_dict(),
        }
        if self.journal_adjustment_account_code:
            result["journal_adjustment_account_code"] = self.journal_adjustment_account_code
        return result


@dataclass(frozen=True)
class PersistedCloseMapping:
    mapping_id: str
    organization_id: str
    version: int
    status: str
    configuration: Mapping[str, object]
    approved_by_subject: str
    created_at: object | None = None


def draft_from_mapping(value: Mapping[str, object]) -> CloseMappingDraft:
    """Construct the validated domain configuration from a JSON-safe mapping."""

    try:
        raw_mappings = value["bank_mappings"]
        rules = value["matching_rules"]
        evidence = value["evidence"]
        if not isinstance(raw_mappings, Sequence) or isinstance(raw_mappings, (str, bytes)):
            raise TypeError
        if not isinstance(rules, Mapping) or not isinstance(evidence, Mapping):
            raise TypeError
        bank_mappings = tuple(
            BankLedgerMapping(
                str(item["plaid_account_id"]),
                str(item["xero_account_code"]),
                str(item["xero_account_name"]),
            )
            for item in raw_mappings
            if isinstance(item, Mapping)
        )
        if len(bank_mappings) != len(raw_mappings):
            raise TypeError
        journal_codes = value["permitted_journal_account_codes"]
        folders = evidence["drive_folder_ids"]
        labels = evidence["gmail_labels"]
        recipients = evidence["allowed_recipients"]
        if any(not isinstance(items, Sequence) or isinstance(items, (str, bytes)) for items in (journal_codes, folders, labels, recipients)):
            raise TypeError
        return CloseMappingDraft(
            str(value["xero_tenant_id"]),
            bank_mappings,
            MatchingRules(
                int(rules["date_window_days"]),
                _money(rules["fee_tolerance"], "fee tolerance"),
                _money(rules["materiality_threshold"], "materiality threshold"),
                str(rules["pending_policy"]),
                int(rules["max_aggregate_size"]),
            ),
            tuple(str(item) for item in journal_codes),
            EvidenceConfiguration(
                tuple(str(item) for item in folders),
                str(evidence["gmail_mailbox"]),
                tuple(str(item) for item in labels),
                tuple(str(item) for item in recipients),
                str(evidence["retention_policy_version"]),
            ),
            str(value["journal_adjustment_account_code"])
            if value.get("journal_adjustment_account_code") is not None
            else None,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise PolicyError("close mapping configuration is incomplete") from exc
