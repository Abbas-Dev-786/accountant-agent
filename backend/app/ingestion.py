"""Worker-facing orchestration for an atomic demo source snapshot."""

from __future__ import annotations

from .domain import CloseRun, CloseService, PolicyError, SourceSnapshot, RunState
from .providers import PlaidSandboxAdapter, ProviderReadError, XeroDemoAdapter


class DemoIngestionService:
    """Read both demo providers and commit only a complete immutable snapshot."""

    def __init__(
        self,
        close_service: CloseService,
        xero: XeroDemoAdapter,
        plaid: PlaidSandboxAdapter,
    ) -> None:
        self.close_service = close_service
        self.xero = xero
        self.plaid = plaid

    def synchronize(self, run: CloseRun) -> SourceSnapshot:
        if run.state == RunState.CREATED:
            self.close_service.begin_sync(run)
        if run.state != RunState.SYNCHRONIZING:
            raise PolicyError("demo ingestion requires a synchronizing close run")
        try:
            xero_batch = self.xero.read_batch()
            plaid_batch = self.plaid.read_batch()
            return self.close_service.build_snapshot(run, (xero_batch, plaid_batch))
        except (ProviderReadError, PolicyError):
            # No provider state is committed by a failed adapter.  The run is
            # explicitly recoverable: a retry starts again from BLOCKED.
            run.state = RunState.BLOCKED
            raise
