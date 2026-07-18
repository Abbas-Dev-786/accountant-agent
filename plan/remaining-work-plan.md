# AccountingOS Remaining Work Plan

**Specification baseline:** v1.3  
**Starting point:** Phases 0–7 safety foundations are implemented and tested.  
**Purpose:** finish the isolated synthetic demo, then separately gate live US
and India expansion.

This plan covers the work that remains after the provider-contract and policy
foundations. The current code uses injected clients and in-memory state so the
rules are testable. The next work replaces those seams with durable persistence,
real demo integrations, worker execution, and a usable web workflow.

## Current baseline

Already implemented:

- deployment, connection, OAuth callback, and demo/live boundary primitives;
- Xero/Plaid source contracts, normalization, cursor/pagination recovery, and
  immutable snapshot rules;
- scoped evidence and checklist evaluation;
- controlled Gmail request policy and ambiguous-send recovery;
- deterministic reconciliation, exceptions, journals, and report invariants;
- bounded AI explanation validation;
- frozen controller approvals and Xero `DRAFT` action policy;
- US/India production release gates;
- 62 backend tests and a successful Next.js production build.

Not yet complete:

- durable PostgreSQL state and migrations behind the Python domain;
- real secret-manager and provider SDK/MCP clients;
- worker DAG, leases, retries, cancellation, and SSE replay;
- integrated API and web screens;
- external Phase 0 capability evidence and end-to-end demo acceptance;
- production provider, compliance, and operational hardening.

## Delivery rules

1. Preserve the demo boundary: only synthetic US/USD data may enter the demo.
2. Keep provider tokens out of PostgreSQL, logs, browser payloads, AI prompts,
   and artifacts; store only secret-manager references.
3. Keep raw provider data append-only and application-owned normalized versions
   immutable.
4. A provider failure, stale/partial source, policy failure, or ambiguous
   external action is a visible blocked/recovery state, never a local fallback.
5. Every phase ends with automated tests plus an operator or external evidence
   artifact. Passing unit tests alone does not prove provider capability.
6. Do not add posting, payment, delete, void, period-lock, arbitrary email, or
   unrestricted MCP capabilities.

## Phase 8 — Durable persistence and workflow data model

### Outcome

The domain rules run against PostgreSQL with immutable source, package, approval,
action, evidence, and audit records. A process restart does not lose a close run
or allow a duplicate external action.

### Work items

1. Add SQLAlchemy models and Alembic migrations for:
   - `raw_xero_demo`, `raw_bank_demo`, and later market raw schemas;
   - normalized record versions, source batches, snapshots, and membership;
   - evidence items, checklist versions/evaluations, reconciliation matches,
     exceptions, journal proposals, reports, and packages;
   - workflow runs/tasks/dependencies/leases/events;
   - approvals, controller decisions, action executions, action manifests;
   - provider calls, webhook receipts, AI calls, and policy decisions.
2. Apply database constraints for organization isolation, deployment mode/data
   class, provider environment, unique provider source identity, immutable
   snapshot membership, and action idempotency keys.
3. Add append-only triggers or repository-level guards for raw records,
   normalized versions, approved packages, and action manifests.
4. Replace in-memory `CloseService`, connection registry, Plaid cursor state,
   evidence executions, and Xero action executions with repositories.
5. Add transaction boundaries for source-batch completion plus snapshot
   membership and Plaid changes plus cursor update.

### Verification

- Migration applies to a clean PostgreSQL database.
- Rollback/restart does not lose state or duplicate rows.
- Cross-organization queries return no records.
- Concurrent workers cannot claim the same task or action.
- Immutable rows reject updates after snapshot/package/action freeze.

### Exit criterion

A persisted close run can be stopped and restarted while preserving its exact
snapshot, package hash, approval, and external-action idempotency state.

## Phase 9 — Real isolated demo provider wiring

### Outcome

