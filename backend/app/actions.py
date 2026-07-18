"""Frozen approval packages and the bounded Xero DRAFT action gateway."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from hashlib import sha256
from typing import Protocol, Sequence
from uuid import uuid4

from .domain import JournalProposal, PolicyError


class ActionPolicyError(PolicyError):
    """Raised when an external accounting action is not exactly authorized."""


@dataclass(frozen=True)
class ReviewPackage:
    package_id: str
    run_id: str
    snapshot_id: str
    snapshot_hash: str
    proposals: tuple[JournalProposal, ...]
    package_hash: str

    @classmethod
    def freeze(cls, run_id: str, snapshot_id: str, snapshot_hash: str, proposals: Sequence[JournalProposal]) -> "ReviewPackage":
        frozen = tuple(proposals)
        package_hash = sha256(
            f"{snapshot_id}|{snapshot_hash}|{'|'.join(item.proposal_hash for item in frozen)}".encode()
        ).hexdigest()
        return cls(str(uuid4()), run_id, snapshot_id, snapshot_hash, frozen, package_hash)


class ApprovalDecision(str, Enum):
    APPROVED = "approved"
    CHANGES_REQUESTED = "changes_requested"


@dataclass(frozen=True)
class ControllerDecision:
    approval_id: str
    package_hash: str
    actor_subject: str
    decision: ApprovalDecision
    decided_at: datetime
    comment: str = ""


class XeroActionStatus(str, Enum):
    PREPARED = "prepared"
    STARTED = "started"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    OUTCOME_UNKNOWN = "outcome_unknown"


@dataclass(frozen=True)
class XeroDraftRequest:
    action_id: str
    proposal_id: str
    proposal_hash: str
    marker: str
    narration: str
    journal_date: str
    lines: tuple[tuple[str, str, str, tuple[str, ...]], ...]
    status: str = "DRAFT"
    request_hash: str = ""


@dataclass(frozen=True)
class XeroDraftRecord:
    journal_id: str
    status: str
    narration: str
    journal_date: str
    lines: tuple[tuple[str, str, str, tuple[str, ...]], ...]
    request_hash: str


@dataclass
class XeroActionExecution:
    request: XeroDraftRequest
    approval_id: str
    status: XeroActionStatus = XeroActionStatus.PREPARED
    xero_journal_id: str | None = None


@dataclass(frozen=True)
class ActionManifest:
    action_id: str
    run_id: str
    package_hash: str
    proposal_hash: str
    request_hash: str
    xero_journal_id: str | None
    status: XeroActionStatus
    created_at: datetime


class XeroDraftClient(Protocol):
    def search_manual_journals(self, marker: str) -> Sequence[XeroDraftRecord] | None:
        ...

    def create_draft_manual_journal(self, request: XeroDraftRequest) -> XeroDraftRecord:
        ...

    def get_manual_journal(self, journal_id: str) -> XeroDraftRecord:
        ...


def _request_lines(proposal: JournalProposal) -> tuple[tuple[str, str, str, tuple[str, ...]], ...]:
    return tuple(
        (line.account_code, str(line.debit), str(line.credit), tuple(line.evidence_ids))
        for line in proposal.lines
    )


class XeroPolicyGateway:
    """Only exposes draft creation; no post, update, delete, or void method."""

    def __init__(self, client: XeroDraftClient, controller_subject: str) -> None:
        self.client = client
        self.controller_subject = controller_subject
        self.decisions: dict[str, ControllerDecision] = {}
        self.executions: dict[str, XeroActionExecution] = {}
        self.manifests: dict[str, ActionManifest] = {}

    def record_approval(self, package: ReviewPackage, actor_subject: str, package_hash: str, *, comment: str = "") -> ControllerDecision:
        if actor_subject != self.controller_subject:
            raise ActionPolicyError("only the configured controller may approve")
        if package_hash != package.package_hash:
            raise ActionPolicyError("approval must reference the frozen package hash")
        decision = ControllerDecision(str(uuid4()), package_hash, actor_subject, ApprovalDecision.APPROVED, datetime.now(timezone.utc), comment)
        self.decisions[package.package_id] = decision
        return decision

    def request_changes(self, package: ReviewPackage, actor_subject: str, package_hash: str, *, comment: str = "") -> ControllerDecision:
        if actor_subject != self.controller_subject or package_hash != package.package_hash:
            raise ActionPolicyError("changes request must reference the controller and frozen package")
        decision = ControllerDecision(str(uuid4()), package_hash, actor_subject, ApprovalDecision.CHANGES_REQUESTED, datetime.now(timezone.utc), comment)
        self.decisions[package.package_id] = decision
        return decision

    def _execution(self, package: ReviewPackage, proposal: JournalProposal, decision: ControllerDecision) -> XeroActionExecution:
        for execution in self.executions.values():
            if execution.request.proposal_hash == proposal.proposal_hash and execution.approval_id == decision.approval_id:
                return execution
        action_id = str(uuid4())
        marker = f"AOSMJv1/{action_id}/{proposal.proposal_hash[:16]}"
        narration = f"{marker} | {proposal.display_narration}"
        lines = _request_lines(proposal)
        request_hash = sha256(f"{proposal.proposal_hash}|{narration}|DRAFT|{lines}".encode()).hexdigest()
        request = XeroDraftRequest(action_id, proposal.proposal_id, proposal.proposal_hash, marker, narration, proposal.journal_date, lines, request_hash=request_hash)
        execution = XeroActionExecution(request, decision.approval_id)
        self.executions[action_id] = execution
        self.manifests[action_id] = ActionManifest(action_id, package.run_id, package.package_hash, proposal.proposal_hash, request_hash, None, XeroActionStatus.PREPARED, datetime.now(timezone.utc))
        return execution

    @staticmethod
    def _matches(request: XeroDraftRequest, record: XeroDraftRecord) -> bool:
        return (
            record.status == "DRAFT"
            and record.narration == request.narration
            and record.journal_date == request.journal_date
            and record.lines == request.lines
            and record.request_hash == request.request_hash
        )

    def create_draft(self, package: ReviewPackage, proposal_id: str) -> XeroActionExecution:
        decision = self.decisions.get(package.package_id)
        if decision is None or decision.decision != ApprovalDecision.APPROVED or decision.package_hash != package.package_hash:
            raise ActionPolicyError("Xero actions require an approved frozen package")
        proposal = next((item for item in package.proposals if item.proposal_id == proposal_id), None)
        if proposal is None:
            raise ActionPolicyError("proposal is not part of the approved package")
        execution = self._execution(package, proposal, decision)
        if execution.status == XeroActionStatus.SUCCEEDED:
            return execution
        execution.status = XeroActionStatus.STARTED
        try:
            found = self.client.search_manual_journals(execution.request.marker)
        except Exception:
            found = None
        if found is None:
            execution.status = XeroActionStatus.OUTCOME_UNKNOWN
            self._update_manifest(execution, package)
            return execution
        if len(found) > 1:
            execution.status = XeroActionStatus.FAILED
            self._update_manifest(execution, package)
            return execution
        try:
            record = found[0] if found else self.client.create_draft_manual_journal(execution.request)
            if not self._matches(execution.request, record):
                execution.status = XeroActionStatus.FAILED
                self._update_manifest(execution, package)
                return execution
            read_back = self.client.get_manual_journal(record.journal_id)
        except Exception:
            found_after = self.client.search_manual_journals(execution.request.marker)
            if found_after is None or len(found_after) != 1:
                execution.status = XeroActionStatus.OUTCOME_UNKNOWN
                self._update_manifest(execution, package)
                return execution
            record = found_after[0]
            if not self._matches(execution.request, record):
                execution.status = XeroActionStatus.FAILED
                self._update_manifest(execution, package)
                return execution
            read_back = self.client.get_manual_journal(record.journal_id)
        if not self._matches(execution.request, read_back):
            execution.status = XeroActionStatus.FAILED
            self._update_manifest(execution, package)
            return execution
        execution.xero_journal_id = read_back.journal_id
        execution.status = XeroActionStatus.SUCCEEDED
        self._update_manifest(execution, package)
        return execution

    def _update_manifest(self, execution: XeroActionExecution, package: ReviewPackage) -> None:
        existing = self.manifests[execution.request.action_id]
        self.manifests[execution.request.action_id] = ActionManifest(
            existing.action_id,
            existing.run_id,
            existing.package_hash,
            existing.proposal_hash,
            existing.request_hash,
            execution.xero_journal_id,
            execution.status,
            existing.created_at,
        )
