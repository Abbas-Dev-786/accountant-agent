"""Server-side Plaid Link token and public-token exchange boundary.

The browser receives only a short-lived Link token and returns a one-time public
token.  Plaid client secrets and long-lived access tokens never cross the API
boundary into the browser or the workflow database.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Mapping, Protocol, Sequence
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .xero_oauth import SecretStore


class PlaidLinkError(RuntimeError):
    """Raised when a Plaid Link operation cannot safely complete."""


@dataclass(frozen=True)
class PlaidLinkConfig:
    client_id: str
    client_secret_ref: str
    webhook_url: str
    base_url: str = "https://production.plaid.com"

    def __post_init__(self) -> None:
        if not self.client_id or self.client_id.startswith("replace-"):
            raise PlaidLinkError("Plaid client ID must be configured")
        if not self.client_secret_ref.startswith("secret://"):
            raise PlaidLinkError("Plaid client secret must be a secret:// reference")
        if not self.webhook_url.startswith("https://"):
            raise PlaidLinkError("Plaid production webhook must use HTTPS")
        if self.base_url != "https://production.plaid.com":
            raise PlaidLinkError("Plaid Link must use the Production endpoint")

    @classmethod
    def from_environment(cls, env: Mapping[str, str] | None = None) -> "PlaidLinkConfig":
        values = os.environ if env is None else env
        return cls(
            values.get("PLAID_CLIENT_ID", ""),
            values.get("PLAID_SECRET_REF", ""),
            values.get("PLAID_WEBHOOK_URL", ""),
            values.get("PLAID_PRODUCTION_URL", "https://production.plaid.com"),
        )


@dataclass(frozen=True)
class JsonResponse:
    status_code: int
    body: Mapping[str, object]


class JsonTransport(Protocol):
    def post(self, url: str, payload: Mapping[str, object]) -> JsonResponse:
        ...


class UrllibJsonTransport:
    def post(self, url: str, payload: Mapping[str, object]) -> JsonResponse:
        request = Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=30) as response:
                body = json.loads(response.read().decode("utf-8"))
                if not isinstance(body, Mapping):
                    raise PlaidLinkError("Plaid response is not an object")
                return JsonResponse(response.status, body)
        except HTTPError as exc:
            try:
                body = json.loads(exc.read().decode("utf-8", errors="replace"))
            except json.JSONDecodeError:
                body = {}
            return JsonResponse(exc.code, body if isinstance(body, Mapping) else {})
        except (URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise PlaidLinkError("Plaid request failed") from exc


@dataclass(frozen=True)
class LinkedPlaidAccount:
    account_id: str
    name: str
    mask: str | None
    subtype: str | None


@dataclass(frozen=True)
class LinkedPlaidItem:
    item_id: str
    access_token: str
    accounts: tuple[LinkedPlaidAccount, ...]


class PlaidLinkClient:
    def __init__(self, config: PlaidLinkConfig, secrets: SecretStore, transport: JsonTransport | None = None) -> None:
        self.config = config
        self.secrets = secrets
        self.transport = transport or UrllibJsonTransport()

    def create_link_token(self, organization_id: str) -> tuple[str, str | None]:
        if not organization_id:
            raise PlaidLinkError("organization ID is required for Plaid Link")
        response = self._post(
            "/link/token/create",
            {
                "user": {"client_user_id": organization_id},
                "client_name": "AccountingOS",
                "products": ["transactions"],
                "country_codes": ["US"],
                "language": "en",
                "webhook": self.config.webhook_url,
                "transactions": {"days_requested": 730},
                "account_selection_enabled": True,
            },
        )
        token = response.get("link_token")
        expiration = response.get("expiration")
        if not isinstance(token, str) or not token:
            raise PlaidLinkError("Plaid did not return a Link token")
        return token, expiration if isinstance(expiration, str) else None

    def exchange_public_token(self, public_token: str, selected_account_ids: Sequence[str]) -> LinkedPlaidItem:
        if not public_token or len(public_token) > 2000:
            raise PlaidLinkError("Plaid public token is invalid")
        response = self._post("/item/public_token/exchange", {"public_token": public_token})
        access_token = response.get("access_token")
        item_id = response.get("item_id")
        if not isinstance(access_token, str) or not access_token or not isinstance(item_id, str) or not item_id:
            raise PlaidLinkError("Plaid public-token exchange response is incomplete")
        accounts = self._accounts(access_token)
        selected = tuple(dict.fromkeys(account_id.strip() for account_id in selected_account_ids if account_id.strip()))
        if not selected:
            raise PlaidLinkError("select at least one bank account in Plaid Link")
        by_id = {item.account_id: item for item in accounts}
        if any(account_id not in by_id for account_id in selected):
            raise PlaidLinkError("Plaid Link account selection could not be verified")
        return LinkedPlaidItem(item_id, access_token, tuple(by_id[account_id] for account_id in selected))

    def _accounts(self, access_token: str) -> tuple[LinkedPlaidAccount, ...]:
        response = self._post("/accounts/get", {"access_token": access_token})
        raw_accounts = response.get("accounts")
        if not isinstance(raw_accounts, list):
            raise PlaidLinkError("Plaid did not return linked accounts")
        accounts: list[LinkedPlaidAccount] = []
        for item in raw_accounts:
            if not isinstance(item, Mapping):
                raise PlaidLinkError("Plaid account response is invalid")
            account_id = item.get("account_id")
            if not isinstance(account_id, str) or not account_id:
                raise PlaidLinkError("Plaid account is missing an ID")
            accounts.append(
                LinkedPlaidAccount(
                    account_id,
                    str(item.get("name", "Selected bank account")),
                    str(item["mask"]) if isinstance(item.get("mask"), str) else None,
                    str(item["subtype"]) if isinstance(item.get("subtype"), str) else None,
                )
            )
        return tuple(accounts)

    def _post(self, path: str, body: Mapping[str, object]) -> Mapping[str, object]:
        secret = self.secrets.resolve(self.config.client_secret_ref)
        response = self.transport.post(
            f"{self.config.base_url}{path}",
            {"client_id": self.config.client_id, "secret": secret, **body},
        )
        if response.status_code >= 400:
            raise PlaidLinkError(f"Plaid request returned HTTP {response.status_code}")
        return response.body
