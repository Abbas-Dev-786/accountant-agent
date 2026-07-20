"""Server-side HTTP clients for the isolated US demo providers.

All clients accept a secret resolver and an injected JSON transport. This keeps
tokens out of browser code/logs and makes provider behavior testable without
network calls.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal
from hashlib import sha256
from typing import Callable, Mapping, Protocol, Sequence
from urllib.parse import urlencode, urljoin
from urllib.request import Request, urlopen

from .actions import XeroDraftRecord, XeroDraftRequest
from .evidence import DriveEvidenceClient, DriveSearchResult, EvidenceScope, GmailDraft, GmailEvidenceClient, GmailSearchResult, GmailSendResult
from .providers import PlaidSandboxClient, PlaidSyncPage, ProviderReadError, XeroDemoClient, XeroPage
from .scenario import XeroBaselineObservation
from .xero_oauth import XeroOAuthClient


class SecretResolver(Protocol):
    def resolve(self, secret_ref: str) -> str:
        ...


class RuntimeConfigError(ProviderReadError):
    """Raised when provider runtime configuration is unsafe or incomplete."""


@dataclass(frozen=True)
class StaticSecretResolver:
    """Test/local resolver; production should use a managed secret store."""

    secrets: Mapping[str, str]

    def resolve(self, secret_ref: str) -> str:
        if not secret_ref.startswith("secret://"):
            raise RuntimeConfigError("provider credentials must be secret:// references")
        value = self.secrets.get(secret_ref, "")
        if not value:
            raise RuntimeConfigError("provider secret reference is unavailable")
        return value


@dataclass(frozen=True)
class JsonResponse:
    status_code: int
    body: Mapping[str, object]
    headers: Mapping[str, str]


class JsonTransport(Protocol):
    def request(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        payload: Mapping[str, object] | None = None,
    ) -> JsonResponse:
        ...


class UrllibJsonTransport:
    def request(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        payload: Mapping[str, object] | None = None,
    ) -> JsonResponse:
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = Request(url, data=body, headers={**headers, "Accept": "application/json", "Content-Type": "application/json"}, method=method)
        try:
            with urlopen(request, timeout=30) as response:
                return JsonResponse(response.status, json.loads(response.read().decode("utf-8")), dict(response.headers.items()))
        except Exception as exc:
            raise ProviderReadError("provider HTTP request failed") from exc


def _request_id(headers: Mapping[str, str]) -> str:
    return headers.get("x-request-id") or headers.get("X-Request-Id") or headers.get("request-id", "")


def _records(body: Mapping[str, object], key: str) -> tuple[Mapping[str, object], ...]:
    value = body.get(key, [])
    if not isinstance(value, list) or any(not isinstance(item, Mapping) for item in value):
        raise ProviderReadError(f"provider response field {key} is not a record list")
    return tuple(value)


@dataclass
class XeroDemoHttpClient(XeroDemoClient):
    tenant_id: str
    access_token_secret_ref: str
    secret_resolver: SecretResolver
    transport: JsonTransport
    base_url: str = "https://api.xero.com"
    resource_path: str = "/api.xro/2.0/Invoices"
    records_key: str = "Invoices"
    page_size: int = 100
    oauth_client: XeroOAuthClient | None = None

    def __post_init__(self) -> None:
        if not self.tenant_id or not self.base_url.startswith("https://") or not self.resource_path.startswith("/"):
            raise RuntimeConfigError("Xero demo HTTP configuration is invalid")
        if self.page_size < 1:
            raise RuntimeConfigError("Xero page size must be positive")

    def get_page(self, page: int) -> XeroPage:
        token = self.oauth_client.access_token() if self.oauth_client else self.secret_resolver.resolve(self.access_token_secret_ref)
        url = urljoin(self.base_url.rstrip("/") + "/", self.resource_path.lstrip("/"))
        url = f"{url}?{urlencode({'page': page})}"
        response = self.transport.request(
            "GET",
            url,
            {"Authorization": f"Bearer {token}", "Xero-tenant-id": self.tenant_id},
        )
        if response.status_code >= 400:
            raise ProviderReadError(f"Xero demo request failed with HTTP {response.status_code}")
        records = _records(response.body, self.records_key)
        raw_next = response.body.get("next_page")
        if raw_next is None:
            next_page = page + 1 if len(records) >= self.page_size else None
        else:
            try:
                next_page = int(raw_next)
            except (TypeError, ValueError) as exc:
                raise ProviderReadError("Xero demo next_page is invalid") from exc
            if next_page < 1:
                raise ProviderReadError("Xero demo next_page must be positive")
        return XeroPage(page, records, next_page, self.tenant_id, "demo", _request_id(response.headers))


class XeroProductionHttpClient(XeroDemoHttpClient):
    """Read-only Xero Accounting API client for the US production boundary.

    The request shape is deliberately identical to the fixture client, but the
    returned page is tagged ``production``.  ``XeroProductionAdapter`` rejects
    anything else before raw records are persisted.
    """

    def get_page(self, page: int) -> XeroPage:
        source_page = super().get_page(page)
        return replace(source_page, provider_environment="production")


@dataclass
class XeroDraftHttpClient:
    """The sole Xero write client: create and verify a manual journal in DRAFT."""

    tenant_id: str
    access_token_secret_ref: str
    secret_resolver: SecretResolver
    transport: JsonTransport
    oauth_client: XeroOAuthClient | None = None
    base_url: str = "https://api.xero.com"

    def _token(self) -> str:
        return self.oauth_client.access_token() if self.oauth_client else self.secret_resolver.resolve(self.access_token_secret_ref)

    def _request(self, method: str, path: str, payload: Mapping[str, object] | None = None) -> Mapping[str, object]:
        response = self.transport.request(
            method,
            urljoin(self.base_url.rstrip("/") + "/", path.lstrip("/")),
            {"Authorization": f"Bearer {self._token()}", "Xero-tenant-id": self.tenant_id},
            payload,
        )
        if response.status_code >= 400:
            raise ProviderReadError(f"Xero manual journal request failed with HTTP {response.status_code}")
        return response.body

    @staticmethod
    def _record(value: Mapping[str, object]) -> XeroDraftRecord:
        journal_id = value.get("ManualJournalID") or value.get("JournalID")
        status = value.get("Status")
        narration = value.get("Narration")
        journal_date = value.get("Date")
        lines = value.get("JournalLines")
        if not all(isinstance(item, str) and item for item in (journal_id, status, narration, journal_date)) or not isinstance(lines, list):
            raise ProviderReadError("Xero manual journal read-back is incomplete")
        normalized_lines: list[tuple[str, str, str, tuple[str, ...]]] = []
        for line in lines:
            if not isinstance(line, Mapping) or not isinstance(line.get("AccountCode"), str):
                raise ProviderReadError("Xero manual journal read-back has an invalid line")
            amount = _decimal_for_draft(line.get("LineAmount"))
            normalized_lines.append((str(line["AccountCode"]), str(amount if amount > 0 else 0), str(-amount if amount < 0 else 0), ()))
        return XeroDraftRecord(str(journal_id), str(status), str(narration), str(journal_date)[:10], tuple(normalized_lines), "")

    def search_manual_journals(self, marker: str):
        # Marker is generated server-side and is included in the narration.
        where = f'Contains(Narration,"{marker}")'
        body = self._request("GET", f"/api.xro/2.0/ManualJournals?{urlencode({'where': where})}")
        return tuple(self._record(item) for item in _records(body, "ManualJournals"))

    def create_draft_manual_journal(self, request: XeroDraftRequest) -> XeroDraftRecord:
        lines = []
        for account_code, debit, credit, _ in request.lines:
            lines.append({"AccountCode": account_code, "LineAmount": debit if Decimal(debit) > 0 else f"-{credit}"})
        body = self._request(
            "POST", "/api.xro/2.0/ManualJournals",
            {"ManualJournals": [{"Narration": request.narration, "Date": request.journal_date,
                                  "Status": "DRAFT", "LineAmountTypes": "NoTax", "JournalLines": lines}]},
        )
        records = _records(body, "ManualJournals")
        if len(records) != 1:
            raise ProviderReadError("Xero did not return exactly one draft journal")
        return self._record(records[0])

    def get_manual_journal(self, journal_id: str) -> XeroDraftRecord:
        records = _records(self._request("GET", f"/api.xro/2.0/ManualJournals/{journal_id}"), "ManualJournals")
        if len(records) != 1:
            raise ProviderReadError("Xero draft journal read-back is ambiguous")
        return self._record(records[0])


def _decimal_for_draft(value: object) -> Decimal:
    try:
        return Decimal(str(value))
    except Exception as exc:
        raise ProviderReadError("Xero manual journal line amount is invalid") from exc


@dataclass
class XeroBaselineHttpClient:
    """Read-only Demo Company identity/accounts used to prepare a baseline."""

    tenant_id: str
    access_token_provider: Callable[[], str]
    transport: JsonTransport
    base_url: str = "https://api.xero.com"

    def __post_init__(self) -> None:
        if not self.tenant_id or not self.base_url.startswith("https://"):
            raise RuntimeConfigError("Xero baseline HTTP configuration is invalid")

    def _get(self, path: str) -> Mapping[str, object]:
        token = self.access_token_provider()
        if not token or token.startswith("secret://"):
            raise RuntimeConfigError("Xero baseline requires a resolved access token")
        response = self.transport.request(
            "GET",
            urljoin(self.base_url.rstrip("/") + "/", path.lstrip("/")),
            {"Authorization": f"Bearer {token}", "Xero-tenant-id": self.tenant_id},
        )
        if response.status_code >= 400:
            raise ProviderReadError(f"Xero baseline request failed with HTTP {response.status_code}")
        return response.body

    def collect(self, required_account_codes: Sequence[str] = ("200", "610")) -> XeroBaselineObservation:
        organization = _records(self._get("/api.xro/2.0/Organisation"), "Organisations")
        if len(organization) != 1 or organization[0].get("IsDemoCompany") is not True:
            raise ProviderReadError("selected Xero tenant is not the Demo Company")
        accounts = _records(self._get("/api.xro/2.0/Accounts"), "Accounts")
        required = tuple(dict.fromkeys(required_account_codes))
        if not required or any(not isinstance(code, str) or not code for code in required):
            raise RuntimeConfigError("Xero baseline account codes are invalid")
        provider_ids: dict[str, str] = {}
        for code in required:
            matches = [item for item in accounts if item.get("Code") == code]
            if len(matches) != 1 or not isinstance(matches[0].get("AccountID"), str):
                raise ProviderReadError(f"Xero baseline account code {code} is missing or ambiguous")
            provider_ids[f"account-{code}"] = str(matches[0]["AccountID"])
        return XeroBaselineObservation(self.tenant_id, required, provider_ids)


@dataclass
class PlaidHttpSandboxClient(PlaidSandboxClient):
    client_id: str
    client_secret_secret_ref: str
    secret_resolver: SecretResolver
    transport: JsonTransport
    base_url: str = "https://sandbox.plaid.com"

    def __post_init__(self) -> None:
        if not self.client_id or not self.base_url.startswith("https://"):
            raise RuntimeConfigError("Plaid Sandbox HTTP configuration is invalid")

    def sync(self, access_token: str, cursor: str | None) -> PlaidSyncPage:
        if not access_token or access_token.startswith("secret://"):
            raise RuntimeConfigError("Plaid adapter must receive a resolved access token")
        secret = self.secret_resolver.resolve(self.client_secret_secret_ref)
        payload: dict[str, object] = {"client_id": self.client_id, "secret": secret, "access_token": access_token}
        if cursor:
            payload["cursor"] = cursor
        response = self.transport.request("POST", f"{self.base_url.rstrip('/')}/transactions/sync", {}, payload)
        if response.status_code >= 400:
            raise ProviderReadError(f"Plaid Sandbox request failed with HTTP {response.status_code}")
        added = _records(response.body, "added")
        modified = _records(response.body, "modified")
        removed_raw = response.body.get("removed", [])
        if not isinstance(removed_raw, list):
            raise ProviderReadError("Plaid Sandbox removed field is not a list")
        next_cursor = response.body.get("next_cursor")
        if not isinstance(next_cursor, str):
            raise ProviderReadError("Plaid Sandbox response is missing next_cursor")
        return PlaidSyncPage(
            cursor,
            next_cursor,
            added,
            modified,
            tuple(item for item in removed_raw if isinstance(item, (Mapping, str))),
            bool(response.body.get("has_more", False)),
            _request_id(response.headers),
            "sandbox",
        )


class PlaidProductionHttpClient(PlaidHttpSandboxClient):
    """Plaid Transactions Sync client restricted to the Production endpoint."""

    def __init__(
        self,
        client_id: str,
        client_secret_secret_ref: str,
        secret_resolver: SecretResolver,
        transport: JsonTransport,
        base_url: str = "https://production.plaid.com",
    ) -> None:
        super().__init__(client_id, client_secret_secret_ref, secret_resolver, transport, base_url)

    def sync(self, access_token: str, cursor: str | None) -> PlaidSyncPage:
        source_page = super().sync(access_token, cursor)
        return replace(source_page, provider_environment="production")


@dataclass
class GoogleDriveHttpClient(DriveEvidenceClient):
    access_token_secret_ref: str
    secret_resolver: SecretResolver
    transport: JsonTransport
    base_url: str = "https://www.googleapis.com/drive/v3/"

    def search_evidence(self, scope: EvidenceScope) -> Sequence[DriveSearchResult]:
        token = self.secret_resolver.resolve(self.access_token_secret_ref)
        folder_query = " or ".join(f"'{folder_id}' in parents" for folder_id in sorted(scope.drive_folder_ids))
        query = f"({folder_query}) and trashed = false"
        url = urljoin(self.base_url, "files") + "?" + urlencode(
            {"q": query, "fields": "files(id,name,mimeType,modifiedTime,parents,md5Checksum)"}
        )
        response = self.transport.request("GET", url, {"Authorization": f"Bearer {token}"})
        if response.status_code >= 400:
            raise ProviderReadError(f"Google Drive request failed with HTTP {response.status_code}")
        files = _records(response.body, "files")
        results: list[DriveSearchResult] = []
        for item in files:
            resource_id = item.get("id")
            parents = item.get("parents", [])
            modified = item.get("modifiedTime")
            if not isinstance(resource_id, str) or not isinstance(parents, list) or not parents or not isinstance(modified, str):
                raise ProviderReadError("Google Drive result is missing scoped metadata")
            parsed = datetime.fromisoformat(modified.replace("Z", "+00:00"))
            metadata_hash = item.get("md5Checksum") or sha256(json.dumps(item, sort_keys=True, default=str).encode()).hexdigest()
            results.append(DriveSearchResult(resource_id, str(parents[0]), str(item.get("name", "")), str(item.get("mimeType", "")), parsed, str(metadata_hash)))
        return results


@dataclass
class GmailHttpClient(GmailEvidenceClient):
    access_token_secret_ref: str
    secret_resolver: SecretResolver
    transport: JsonTransport
    base_url: str = "https://gmail.googleapis.com/gmail/v1/users/"

    def search_evidence(self, scope: EvidenceScope) -> Sequence[GmailSearchResult]:
        token = self.secret_resolver.resolve(self.access_token_secret_ref)
        query = f"after:{scope.start_date.isoformat()} before:{scope.end_date.isoformat()}"
        list_url = urljoin(self.base_url, f"{scope.gmail_mailbox}/messages") + "?" + urlencode({"q": query})
        response = self.transport.request("GET", list_url, {"Authorization": f"Bearer {token}"})
        if response.status_code >= 400:
            raise ProviderReadError(f"Gmail search failed with HTTP {response.status_code}")
        messages = _records(response.body, "messages")
        results: list[GmailSearchResult] = []
        for item in messages:
            message_id = item.get("id")
            if not isinstance(message_id, str):
                raise ProviderReadError("Gmail result is missing a message id")
            get_url = urljoin(self.base_url, f"{scope.gmail_mailbox}/messages/{message_id}") + "?" + urlencode({"format": "metadata"})
            detail = self.transport.request("GET", get_url, {"Authorization": f"Bearer {token}"})
            if detail.status_code >= 400:
                raise ProviderReadError(f"Gmail message fetch failed with HTTP {detail.status_code}")
            payload = detail.body
            internal_ms = payload.get("internalDate")
            if not isinstance(internal_ms, str):
                raise ProviderReadError("Gmail message is missing internalDate")
            payload_block = payload.get("payload", {})
            if not isinstance(payload_block, Mapping):
                raise ProviderReadError("Gmail message payload is invalid")
            raw_headers = payload_block.get("headers", [])
            if not isinstance(raw_headers, list):
                raise ProviderReadError("Gmail message headers are invalid")
            headers = {
                str(header.get("name", "")).lower(): str(header.get("value", ""))
                for header in raw_headers
                if isinstance(header, Mapping)
            }
            observed = datetime.fromtimestamp(int(internal_ms) / 1000, timezone.utc)
            results.append(
                GmailSearchResult(
                    message_id,
                    str(payload.get("threadId", "")),
                    scope.gmail_mailbox,
                    frozenset(payload.get("labelIds", [])),
                    observed,
                    headers.get("from", ""),
                    headers.get("subject", ""),
                    sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest(),
                )
            )
        return results

    def _token(self) -> str:
        return self.secret_resolver.resolve(self.access_token_secret_ref)

    def create_request_draft(self, recipient: str, subject: str, body: str, marker: str) -> GmailDraft:
        if not recipient or not subject or not marker:
            raise ProviderReadError("Gmail recovery draft parameters are incomplete")
        raw = (
            f"To: {recipient}\r\nSubject: {subject}\r\nMIME-Version: 1.0\r\n"
            "Content-Type: text/plain; charset=UTF-8\r\n\r\n"
            f"{body}\n\nReference: {marker}\n"
        ).encode()
        encoded = base64.urlsafe_b64encode(raw).decode().rstrip("=")
        response = self.transport.request(
            "POST", urljoin(self.base_url, "me/drafts"),
            {"Authorization": f"Bearer {self._token()}"}, {"message": {"raw": encoded}},
        )
        draft_id = response.body.get("id")
        if response.status_code >= 400 or not isinstance(draft_id, str) or not draft_id:
            raise ProviderReadError("Gmail recovery draft could not be created")
        return GmailDraft(draft_id, marker)

    def send_approved_request(self, draft_id: str) -> GmailSendResult:
        response = self.transport.request(
            "POST", urljoin(self.base_url, "me/drafts/send"), {"Authorization": f"Bearer {self._token()}"}, {"id": draft_id},
        )
        message_id, thread_id = response.body.get("id"), response.body.get("threadId")
        if response.status_code >= 400 or not isinstance(message_id, str) or not isinstance(thread_id, str):
            raise ProviderReadError("Gmail recovery draft could not be sent")
        return GmailSendResult(message_id, thread_id)

    def search_sent_by_marker(self, marker: str) -> Sequence[GmailSendResult] | None:
        response = self.transport.request(
            "GET", urljoin(self.base_url, "me/messages") + "?" + urlencode({"q": f'in:sent "{marker}"'}),
            {"Authorization": f"Bearer {self._token()}"},
        )
        if response.status_code >= 500:
            return None
        if response.status_code >= 400:
            raise ProviderReadError("Gmail sent-mail recovery search failed")
        messages = _records(response.body, "messages")
        return tuple(
            GmailSendResult(str(item["id"]), str(item.get("threadId", "")))
            for item in messages
            if isinstance(item.get("id"), str)
        )