The injected contracts are backed by the isolated demo Xero, Plaid Sandbox,
Google Workspace, B2, and OpenAI credentials without exposing secrets.

### Work items

1. Create separate demo secret-store entries and callback URLs.
2. Wire Xero OAuth and direct Demo Company reads through the bounded adapter.
3. Wire Plaid Sandbox Link/access-token/cursor sync and webhook verification.
4. Wire Google Drive/Gmail scoped search, draft, and allowlisted send clients.
5. Wire B2 upload, signed retrieval, Object Lock, and content-addressed keys.
6. Wire OpenAI structured output using the bounded AI context and schema.
7. Add provider health checks, request/event IDs, rate-limit handling, and
   stale/partial/revoked status mapping.
8. Record provider calls and redacted outcomes in the audit ledger.

### Required external evidence

- Xero Demo Company tenant, scope, pagination, control-total, marker, and
  `DRAFT` read-back evidence.
- Plaid Sandbox cursor, added/modified/removed, pending-to-posted, webhook
  replay, and Item-error evidence.
- Google OAuth scope, folder/mailbox scope, and allowlisted test-send evidence.
- B2 Object Lock and signed retrieval evidence.
- OpenAI demo model/schema validation evidence.

### Exit criterion

The fixed synthetic scenario reads current provider data and produces complete
source/evidence batches with real provider request IDs, or blocks with the true
provider condition.

## Phase 10 — Worker DAG, webhooks, and recovery

### Outcome

Close runs execute the documented workflow DAG with durable leases, visible
progress, safe retries, cancellation, and restart recovery.

### Work items

1. Implement `close-readiness-v1` task definitions and dependency transitions.
2. Add PostgreSQL task claim with `FOR UPDATE SKIP LOCKED`, 60-second leases,
   15-second heartbeat, per-task timeout, and bounded attempts.
3. Classify retryable provider/database/network errors separately from policy,
   accounting-control, permission, and partial-data blockers.
4. Add webhook signature validation, replay protection, deduplicated receipts,
   and event-to-task dispatch for Plaid, Gmail, and provider sync notifications.
5. Add cancellation semantics before and during external actions.
6. Persist audit events and implement SSE replay from the last event cursor.
7. Add operator recovery commands for expired leases, stale sources, unknown
   Gmail/Xero outcomes, and revoked connections.

### Exit criterion

A worker restart, duplicate webhook, expired lease, cancellation, and provider
timeout produce deterministic state transitions without duplicate side effects.

## Phase 11 — API and web workflow integration

### Outcome

The controller can operate the complete demo workflow from the browser without
direct provider writes or secret exposure.

### Work items

1. Add authenticated organization and connection onboarding endpoints.
2. Add close-run creation, status, progress, snapshot, evidence, checklist,
   reconciliation, reports, exception, and package endpoints.
3. Add approval/request-change endpoints referencing frozen package hashes.
4. Keep Xero/Gmail external actions worker-only; do not add browser-callable
   provider action routes.
5. Replace the static web shell with screens for:
   - connection health and remediation;
   - synchronization progress and watermarks;
   - evidence inventory and missing-document checklist;
   - reconciliation matches and exceptions;
   - reports and journal proposals;
   - AI explanations with citations/uncertainty;
   - package review, approval, action status, and recovery.
6. Add SSE event replay, loading/error/blocked states, and accessible keyboard
   navigation.

### Exit criterion

A controller can run the fixed demo scenario end-to-end from the browser and
sees the same persisted state after refresh or reconnect.

## Phase 12 — Demo acceptance and operational readiness

### Outcome

The isolated synthetic demo is reproducible, supportable, and honest about its
provider state.

### Work items

1. Bootstrap and verify `demo-scenario-v1` against current provider IDs.
2. Capture a complete close run, evidence batch, reconciliation result,
   reports, AI explanation, frozen package, and verified Xero `DRAFT`.
