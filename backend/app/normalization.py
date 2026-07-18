"""Deterministic provider-record normalization.

Adapters use this module at the ingestion boundary.  The provider payload is
canonicalized before hashing, so retries and key-order changes cannot create a
different source version for the same facts.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
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
    currency_value = payload.get("currency") or payload.get("iso_currency_code")
    date_value = payload.get("accounting_date") or payload.get("date")
    return SourceRecordVersion(
        version_id=version_id,
        provider=provider,
        provider_record_id=provider_record_id,
        content_hash=content_hash,
        observed_at=observed_at(payload, fallback_observed_at),
        payload_json=canonical,
        currency=str(currency_value) if currency_value is not None else None,
        accounting_date=str(date_value) if date_value is not None else None,
    )
