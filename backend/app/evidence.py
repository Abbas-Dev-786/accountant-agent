"""Scoped evidence collection and policy-controlled Gmail requests.

This module keeps provider payloads at the worker boundary.  It stores only
immutable evidence metadata and content hashes in the application facts used by
checklists and later AI validation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from enum import Enum
from hashlib import sha256
from typing import Mapping, Protocol, Sequence
from uuid import uuid4

from .domain import PolicyError


class EvidencePolicyError(PolicyError):
    """Raised when evidence or an email request violates configured policy."""


@dataclass(frozen=True)
class EvidenceScope:
    drive_folder_ids: frozenset[str]
    gmail_mailbox: str
    gmail_labels: frozenset[str]
    start_date: date
    end_date: date

    def __post_init__(self) -> None:
        if not self.drive_folder_ids:
            raise EvidencePolicyError("at least one Drive evidence folder is required")
        if not self.gmail_mailbox:
            raise EvidencePolicyError("a Gmail evidence mailbox is required")
        if self.end_date < self.start_date:
            raise EvidencePolicyError("evidence date range is inverted")


@dataclass(frozen=True)
class DriveSearchResult:
    resource_id: str
    folder_id: str
    name: str
    mime_type: str
    modified_at: datetime
    content_hash: str
    tags: frozenset[str] = frozenset()


@dataclass(frozen=True)
class GmailSearchResult:
    message_id: str
    thread_id: str
    mailbox: str
    labels: frozenset[str]
    internal_at: datetime
    sender: str
    subject: str
    content_hash: str
    tags: frozenset[str] = frozenset()


class DriveEvidenceClient(Protocol):
    def search_evidence(self, scope: EvidenceScope) -> Sequence[DriveSearchResult]:
        ...


class GmailEvidenceClient(Protocol):
    def search_evidence(self, scope: EvidenceScope) -> Sequence[GmailSearchResult]:
        ...


@dataclass(frozen=True)
class EvidenceItem:
    evidence_id: str
    provider: str
    source_id: str
    content_hash: str
    observed_at: datetime
    kind: str
    scope_reference: str
    tags: frozenset[str] = frozenset()
    metadata: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class EvidenceBatch:
    batch_id: str
    scope: EvidenceScope
    items: tuple[EvidenceItem, ...]
    completed_at: datetime
    query_ids: tuple[str, ...] = ()
    complete: bool = True
    warnings: tuple[str, ...] = ()

    def validate(self) -> None:
        if not self.complete or self.warnings:
            raise EvidencePolicyError("evidence search is incomplete or warning-bearing")
        seen: set[tuple[str, str]] = set()
        for item in self.items:
            identity = (item.provider, item.source_id)
            if identity in seen:
                raise EvidencePolicyError("evidence source appears more than once")
            seen.add(identity)


def _iso(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


class EvidenceCollector:
    def __init__(self, drive: DriveEvidenceClient, gmail: GmailEvidenceClient) -> None:
        self.drive = drive
        self.gmail = gmail

    def collect(self, scope: EvidenceScope) -> EvidenceBatch:
        items: list[EvidenceItem] = []
        query_ids: list[str] = []
        for result in self.drive.search_evidence(scope):
            if result.folder_id not in scope.drive_folder_ids:
                raise EvidencePolicyError("Drive search returned an out-of-scope folder")
            observed = _iso(result.modified_at)
            if not scope.start_date <= observed.date() <= scope.end_date:
                # Configured evidence folders commonly contain prior-period
                # support. It is not a provider-scope violation; it simply is
                # not evidence for this close period.
                continue
            if not result.resource_id or not result.content_hash:
                raise EvidencePolicyError("Drive evidence requires an id and content hash")
            items.append(
                EvidenceItem(
                    evidence_id=f"drive:{result.resource_id}:{result.content_hash[:24]}",
                    provider="drive",
                    source_id=result.resource_id,
                    content_hash=result.content_hash,
                    observed_at=observed,
                    kind="document",
                    scope_reference=result.folder_id,
                    tags=result.tags,
                    metadata=(("name", result.name), ("mime_type", result.mime_type)),
                )
            )
        for result in self.gmail.search_evidence(scope):
            if result.mailbox != scope.gmail_mailbox:
                raise EvidencePolicyError("Gmail search returned an out-of-scope mailbox")
            if not result.labels.intersection(scope.gmail_labels):
                # Retain the fail-closed mailbox boundary while filtering a
                # benign result from a broad/inconsistent provider search.
                continue
            observed = _iso(result.internal_at)
            if not scope.start_date <= observed.date() <= scope.end_date:
                continue
            if not result.message_id or not result.content_hash:
                raise EvidencePolicyError("Gmail evidence requires an id and content hash")
            items.append(
                EvidenceItem(
                    evidence_id=f"gmail:{result.message_id}:{result.content_hash[:24]}",
                    provider="gmail",
                    source_id=result.message_id,
                    content_hash=result.content_hash,
                    observed_at=observed,
                    kind="email",
                    scope_reference=scope.gmail_mailbox,
                    tags=result.tags,
                    metadata=(("thread_id", result.thread_id), ("sender", result.sender), ("subject", result.subject)),
                )
            )
        batch = EvidenceBatch(str(uuid4()), scope, tuple(items), datetime.now(timezone.utc), tuple(query_ids))
        batch.validate()
        return batch


@dataclass(frozen=True)
class ChecklistRequirement:
    requirement_id: str
    description: str
    required_tags: frozenset[str]
    allowed_kinds: frozenset[str] = frozenset({"document", "email"})


@dataclass(frozen=True)
class ChecklistVersion:
    checklist_id: str
    version: int
    requirements: tuple[ChecklistRequirement, ...]

    def __post_init__(self) -> None:
        if self.version < 1 or not self.requirements:
            raise EvidencePolicyError("a checklist needs a positive version and requirements")


@dataclass(frozen=True)
class MissingDocument:
    requirement_id: str
    description: str


@dataclass(frozen=True)
class ChecklistEvaluation:
    checklist_id: str
    checklist_version: int
    evidence_batch_id: str
    satisfied: tuple[str, ...]
    missing: tuple[MissingDocument, ...]

    @property
    def ready(self) -> bool:
        return not self.missing


def evaluate_checklist(checklist: ChecklistVersion, batch: EvidenceBatch) -> ChecklistEvaluation:
    batch.validate()
    satisfied: list[str] = []
    missing: list[MissingDocument] = []
    for requirement in checklist.requirements:
        found = any(
            requirement.required_tags.issubset(item.tags) and item.kind in requirement.allowed_kinds
            for item in batch.items
        )
        if found:
            satisfied.append(requirement.requirement_id)
        else:
            missing.append(MissingDocument(requirement.requirement_id, requirement.description))
    return ChecklistEvaluation(checklist.checklist_id, checklist.version, batch.batch_id, tuple(satisfied), tuple(missing))


class GmailActionStatus(str, Enum):
    PREPARED = "prepared"
    DRAFTED = "drafted"
    SENT = "sent"
    FAILED = "failed"
    OUTCOME_UNKNOWN = "outcome_unknown"


@dataclass(frozen=True)
class EmailTemplate:
    version: str
    subject: str
    body: str


@dataclass(frozen=True)
class EmailRequest:
    recipient: str
    template: EmailTemplate
    missing_requirement_ids: tuple[str, ...]
    attachments: tuple[str, ...] = ()


@dataclass
class GmailActionExecution:
    action_id: str
    marker: str
    request_hash: str
    request: EmailRequest
    status: GmailActionStatus = GmailActionStatus.PREPARED
    gmail_message_id: str | None = None
    gmail_thread_id: str | None = None


@dataclass(frozen=True)
class GmailDraft:
    draft_id: str
    marker: str


@dataclass(frozen=True)
class GmailSendResult:
    message_id: str
    thread_id: str


class GmailRequestClient(Protocol):
    def create_request_draft(self, recipient: str, subject: str, body: str, marker: str) -> GmailDraft:
        ...

    def send_approved_request(self, draft_id: str) -> GmailSendResult:
        ...

    def search_sent_by_marker(self, marker: str) -> Sequence[GmailSendResult] | None:
        ...


@dataclass(frozen=True)
class EmailPolicy:
    allowlisted_addresses: frozenset[str]
    allowlisted_domains: frozenset[str]
    approved_template_versions: frozenset[str]
    max_per_run: int = 1
    max_per_recipient: int = 1
    prohibited_terms: frozenset[str] = frozenset(
        {"payment instruction", "bank account", "credential", "password", "journal posting", "legal commitment"}
    )

    def authorize(self, request: EmailRequest, *, run_count: int, recipient_count: int) -> None:
        recipient = request.recipient.strip().lower()
        if "@" not in recipient:
            raise EvidencePolicyError("email recipient is invalid")
        local, domain = recipient.rsplit("@", 1)
        if not local or not domain:
            raise EvidencePolicyError("email recipient is invalid")
        if recipient not in self.allowlisted_addresses and domain not in self.allowlisted_domains:
            raise EvidencePolicyError("email recipient is not allowlisted")
        if request.template.version not in self.approved_template_versions:
            raise EvidencePolicyError("email template is not approved")
        if request.attachments:
            raise EvidencePolicyError("automatic missing-document email cannot include attachments")
        body = f"{request.template.subject}\n{request.template.body}".lower()
        if any(term.lower() in body for term in self.prohibited_terms):
            raise EvidencePolicyError("email content violates the automatic-send policy")
        if run_count >= self.max_per_run or recipient_count >= self.max_per_recipient:
            raise EvidencePolicyError("email rate limit exceeded")


class GmailRequestService:
    def __init__(self, client: GmailRequestClient, policy: EmailPolicy) -> None:
        self.client = client
        self.policy = policy
        self.executions: dict[str, GmailActionExecution] = {}

    def prepare(self, request: EmailRequest) -> GmailActionExecution:
        request_hash = sha256(
            f"{request.recipient}|{request.template.version}|{request.template.subject}|{request.template.body}|{','.join(request.missing_requirement_ids)}".encode()
        ).hexdigest()
        for execution in self.executions.values():
            if execution.request_hash == request_hash:
                return execution
        self.policy.authorize(
            request,
            run_count=len(self.executions),
            recipient_count=sum(item.request.recipient == request.recipient for item in self.executions.values()),
        )
        action_id = str(uuid4())
        marker = f"AOSMAILv1/{action_id}"
        execution = GmailActionExecution(action_id, marker, request_hash, request)
        self.executions[action_id] = execution
        return execution

    def send(self, action_id: str) -> GmailActionExecution:
        execution = self.executions[action_id]
        if execution.status == GmailActionStatus.SENT:
            return execution
        self.policy.authorize(
            execution.request,
            run_count=sum(
                item.status in {GmailActionStatus.DRAFTED, GmailActionStatus.SENT}
                for key, item in self.executions.items()
                if key != action_id
            ),
            recipient_count=sum(
                item.request.recipient == execution.request.recipient
                and item.status == GmailActionStatus.SENT
                for key, item in self.executions.items()
                if key != action_id
            ),
        )
        try:
            draft = self.client.create_request_draft(
                execution.request.recipient,
                execution.request.template.subject,
                execution.request.template.body,
                execution.marker,
            )
            execution.status = GmailActionStatus.DRAFTED
            result = self.client.send_approved_request(draft.draft_id)
        except Exception:
            found = self.client.search_sent_by_marker(execution.marker)
            if found is None or len(found) > 1:
                execution.status = GmailActionStatus.OUTCOME_UNKNOWN
                return execution
            if not found:
                execution.status = GmailActionStatus.FAILED
                return execution
            result = found[0]
        execution.gmail_message_id = result.message_id
        execution.gmail_thread_id = result.thread_id
        execution.status = GmailActionStatus.SENT
        return execution