3. Run failure drills: stale/partial source, wrong tenant, duplicate webhook,
   revoked token, Gmail ambiguous send, Xero timeout, tampered read-back,
   worker restart, and cancellation.
4. Add structured logs, redaction checks, dashboards, alerts, and operator
   runbooks.
5. Verify backups, restore, retention/deletion, secret rotation, B2 retention,
   and audit export.
6. Run accessibility, dependency, security, and load checks appropriate for the
   demo deployment.

### Exit criterion

The demo acceptance checklist is signed with real provider evidence; no
placeholder or local fixture is used to claim readiness.

## Phase 13 — US production pilot

### Preconditions

- Phase 12 complete.
- Separate US production account, database, secret store, callbacks, and B2
  bucket.
- Xero production, Fivetran, Plaid Production, Google, B2, and OpenAI evidence.
- Pilot organization authorization and controller sign-off.

### Work items

1. Register the US production deployment as `production`/`live`/`US`/`USD`.
2. Implement Fivetran Xero sync completion, read-only raw schema, freshness
   barrier, and direct Xero control-total verification.
3. Implement Plaid Production onboarding, refresh/webhook flow, consent,
   selected accounts, and transaction completion checks.
4. Run live Drive/Gmail policy acceptance, reports, AI, approval, and Xero
   `DRAFT` recovery tests with the pilot.
5. Review security, retention, incident response, observability, accessibility,
   and audit export before release.

### Exit criterion

US is released independently only after signed live acceptance evidence exists.

## Phase 14 — India production pilot

### Preconditions

- Phase 12 complete and US completion does not substitute for India approval.
- Setu agreement, FIU eligibility/certified partner path, Sahamati/ReBIT
  requirements, supported FIP, and approved processing/retention policy.
- Separate India production deployment, credentials, callbacks, database, and
  B2 bucket.

### Work items

1. Register the India deployment as `production`/`live`/`IN`/`INR`.
2. Implement consent creation, approval/rejection, session start, notification
   deduplication, and selected-account completeness checks.
3. Block partial, failed, expired, revoked, or out-of-range FI data.
4. Run India-specific security, retention, cross-border, audit, and pilot
   acceptance tests.

### Exit criterion

India is released independently only after applicable legal, provider, and live
pilot evidence is signed off.

## Phase 15 — Cross-market hardening and release governance

### Outcome

The product can operate multiple deployments without data or control leakage.

### Work items

- managed PostgreSQL backups, restore drills, migrations, and disaster recovery;
- B2 lifecycle/Object Lock verification and artifact integrity checks;
- secret rotation, token expiry, consent revocation, and provider outage drills;
- security review, dependency scanning, penetration testing, and WCAG AA audit;
- load/performance testing for worker concurrency, SSE replay, and report builds;
- incident response, audit retention/deletion, operator access review, and
  deployment-specific monitoring;
- release checklist that blocks market promotion until all gates are signed.

### Exit criterion

Each market has an independently reproducible release record, and no deployment
can read credentials, artifacts, or financial records from another market.

## Critical path and sequencing

```text
Phase 8 Persistence
      ↓
Phase 9 Real demo providers
      ↓
Phase 10 Worker/recovery
      ↓
Phase 11 API/web integration
      ↓
Phase 12 Demo acceptance
      ├──────────────→ Phase 13 US pilot
      └──────────────→ Phase 14 India pilot (with separate compliance gates)
                              ↓
                    Phase 15 hardening/governance
```

Phase 9 can begin provider account setup in parallel with Phase 8, but no
end-to-end acceptance should be claimed until persistence and recovery are
available. Phases 13 and 14 must remain separate releases.

## Definition of done

The project is ready for a demo release only when Phase 12 is signed. It is
ready for a live US or India release only when its own phase and all external
provider/compliance gates are signed. Code completion, mocked clients, or a
passing unit-test suite cannot substitute for those gates.
