"""Safety-critical close-readiness domain rules.

This module intentionally has no provider SDK or database dependency. Provider
adapters persist the same immutable facts later; keeping these rules pure makes
the state and accounting invariants directly testable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from hashlib import sha256
from typing import Iterable
from uuid import uuid4


class PolicyError(ValueError):
    """Raised when a request would violate the documented action policy."""


class RunState(str, Enum):
    CREATED = "created"
    PREFLIGHT = "preflight"
    SYNCHRONIZING = "synchronizing"
    RUNNING = "running"
    BLOCKED = "blocked"
    AWAITING_APPROVAL = "awaiting_approval"
    APPLYING_APPROVED_ACTIONS = "applying_approved_actions"
    ACTION_FAILED = "action_failed"
    APPROVED = "approved"
    CANCELLED = "cancelled"


class ActionStatus(str, Enum):
    PREPARED = "prepared"
    STARTED = "started"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    OUTCOME_UNKNOWN = "outcome_unknown"


@dataclass(frozen=True)
class DeploymentConfig:
    deployment_id: str
    mode: str
    data_class: str
    market: str
    currency: str
    controller_subject: str

    def __post_init__(self) -> None:
        if self.mode == "demo":
            if (self.data_class, self.market, self.currency) != ("synthetic", "US", "USD"):
                raise PolicyError("demo deployment must be synthetic US/USD")
        elif self.mode == "production":
            if self.data_class != "live":
                raise PolicyError("production deployment must use live data")
        else:
            raise PolicyError("deployment mode must be demo or production")


@dataclass(frozen=True)
class SourceRecordVersion:
    version_id: str
    provider: str
    provider_record_id: str
    content_hash: str
    observed_at: datetime
    # Canonical JSON is retained with the immutable version so downstream
    # workers can inspect the normalized facts without calling a provider.
    payload_json: str = ""
    currency: str | None = None
    accounting_date: str | None = None


@dataclass(frozen=True)
class SourceBatch:
    batch_id: str
    provider: str
    provider_environment: str
    watermark: str
    completed_at: datetime
    record_versions: tuple[SourceRecordVersion, ...]
    complete: bool = True
    warnings: tuple[str, ...] = ()

    def validate_for(self, deployment: DeploymentConfig) -> None:
        expected = {"demo": {"demo", "sandbox"}, "production": {"production"}}[deployment.mode]
        if self.provider_environment not in expected:
            raise PolicyError(f"{self.provider} environment does not match deployment")
        if not self.complete or self.warnings:
            raise PolicyError(f"{self.provider} source batch is incomplete or warning-bearing")


@dataclass(frozen=True)
class SnapshotRecord:
    record_version_id: str
    source_batch_id: str
    provider: str
    provider_record_id: str
    content_hash: str


@dataclass(frozen=True)
class SourceSnapshot:
    snapshot_id: str
    deployment_id: str
    mode: str
    data_class: str
    cutoff_at: datetime
    records: tuple[SnapshotRecord, ...]
    source_batch_ids: tuple[str, ...]


@dataclass(frozen=True)
class JournalLine:
    account_code: str
    debit: Decimal
    credit: Decimal
    evidence_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.debit < 0 or self.credit < 0:
            raise PolicyError("journal amounts cannot be negative")
        if bool(self.debit) == bool(self.credit):
            raise PolicyError("each journal line must have either a debit or a credit")
        if not self.evidence_ids:
            raise PolicyError("every journal line needs source evidence")


@dataclass(frozen=True)
class JournalProposal:
    proposal_id: str
    journal_date: str
    display_narration: str
    lines: tuple[JournalLine, ...]

    def __post_init__(self) -> None:
        if len(self.lines) < 2:
            raise PolicyError("journal needs at least two lines")
        if sum((line.debit for line in self.lines), Decimal("0")) != sum(
            (line.credit for line in self.lines), Decimal("0")
        ):
            raise PolicyError("journal proposal must balance")

    @property
    def proposal_hash(self) -> str:
        payload = "|".join(
            f"{line.account_code}:{line.debit}:{line.credit}:{','.join(line.evidence_ids)}"
            for line in self.lines
        )
        return sha256(f"{self.journal_date}|{self.display_narration}|{payload}".encode()).hexdigest()


@dataclass
class ActionExecution:
    action_id: str
    proposal_id: str
    request_hash: str
    marker: str
    expected_narration: str
    status: ActionStatus = ActionStatus.PREPARED
    xero_journal_id: str | None = None


@dataclass
class CloseRun:
    run_id: str
    organization_id: str
    period_start: str
    period_end: str
    deployment: DeploymentConfig
    state: RunState = RunState.CREATED
    snapshot: SourceSnapshot | None = None
    package_hash: str | None = None
    approved_proposals: tuple[JournalProposal, ...] = ()
    approval_actor: str | None = None
    actions: dict[str, ActionExecution] = field(default_factory=dict)


class CloseService:
    """In-memory orchestration core; persistence is added behind this boundary."""

    def __init__(self, deployment: DeploymentConfig) -> None:
        self.deployment = deployment
        self.runs: dict[str, CloseRun] = {}

    def create_run(self, organization_id: str, period_start: str, period_end: str) -> CloseRun:
        run = CloseRun(str(uuid4()), organization_id, period_start, period_end, self.deployment)
        self.runs[run.run_id] = run
        return run

    def begin_sync(self, run: CloseRun) -> None:
        self._transition(run, {RunState.CREATED, RunState.BLOCKED}, RunState.SYNCHRONIZING)

    def build_snapshot(self, run: CloseRun, batches: Iterable[SourceBatch]) -> SourceSnapshot:
        if run.state != RunState.SYNCHRONIZING:
            raise PolicyError("a snapshot can be built only while synchronizing")
        selected = tuple(batches)
        if not selected:
            raise PolicyError("a snapshot requires at least one source batch")
        records: list[SnapshotRecord] = []
        seen_versions: set[str] = set()
        seen_provider_records: set[tuple[str, str]] = set()
        for batch in selected:
            batch.validate_for(run.deployment)
            for record in batch.record_versions:
                if record.version_id in seen_versions:
                    raise PolicyError("record version appears in more than one snapshot batch")
                source_identity = (record.provider, record.provider_record_id)
                if source_identity in seen_provider_records:
                    raise PolicyError("provider source record appears more than once in a snapshot")
                seen_versions.add(record.version_id)
                seen_provider_records.add(source_identity)
                records.append(
                    SnapshotRecord(
                        record.version_id,
                        batch.batch_id,
                        record.provider,
                        record.provider_record_id,
                        record.content_hash,
                    )
                )
        snapshot = SourceSnapshot(
            str(uuid4()),
            run.deployment.deployment_id,
            run.deployment.mode,
            run.deployment.data_class,
            datetime.now(timezone.utc),
            tuple(records),
            tuple(batch.batch_id for batch in selected),
        )
        run.snapshot = snapshot
        self._transition(run, {RunState.SYNCHRONIZING}, RunState.RUNNING)
        return snapshot

    def prepare_for_review(self, run: CloseRun, proposals: Iterable[JournalProposal]) -> str:
        if run.state != RunState.RUNNING or run.snapshot is None:
            raise PolicyError("a complete source snapshot is required before review")
        proposal_list = tuple(proposals)
        hash_input = run.snapshot.snapshot_id + "|" + "|".join(
            proposal.proposal_hash for proposal in proposal_list
        )
        run.package_hash = sha256(hash_input.encode()).hexdigest()
        run.approved_proposals = proposal_list
        self._transition(run, {RunState.RUNNING}, RunState.AWAITING_APPROVAL)
        return run.package_hash

    def approve(self, run: CloseRun, actor_subject: str, package_hash: str) -> None:
        if actor_subject != run.deployment.controller_subject:
            raise PolicyError("only the configured controller may approve")
        if package_hash != run.package_hash:
            raise PolicyError("approval must reference the frozen package hash")
        run.approval_actor = actor_subject
        if not run.approved_proposals:
            self._transition(run, {RunState.AWAITING_APPROVAL}, RunState.APPROVED)
            return
        self._transition(run, {RunState.AWAITING_APPROVAL}, RunState.APPLYING_APPROVED_ACTIONS)

    def prepare_xero_action(self, run: CloseRun, proposal: JournalProposal) -> ActionExecution:
        if run.state not in {RunState.APPLYING_APPROVED_ACTIONS, RunState.ACTION_FAILED}:
            raise PolicyError("Xero actions require a frozen controller approval")
        if proposal not in run.approved_proposals:
            raise PolicyError("only an approved proposal may be sent to Xero")
        for action in run.actions.values():
            if action.proposal_id == proposal.proposal_id:
                return action
        action_id = str(uuid4())
        marker = f"AOSMJv1/{action_id}/{proposal.proposal_hash[:16]}"
        narration = f"{marker} | {proposal.display_narration}"
        request_hash = sha256(
            f"{proposal.proposal_hash}|{narration}|DRAFT".encode()
        ).hexdigest()
        action = ActionExecution(action_id, proposal.proposal_id, request_hash, marker, narration)
        run.actions[action_id] = action
        return action

    def reconcile_xero_action(
        self,
        run: CloseRun,
        action_id: str,
        xero_journal_id: str | None,
        returned_narration: str | None,
        returned_request_hash: str | None,
    ) -> ActionExecution:
        action = run.actions[action_id]
        if xero_journal_id is None:
            action.status = ActionStatus.OUTCOME_UNKNOWN
            run.state = RunState.ACTION_FAILED
            return action
        if returned_narration != action.expected_narration or returned_request_hash != action.request_hash:
            action.status = ActionStatus.FAILED
            run.state = RunState.ACTION_FAILED
            return action
        action.xero_journal_id = xero_journal_id
        action.status = ActionStatus.SUCCEEDED
        if all(item.status == ActionStatus.SUCCEEDED for item in run.actions.values()):
            run.state = RunState.APPROVED
        return action

    @staticmethod
    def _transition(run: CloseRun, allowed: set[RunState], destination: RunState) -> None:
        if run.state not in allowed:
            raise PolicyError(f"invalid transition: {run.state.value} -> {destination.value}")
        run.state = destination
