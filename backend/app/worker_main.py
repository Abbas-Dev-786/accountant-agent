"""Command-line process for the server-owned AccountingOS workflow worker."""

from __future__ import annotations

import argparse
import logging
import os
import socket
import time

from .domain import DeploymentConfig
from .durable_worker import (
    DemoSourceSyncExecutor,
    DurableWorkflowWorker,
    EnvironmentPreflightExecutor,
    DurableReconciliationExecutor,
    GmailRecoveryActionExecutor,
    GoogleEvidenceExecutor,
    ProductionPreflightExecutor,
    ProductionSourceSyncExecutor,
    PlaidWebhookSyncWorker,
    RegisteredTaskExecutor,
    XeroDraftActionExecutor,
)
from .supabase_db import SupabaseDatabaseConfig, SupabaseWorkflowStore


logger = logging.getLogger("accountingos.worker")


class WorkflowWorkerCoordinator:
    """Prioritize verified Plaid sync notifications without starving close tasks."""

    def __init__(self, close_worker: DurableWorkflowWorker, plaid_worker: PlaidWebhookSyncWorker | None) -> None:
        self.close_worker = close_worker
        self.plaid_worker = plaid_worker

    def process_once(self):
        if self.plaid_worker is not None:
            result = self.plaid_worker.process_once()
            if result.status != "idle":
                return result
        return self.close_worker.process_once()


def deployment_from_environment() -> DeploymentConfig:
    return DeploymentConfig(
        deployment_id=os.getenv("ACCOUNTINGOS_DEPLOYMENT_ID", "us-production"),
        mode=os.getenv("ACCOUNTINGOS_DEPLOYMENT_MODE", "production"),
        data_class=os.getenv("ACCOUNTINGOS_DATA_CLASS", "live"),
        market=os.getenv("ACCOUNTINGOS_MARKET", "US"),
        currency=os.getenv("ACCOUNTINGOS_CURRENCY", "USD"),
        controller_subject=os.getenv("ACCOUNTINGOS_CONTROLLER_SUBJECT", "unconfigured-controller"),
    )


def build_worker(worker_id: str) -> WorkflowWorkerCoordinator:
    store = SupabaseWorkflowStore(SupabaseDatabaseConfig.from_environment())
    deployment = deployment_from_environment()
    if deployment.mode == "production":
        handlers = {
            "preflight": ProductionPreflightExecutor(store),
            "synchronize_sources": ProductionSourceSyncExecutor(store, deployment),
            "collect_evidence": GoogleEvidenceExecutor(store),
            "reconcile": DurableReconciliationExecutor(store),
            "apply_approved_actions": XeroDraftActionExecutor(store),
            "send_recovery_request": GmailRecoveryActionExecutor(store),
        }
    else:
        # Fixture execution is retained for isolated test stacks only. It is
        # never the default and cannot process a live deployment.
        handlers = {
            "preflight": EnvironmentPreflightExecutor(),
            "synchronize_sources": DemoSourceSyncExecutor(store, deployment),
            "collect_evidence": GoogleEvidenceExecutor(store),
            "reconcile": DurableReconciliationExecutor(store),
            "apply_approved_actions": XeroDraftActionExecutor(store),
            "send_recovery_request": GmailRecoveryActionExecutor(store),
        }
    executor = RegisteredTaskExecutor(
        handlers
    )
    close_worker = DurableWorkflowWorker(store, executor, worker_id=worker_id)
    return WorkflowWorkerCoordinator(
        close_worker,
        PlaidWebhookSyncWorker(store, worker_id=worker_id) if deployment.mode == "production" else None,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the AccountingOS durable workflow worker")
    parser.add_argument("--once", action="store_true", help="claim and process at most one task")
    parser.add_argument("--poll-seconds", type=float, default=2.0)
    parser.add_argument("--worker-id", default=f"{socket.gethostname()}-{os.getpid()}")
    args = parser.parse_args()
    if args.poll_seconds <= 0:
        parser.error("--poll-seconds must be positive")
    worker = build_worker(args.worker_id)
    while True:
        try:
            result = worker.process_once()
        except Exception:
            # A transient database failure must not kill the process and strand
            # later tasks. Leases are reclaimable, and the next iteration will
            # retry the claim/finish operation safely.
            logger.exception("Worker iteration failed; retrying after the poll interval")
            if args.once:
                return 1
            time.sleep(args.poll_seconds)
            continue
        print(result.status if result.task_key is None else f"{result.status}: {result.task_key}")
        if args.once:
            return 0
        if result.status == "idle":
            time.sleep(args.poll_seconds)


if __name__ == "__main__":  # pragma: no cover - exercised by deployment
    raise SystemExit(main())
