"""Backblaze B2 immutable close-package uploader.

This client uses the native B2 API so the retention headers are explicit and
testable. It uploads only a worker-produced JSON package, never source tokens
or provider payloads that are not part of the review package.
"""

from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha1, sha256
from typing import Mapping, Protocol
from urllib.parse import quote
from urllib.request import Request, urlopen

class B2Error(RuntimeError):
    pass


def _environment_secret(values: Mapping[str, str], name: str, legacy_name: str) -> str:
    """Read a static server credential without consulting Supabase Vault."""
    value = values.get(name, "").strip()
    if value:
        return value
    legacy_value = values.get(legacy_name, "").strip()
    return "" if legacy_value.startswith("secret://") else legacy_value


@dataclass(frozen=True)
class B2Config:
    bucket_name: str
    key_id: str
    application_key: str
    retention_days: int = 2555

    def __post_init__(self) -> None:
        if not self.bucket_name or self.bucket_name.startswith("replace-"):
            raise B2Error("B2_BUCKET_NAME must be configured")
        if (
            not self.key_id
            or not self.application_key
            or self.key_id.startswith(("replace-", "secret://"))
            or self.application_key.startswith(("replace-", "secret://"))
        ):
            raise B2Error("B2_KEY_ID and B2_APPLICATION_KEY must be configured server-side")
        if not 1 <= self.retention_days <= 36500:
            raise B2Error("B2 retention days must be between 1 and 36500")

    @classmethod
    def from_environment(cls, env: Mapping[str, str] | None = None) -> "B2Config":
        values = os.environ if env is None else env
        try:
            retention_days = int(values.get("B2_OBJECT_LOCK_RETENTION_DAYS", "2555"))
        except ValueError as exc:
            raise B2Error("B2_OBJECT_LOCK_RETENTION_DAYS must be an integer") from exc
        return cls(
            values.get("B2_BUCKET_NAME", "").strip(),
            _environment_secret(values, "B2_KEY_ID", "B2_KEY_ID_REF"),
            _environment_secret(values, "B2_APPLICATION_KEY", "B2_APPLICATION_KEY_REF"),
            retention_days,
        )


@dataclass(frozen=True)
class B2Response:
    status_code: int
    body: Mapping[str, object]


class B2Transport(Protocol):
    def request(self, method: str, url: str, headers: Mapping[str, str], body: bytes | None = None) -> B2Response:
        ...


class UrllibB2Transport:
    def request(self, method: str, url: str, headers: Mapping[str, str], body: bytes | None = None) -> B2Response:
        request = Request(url, data=body, headers=dict(headers), method=method)
        try:
            with urlopen(request, timeout=30) as response:
                raw = response.read().decode("utf-8")
                return B2Response(response.status, json.loads(raw) if raw else {})
        except Exception as exc:
            raise B2Error("B2 request failed") from exc


@dataclass(frozen=True)
class B2Artifact:
    object_key: str
    content_hash: str
    file_id: str
    retain_until: datetime


class B2ObjectLockClient:
    def __init__(self, config: B2Config, *, transport: B2Transport | None = None) -> None:
        self.config = config
        self.transport = transport or UrllibB2Transport()

    def _authorized(self) -> Mapping[str, object]:
        basic = base64.b64encode(f"{self.config.key_id}:{self.config.application_key}".encode()).decode()
        response = self.transport.request(
            "GET", "https://api.backblazeb2.com/b2api/v2/b2_authorize_account",
            {"Authorization": f"Basic {basic}", "Accept": "application/json"},
        )
        if response.status_code >= 400 or not response.body.get("authorizationToken") or not response.body.get("apiUrl"):
            raise B2Error("B2 authorization failed")
        return response.body

    def _bucket_id(self, authorization: Mapping[str, object]) -> str:
        allowed = authorization.get("allowed")
        if isinstance(allowed, Mapping) and isinstance(allowed.get("bucketId"), str) and allowed.get("bucketId"):
            return str(allowed["bucketId"])
        api_url = str(authorization["apiUrl"]).rstrip("/")
        token = str(authorization["authorizationToken"])
        response = self.transport.request(
            "POST", f"{api_url}/b2api/v2/b2_list_buckets",
            {"Authorization": token, "Content-Type": "application/json"},
            json.dumps({"accountId": authorization.get("accountId"), "bucketName": self.config.bucket_name}).encode(),
        )
        buckets = response.body.get("buckets")
        if response.status_code >= 400 or not isinstance(buckets, list) or len(buckets) != 1 or not isinstance(buckets[0], Mapping):
            raise B2Error("configured B2 bucket could not be resolved")
        bucket_id = buckets[0].get("bucketId")
        if not isinstance(bucket_id, str) or not bucket_id:
            raise B2Error("configured B2 bucket has no id")
        return bucket_id

    def upload_close_package(self, *, run_id: str, package: Mapping[str, object]) -> B2Artifact:
        encoded = json.dumps(package, sort_keys=True, separators=(",", ":"), default=str).encode()
        content_hash = sha256(encoded).hexdigest()
        object_key = f"accountingos/close-runs/{run_id}/{content_hash}.json"
        retain_until = datetime.now(timezone.utc) + timedelta(days=self.config.retention_days)
        authorization = self._authorized()
        bucket_id = self._bucket_id(authorization)
        api_url = str(authorization["apiUrl"]).rstrip("/")
        token = str(authorization["authorizationToken"])
        upload = self.transport.request(
            "POST", f"{api_url}/b2api/v2/b2_get_upload_url",
            {"Authorization": token, "Content-Type": "application/json"},
            json.dumps({"bucketId": bucket_id}).encode(),
        )
        upload_url, upload_token = upload.body.get("uploadUrl"), upload.body.get("authorizationToken")
        if upload.status_code >= 400 or not isinstance(upload_url, str) or not isinstance(upload_token, str):
            raise B2Error("B2 upload URL could not be acquired")
        response = self.transport.request(
            "POST", upload_url,
            {
                "Authorization": upload_token,
                "Content-Type": "application/json",
                "Content-Length": str(len(encoded)),
                "X-Bz-File-Name": quote(object_key, safe="/"),
                "X-Bz-Content-Sha1": sha1(encoded).hexdigest(),
                "X-Bz-File-Retention-Mode": "compliance",
                "X-Bz-File-Retention-Retain-Until-Timestamp": str(int(retain_until.timestamp() * 1000)),
            },
            encoded,
        )
        file_id = response.body.get("fileId")
        retention = response.body.get("fileRetention")
        if response.status_code >= 400 or not isinstance(file_id, str) or not file_id:
            raise B2Error("B2 close-package upload failed")
        if not isinstance(retention, Mapping) or retention.get("mode") != "compliance":
            raise B2Error("B2 did not confirm compliance Object Lock retention")
        return B2Artifact(object_key, content_hash, file_id, retain_until)
