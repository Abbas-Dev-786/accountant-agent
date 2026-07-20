"""Deterministic provider-record normalization.

Adapters use this module at the ingestion boundary.  The provider payload is
canonicalized before hashing, so retries and key-order changes cannot create a
different source version for the same facts.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
from typing import Mapping

from .domain import PolicyError, SourceRecordVersion


def _json_default(value: object) -> str:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    return str(value)


def canonical_payload(payload: Mapping[str, object]) -> str:
    try:
        return json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            default=_json_default,
        )
    except (TypeError, ValueError) as exc:
        raise PolicyError("provider payload cannot be normalized") from exc


def observed_at(payload: Mapping[str, object], fallback: datetime) -> datetime:
    value = payload.get("observed_at") or payload.get("updated_at") or payload.get("date")
    if not value:
        return fallback
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise PolicyError("provider record has an invalid observation timestamp") from exc
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    raise PolicyError("provider record has an invalid observation timestamp")


_XERO_WIRE_DATE = re.compile(r"^/Date\((-?\d+)([+-]\d{4})?\)/$")


def provider_accounting_date(value: object) -> str:
    """Normalize ISO and Xero ``/Date(milliseconds±offset)/`` values to ISO dates."""
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if not isinstance(value, str) or not value.strip():
        raise PolicyError("provider record has an invalid accounting date")
    raw = value.strip()
    xero_match = _XERO_WIRE_DATE.fullmatch(raw)
    if xero_match:
        milliseconds = int(xero_match.group(1))
        offset = xero_match.group(2) or "+0000"
        sign = 1 if offset[0] == "+" else -1
        hours, minutes = int(offset[1:3]), int(offset[3:5])
        if hours > 23 or minutes > 59:
            raise PolicyError("provider record has an invalid accounting date")
        zone = timezone(sign * timedelta(hours=hours, minutes=minutes))
        return datetime.fromtimestamp(milliseconds / 1000, timezone.utc).astimezone(zone).date().isoformat()
    try:
        return date.fromisoformat(raw[:10]).isoformat()
    except ValueError as exc:
        raise PolicyError("provider record has an invalid accounting date") from exc


def provider_currency(payload: Mapping[str, object]) -> str | None:
    """Find a provider currency without mistaking a numeric exchange rate for one."""
    for key in ("currency", "iso_currency_code", "CurrencyCode"):
        value = payload.get(key)
        if isinstance(value, str) and len(value.strip()) == 3:
            return value.strip().upper()
    # Payments expose the bank account and/or related document rather than a
    # top-level CurrencyCode.  CurrencyRate is deliberately never a currency.
    for key in ("BankAccount", "Account", "Invoice", "CreditNote", "Prepayment", "Overpayment"):
        nested = payload.get(key)
        if isinstance(nested, Mapping):
            value = nested.get("CurrencyCode") or nested.get("currency") or nested.get("iso_currency_code")
            if isinstance(value, str) and len(value.strip()) == 3:
                return value.strip().upper()
    return None


def normalize_provider_record(
    provider: str,
    provider_record_id: str,
    payload: Mapping[str, object],
    *,
    fallback_observed_at: datetime,
) -> SourceRecordVersion:
    if not provider or not provider_record_id:
        raise PolicyError("provider records require a provider and provider record id")
    canonical = canonical_payload(payload)
    content_hash = sha256(canonical.encode("utf-8")).hexdigest()
    version_id = f"{provider}:{provider_record_id}:{content_hash[:24]}"
    currency_value = provider_currency(payload)
    date_value = (
        payload.get("accounting_date")
        or payload.get("date")
        or payload.get("Date")
        or payload.get("transaction_date")
        or payload.get("TransactionDate")
    )
    return SourceRecordVersion(
        version_id=version_id,
        provider=provider,
        provider_record_id=provider_record_id,
        content_hash=content_hash,
        observed_at=observed_at(payload, fallback_observed_at),
        payload_json=canonical,
        currency=currency_value,
        accounting_date=provider_accounting_date(date_value) if date_value is not None else None,
    )
