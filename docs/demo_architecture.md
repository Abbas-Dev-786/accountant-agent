# AccountingOS Demo Architecture

**Version:** 1.3
**Status:** Approved for implementation  
**Decision date:** 2026-07-18

This document defines the first implementation milestone. It is authoritative
for demo-mode boundaries and must be read with `PRD.md`, `TDD.md`, and
`live_integrations.md`.

## Purpose

The first milestone is a truthful, repeatable US close-readiness demo. It uses
synthetic financial records in isolated provider test environments while still
exercising real OAuth, provider APIs, synchronization, deterministic controls,
controller approval, Xero draft creation, read-back, and audit behavior.

Demo mode is not production acceptance and must never be presented as a live
customer close.

## Environment boundary

Demo and production are separate deployments, not a runtime toggle:

```text
Demo:       demo web/API/worker/webhooks
            demo PostgreSQL + demo B2 bucket + demo secrets
            Plaid Sandbox + Xero Demo Company + Google test Workspace

Production: production web/API/worker/webhooks
            production PostgreSQL + production B2 + production secrets
            Plaid Production/Setu + real Xero organization + Google production
```

The deployment mode and data class are immutable deployment configuration. They
are never selected by a browser request or stored as mutable organization
attributes. Every connection stores its provider environment. The server copies
the deployment mode (`demo` or `production`) and data class (`synthetic` or
`live`) into each run when it is created; database constraints reject any value
that differs from the deployment configuration.

Startup and database guards reject:

- live provider credentials in a demo deployment;
- Sandbox or test credentials in a production deployment;
- a provider tenant or Item whose environment does not match the deployment;
- demo artifacts, callbacks, webhooks, or secrets in production;
- a production close run that references synthetic data.

The UI shows a persistent `DEMO — SYNTHETIC DATA` banner and identifies the
provider environment on connection, snapshot, package, and action screens.

## Demo providers

| Capability | Demo provider/path | Data or action boundary |
| --- | --- | --- |
| Accounting reads | Xero Demo Company through the real Xero API | Sample organization only |
| Accounting ingestion | `XeroDirectDemoAdapter` | Fivetran is not used in the demo |
| Bank data | Plaid Sandbox Transactions | Synthetic Sandbox Item and transactions |
| Evidence | Google test Workspace | Test Drive folders and Gmail mailbox |
| Artifacts | Demo B2 bucket | No production artifacts or URLs |
| AI | OpenAI with synthetic, bounded evidence | No customer data or secrets |

India/Setu, Plaid Production, real customer Xero organizations, and production
Fivetran ingestion are later milestones.

## Shared source contract

Provider-specific code returns a canonical `SourceBatch` containing:

- provider and provider environment;
- tenant/account identifiers and source watermarks;
- immutable source record payloads or permitted copies;
- provider request/event IDs;
- completeness, warnings, and failure details.

The demo Xero adapter and the future Fivetran adapter implement the same
contract. Normalization, snapshots, reconciliation, reports, and workflow tasks
do not branch on provider SDK response shapes.

## Scenario bootstrap

Every demo run selects a versioned scenario manifest. The bootstrap seeds Plaid
Sandbox and the test Workspace, then verifies a prepared Xero Demo Company
baseline against the manifest. It does not claim to reset the Xero Demo Company
through the API: resetting that company is an explicit operator runbook step.

The manifest defines the fixed accounting period, expected Xero baseline
fingerprint and provider IDs, Plaid accounts and transactions, evidence,
references, fees, expected exceptions, and permitted journal proposal. The
Plaid portion uses a custom Sandbox user or dynamic test user; its data must be
created within the provider's supported date and account limitations.

The bootstrap is idempotent and records every provider ID. A partial seed or a
Xero-baseline mismatch marks the scenario unusable; it never lets a run proceed
with silently incomplete data. Repeated presentations use a new scenario
version or an operator-performed Xero Demo Company reset followed by baseline
verification, so draft journals and evidence are not mistaken for a clean run.

## Agent execution

The MVP uses logical task owners, not autonomous model sessions:

- workflow supervisor: persisted fixed DAG;
- integration executor: provider adapters and health checks;
- document executor: scoped evidence checklist;
- reconciliation executor: deterministic matching;
- journal executor: balanced proposal generation;
- reporting executor: deterministic reports;
- exception investigator: bounded evidence-grounded model call;
- notification executor: policy-controlled test email;
- workflow tracker: tasks, audit events, and SSE replay.

The model cannot reconcile amounts, approve actions, select permissions, create
arbitrary tools, or write directly to a provider.

## Demo acceptance flow

1. Connect a demo organization to Plaid Sandbox, Xero Demo Company, and the
   test Workspace.
2. Bootstrap and verify one versioned scenario.
3. Synchronize all providers and atomically record source watermarks.
4. Build an immutable snapshot and run deterministic evidence and reconciliation
   controls.
5. Generate cited exception explanations, reports, and balanced proposals.
6. Freeze the package and require controller approval.
7. Create an approved Xero `DRAFT` journal in the Demo Company, read it back,
   and emit a separate action manifest.
8. Demonstrate provider failure, duplicate webhook, ambiguous action, or
   cancellation behavior without claiming a false success.

## Explicitly prohibited in demo mode

- live customer financial data;
- live customer credentials or tenants;
- production Plaid Items or bank accounts;
- journal posting, payment, period locking, deletion, or voiding;
- silently substituting local data for a failed provider;
- calling synthetic data a live close or an approved accounting period.
