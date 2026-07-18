"""Provider source contracts and demo/sandbox ingestion adapters.

The adapters deliberately depend on injected clients rather than SDKs or the
network.  That keeps the worker boundary deterministic in demo deployments and
makes pagination, cursor, and recovery behavior directly testable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Mapping, Protocol
from uuid import uuid4

from .domain import PolicyError, SourceBatch, SourceRecordVersion
from .normalization import normalize_provider_record


class ProviderReadError(PolicyError):
    """Raised when a source cannot produce a complete immutable batch."""


def _record_id(record: Mapping[str, object]) -> str:
    for key in ("id", "transaction_id", "journal_id", "invoice_id", "account_id"):
        value = record.get(key)
        if value is not None and str(value):
            return str(value)
    raise ProviderReadError("provider record is missing a stable id")


@dataclass(frozen=True)
class XeroPage:
    page: int
    records: tuple[Mapping[str, object], ...]
    next_page: int | None
    tenant_id: str
    provider_environment: str = "demo"
    request_id: str = ""


class XeroDemoClient(Protocol):
    def get_page(self, page: int) -> XeroPage:
        ...


class XeroDemoAdapter:
    def __init__(self, client: XeroDemoClient, tenant_id: str, *, max_pages: int = 100) -> None:
        if not tenant_id:
            raise ProviderReadError("Xero demo ingestion requires a tenant id")
        if max_pages < 1:
            raise ProviderReadError("Xero page limit must be positive")
        self.client = client
        self.tenant_id = tenant_id
        self.max_pages = max_pages

    def read_batch(self) -> SourceBatch:
        started_at = datetime.now(timezone.utc)
        page_number = 1
        pages_seen: set[int] = set()
        provider_ids: set[str] = set()
        versions: list[SourceRecordVersion] = []
        request_ids: list[str] = []

        for _ in range(self.max_pages):
            try:
                page = self.client.get_page(page_number)
            except Exception as exc:  # provider SDK errors must block the snapshot
                raise ProviderReadError("Xero demo page read failed") from exc
            if not isinstance(page, XeroPage):
                raise ProviderReadError("Xero client returned an invalid page")
            if page.page != page_number or page.page in pages_seen:
                raise ProviderReadError("Xero pagination returned an unexpected page")
            if page.tenant_id != self.tenant_id:
                raise ProviderReadError("Xero page belongs to a different tenant")
            if page.provider_environment != "demo":
                raise ProviderReadError("Xero demo adapter received a non-demo page")
            pages_seen.add(page.page)
            if page.request_id:
                request_ids.append(page.request_id)
            for record in page.records:
                record_id = _record_id(record)
                if record_id in provider_ids:
                    raise ProviderReadError("Xero pagination returned a duplicate record")
                provider_ids.add(record_id)
                versions.append(
                    normalize_provider_record(
                        "xero",
                        record_id,
                        record,
                        fallback_observed_at=started_at,
                    )
                )
            if page.next_page is None:
                return SourceBatch(
                    batch_id=str(uuid4()),
                    provider="xero",
                    provider_environment="demo",
                    watermark=f"page-{page.page}|{','.join(request_ids)}",
                    completed_at=datetime.now(timezone.utc),
                    record_versions=tuple(versions),
                )
            if page.next_page != page_number + 1:
                raise ProviderReadError("Xero pagination skipped or repeated a page")
            page_number = page.next_page
        raise ProviderReadError("Xero pagination exceeded the configured page limit")


@dataclass(frozen=True)
class PlaidSyncPage:
    cursor: str | None
    next_cursor: str
    added: tuple[Mapping[str, object], ...] = ()
    modified: tuple[Mapping[str, object], ...] = ()
    removed: tuple[Mapping[str, object] | str, ...] = ()
    has_more: bool = False
    request_id: str = ""
    provider_environment: str = "sandbox"


class PlaidSandboxClient(Protocol):
    def sync(self, access_token: str, cursor: str | None) -> PlaidSyncPage:
        ...


@dataclass
class PlaidCursorState:
    cursor: str | None = None
    records: dict[str, Mapping[str, object]] = field(default_factory=dict)


class PlaidSandboxAdapter:
    def __init__(
        self,
        client: PlaidSandboxClient,
        access_token: str,
        *,
        state: PlaidCursorState | None = None,
        max_pages: int = 100,
    ) -> None:
        if not access_token:
            raise ProviderReadError("Plaid sandbox ingestion requires an access token")
        if max_pages < 1:
            raise ProviderReadError("Plaid page limit must be positive")
        self.client = client
        self.access_token = access_token
        self.state = state or PlaidCursorState()
        self.max_pages = max_pages

    def read_batch(self) -> SourceBatch:
        started_at = datetime.now(timezone.utc)
        original_cursor = self.state.cursor
        staged_records = dict(self.state.records)
        cursor = original_cursor
        seen_request_ids: set[str] = set()
        removed_ids: list[str] = []
        removed_id_set: set[str] = set()

        for _ in range(self.max_pages):
            try:
                page = self.client.sync(self.access_token, cursor)
            except Exception as exc:
                raise ProviderReadError("Plaid sandbox sync failed") from exc
            if not isinstance(page, PlaidSyncPage):
                raise ProviderReadError("Plaid client returned an invalid sync page")
            if page.cursor != cursor:
                raise ProviderReadError("Plaid sync response cursor does not match the request")
            if page.provider_environment != "sandbox":
                raise ProviderReadError("Plaid sandbox adapter received a non-sandbox page")
            if page.request_id and page.request_id in seen_request_ids:
                raise ProviderReadError("Plaid sync repeated a request id")
            if page.request_id:
                seen_request_ids.add(page.request_id)

            page_ids: set[str] = set()
            for record in (*page.added, *page.modified):
                record_id = _record_id(record)
                if record_id in page_ids:
                    raise ProviderReadError("Plaid sync returned a duplicate transaction")
                page_ids.add(record_id)
                staged_records[record_id] = record
            for removed in page.removed:
                record_id = _record_id(removed) if isinstance(removed, Mapping) else str(removed)
                if not record_id:
                    raise ProviderReadError("Plaid sync returned an empty removed id")
                if record_id in removed_id_set:
                    raise ProviderReadError("Plaid sync returned a duplicate removed transaction")
                removed_id_set.add(record_id)
                staged_records.pop(record_id, None)
                removed_ids.append(record_id)

            if not page.has_more:
                if not page.next_cursor:
                    raise ProviderReadError("Plaid sync did not return a final cursor")
                versions = [
                    normalize_provider_record(
                        "plaid", record_id, record, fallback_observed_at=started_at
                    )
                    for record_id, record in staged_records.items()
                ]
                versions.extend(
                    normalize_provider_record(
                        "plaid",
                        record_id,
                        {"transaction_id": record_id, "removed": True},
                        fallback_observed_at=started_at,
                    )
                    for record_id in removed_ids
                )
                # Commit the staged store only after every page and every
                # normalized version has succeeded.  A failed retry therefore
                # starts from the original cursor and record set.
                self.state.records = staged_records
                self.state.cursor = page.next_cursor
                watermark = f"cursor:{page.next_cursor}"
                if seen_request_ids:
                    watermark += f"|{','.join(sorted(seen_request_ids))}"
                return SourceBatch(
                    batch_id=str(uuid4()),
                    provider="plaid",
                    provider_environment="sandbox",
                    watermark=watermark,
                    completed_at=datetime.now(timezone.utc),
                    record_versions=tuple(versions),
                )
            if not page.next_cursor or page.next_cursor == cursor:
                raise ProviderReadError("Plaid sync cursor did not advance")
            cursor = page.next_cursor
        raise ProviderReadError("Plaid sync exceeded the configured page limit")


# Descriptive aliases keep the worker contract readable at call sites while
# retaining the concise class names used by the demo implementation.
XeroDirectDemoAdapter = XeroDemoAdapter
PlaidSandboxIngestionAdapter = PlaidSandboxAdapter
