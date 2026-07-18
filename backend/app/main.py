"""FastAPI shell for the AccountingOS foundation."""

from __future__ import annotations

import os
from decimal import Decimal

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .domain import CloseService, DeploymentConfig, JournalLine, JournalProposal, PolicyError


def deployment_from_environment() -> DeploymentConfig:
    return DeploymentConfig(
        deployment_id=os.getenv("ACCOUNTINGOS_DEPLOYMENT_ID", "demo-us"),
        mode=os.getenv("ACCOUNTINGOS_DEPLOYMENT_MODE", "demo"),
        data_class=os.getenv("ACCOUNTINGOS_DATA_CLASS", "synthetic"),
        market=os.getenv("ACCOUNTINGOS_MARKET", "US"),
        currency=os.getenv("ACCOUNTINGOS_CURRENCY", "USD"),
        controller_subject=os.getenv("ACCOUNTINGOS_CONTROLLER_SUBJECT", "demo-controller"),
    )


service = CloseService(deployment_from_environment())
app = FastAPI(title="AccountingOS API", version="0.1.0")


class CreateRunRequest(BaseModel):
    organization_id: str = Field(min_length=1)
    period_start: str
    period_end: str


class JournalLineRequest(BaseModel):
    account_code: str
    debit: Decimal = Decimal("0")
    credit: Decimal = Decimal("0")
    evidence_ids: list[str]


class ProposalRequest(BaseModel):
    proposal_id: str
    journal_date: str
    narration: str
    lines: list[JournalLineRequest]


class ApprovalRequest(BaseModel):
    actor_subject: str
    package_hash: str


@app.exception_handler(PolicyError)
async def policy_error_handler(_, exc: PolicyError):
    return JSONResponse(status_code=409, content={"detail": str(exc)})


def serialize_run(run) -> dict[str, object]:
    return {
        "id": run.run_id,
        "organization_id": run.organization_id,
        "period": {"start": run.period_start, "end": run.period_end},
        "status": run.state.value,
        "deployment": {"mode": run.deployment.mode, "data_class": run.deployment.data_class},
        "snapshot_id": run.snapshot.snapshot_id if run.snapshot else None,
        "package_hash": run.package_hash,
        "actions": [
            {
                "id": action.action_id,
                "proposal_id": action.proposal_id,
                "status": action.status.value,
                "xero_journal_id": action.xero_journal_id,
            }
            for action in run.actions.values()
        ],
    }


def get_run(run_id: str):
    run = service.runs.get(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="close run not found")
    return run


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "mode": service.deployment.mode, "data_class": service.deployment.data_class}


@app.post("/api/v1/close-runs", status_code=201)
def create_close_run(request: CreateRunRequest) -> dict[str, str]:
    run = service.create_run(request.organization_id, request.period_start, request.period_end)
    service.begin_sync(run)
    return {"id": run.run_id, "status": run.state.value, "data_class": run.deployment.data_class}


@app.get("/api/v1/close-runs/{run_id}")
def get_close_run(run_id: str) -> dict[str, object]:
    return serialize_run(get_run(run_id))


@app.post("/api/v1/close-runs/{run_id}/prepare-review")
def prepare_review(run_id: str, proposals: list[ProposalRequest]) -> dict[str, str]:
    run = get_run(run_id)
    try:
        domain_proposals = [
            JournalProposal(
                item.proposal_id,
                item.journal_date,
                item.narration,
                tuple(
                    JournalLine(line.account_code, line.debit, line.credit, tuple(line.evidence_ids))
                    for line in item.lines
                ),
            )
            for item in proposals
        ]
        package_hash = service.prepare_for_review(run, domain_proposals)
    except PolicyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"package_hash": package_hash, "status": run.state.value}


@app.post("/api/v1/close-runs/{run_id}/approvals")
def approve_close_run(run_id: str, request: ApprovalRequest) -> dict[str, object]:
    run = get_run(run_id)
    try:
        service.approve(run, request.actor_subject, request.package_hash)
    except PolicyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return serialize_run(run)

