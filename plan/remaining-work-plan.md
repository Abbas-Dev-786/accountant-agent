# AccountingOS Remaining Work Plan

**Specification baseline:** v1.3  
**Starting point:** Phases 0–7 safety foundations, Phase 8 private persistence,
Phase 9 server-side read adapters, and the Phase 10 durable task/event layer
are implemented and tested. External/database evidence is still required.
**Purpose:** deliver the US production product. Synthetic scenarios are retained
only as isolated test fixtures. India is explicitly deferred until a separate
scope decision.

This plan covers the work that remains after the provider-contract and policy
foundations. The current code includes transport-injected provider seams,
Supabase migrations, server-only Groq/Supabase boundaries, Supabase Auth API
enforcement, and a working browser onboarding/close-run flow. The next work
applies the migration to the chosen production project, provisions the US
production accounts, and configures the organization-specific accounting mapping
and action/artifact integrations.

## Current baseline

Already implemented:

- deployment, connection, OAuth callback, and production/fixture boundary
  primitives;
- multi-tenant Xero connection registration with an optional tenant allowlist,
  a durable Postgres-backed OAuth session store, and durable connection-health
  records;
- Supabase Auth magic-link browser sign-in, server-side token verification,
  organization membership checks, and idempotent close-run creation;
- Xero/Plaid source contracts, normalization, cursor/pagination recovery, and
  immutable snapshot rules;
- scoped evidence and checklist evaluation;
- controlled Gmail request policy and ambiguous-send recovery;
- deterministic reconciliation, exceptions, journals, and report invariants;
- bounded AI explanation validation;
- frozen controller approvals and Xero `DRAFT` action policy;
- US production release gates, with India retained only as deferred boundary
  code;
- 135 backend tests and a successful Next.js production build.

Not yet complete:

- applying and validating the Supabase migrations against the selected remote
  project;
- managed secret-manager provisioning and real provider account evidence;
- replacing the current demo-default bootstrap and source adapters with the
  production-only organization onboarding and source configuration;
- the organization-specific selected bank accounts, ledger source, tolerances,
  and account mappings required to turn provider records into reconciliation
  facts safely;
- durable reconciliation/report/action/artifact execution on the frozen
  mapping, including B2 Object Lock and Xero draft read-back;
- SSE streaming/replay and the detailed browser screens for source evidence,
  reconciliation, reports, and action recovery;
- production provider, compliance, and operational acceptance evidence.

## Delivery rules

1. Preserve the production boundary: only live US/USD data may enter the active
   deployment; any synthetic data stays in a physically separate fixture stack.
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

## Technology decisions for the next build

### Supabase

- Supabase Postgres is the authoritative workflow, normalized-data, audit, and
  action-idempotency database for the US deployment.
- Migrations live under `supabase/migrations/` and are created through the
  Supabase CLI; do not hand-name migration files.
- The FastAPI backend uses a server-side Postgres connection for transactional
  work. The browser never receives a Supabase secret/service-role key.
- Keep financial tables in private schemas (`workflow`, `raw_xero`,
  `raw_bank_us`, `normalized`, and `audit`) rather than exposing them through
  the Data API. Legacy `*_demo` schemas are fixture-only. If any table is
  exposed later, enable RLS and add explicit grants/policies for the actual
  organization-membership model.
- Supabase Auth is the selected controller identity provider. Supabase
  Storage/Realtime are optional and must not bypass the FastAPI policy boundary.

Supabase's current platform defaults require deliberate Data API exposure and
RLS/grants for newly created tables, so the plan defaults to private schemas and
server-side access. See the [Supabase changelog](https://supabase.com/changelog)
and [Data API security guidance](https://supabase.com/docs/guides/api/securing-your-api.md).

### Groq

- Implement a `GroqExplanationModel` behind the existing `ExplanationModel`
  protocol. This keeps deterministic controls independent of the model vendor.
- Store `GROQ_API_KEY` only in the server-side secret store and configure the
  model with `GROQ_MODEL`; never put it in a `NEXT_PUBLIC_*` variable.
- Start with a Groq model that supports strict structured output, preferably
  `openai/gpt-oss-20b`, and keep the model ID configurable because hosted model
  availability changes.
- Use bounded prompts and JSON Schema output. The application still performs
  citation, amount, date, account, and prompt-injection validation after Groq
  returns; model confidence never becomes an accounting control.
- Treat the free tier as rate-limited capacity: record 429s and usage metadata,
  retry only within the existing one-retry policy, and fail closed when limits
  are reached. Do not add browser search, code execution, arbitrary tools, or
  provider writes to an accounting explanation request.

Groq documents OpenAI-compatible endpoints, strict structured-output support
for selected models, and organization-level rate limits; these are operational
constraints, not guarantees of unlimited free capacity. See the [Groq
compatibility docs](https://console.groq.com/docs/openai), [structured outputs
docs](https://console.groq.com/docs/structured-outputs), and [rate-limit
docs](https://console.groq.com/docs/rate-limits).

## Phase 8 — Supabase Postgres persistence and workflow data model

### Outcome

The domain rules run against PostgreSQL with immutable source, package, approval,
action, evidence, and audit records. A process restart does not lose a close run
or allow a duplicate external action.

### Work items

1. Initialize the Supabase project and create migrations with the Supabase CLI.
2. Add SQLAlchemy models/repositories and Supabase migrations for:
   - `raw_xero`, `raw_bank_us`, and fixture-only raw schemas;
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
5. Replace in-memory `CloseService`, connection registry, Plaid cursor state,
   evidence executions, and Xero action executions with repositories.
6. Add transaction boundaries for source-batch completion plus snapshot
   membership and Plaid changes plus cursor update.
7. Add a server-only Supabase connection configuration, health check, migration
   check, and local test project or disposable database path.

### Verification

- Supabase migrations apply to a clean project and local verification database.
- RLS/security review confirms private schemas are not exposed through the Data
  API; any exposed table has explicit grants and organization policies.
- Rollback/restart does not lose state or duplicate rows.
- Cross-organization queries return no records.
- Concurrent workers cannot claim the same task or action.
- Immutable rows reject updates after snapshot/package/action freeze.

### Exit criterion

A persisted close run can be stopped and restarted while preserving its exact
snapshot, package hash, approval, and external-action idempotency state.

## Phase 9 — US production provider wiring and Groq

### Outcome

The server-only contracts connect the approved US production Xero source, Plaid
Production, Google Workspace, B2, and Groq. Credentials, organization mapping,
and external evidence are required before this phase can be accepted.

### Work items

1. Create US production secret-store entries and callback URLs, including
   `GROQ_API_KEY` and Xero client-secret/refresh-token references (external
   setup pending).
2. Implement Xero standard OAuth Auth Code + PKCE authorization, token
   exchange, refresh, and `/connections` tenant discovery (`xero_oauth.py`
   provides the server-side exchange/rotation boundary and `list_tenants`;
   the callback registers a connection for every granted tenant. Live
   secret-store wiring remains). Registration is multi-tenant by default; the
   optional `ACCOUNTINGOS_XERO_TENANT_ALLOWLIST` pins an organization to its
   approved production tenant. OAuth transaction state
   is held in the durable `workflow.oauth_sessions` store when a database is
   configured, so a restart or second worker does not drop an in-flight
   authorization. Use the current granular scope profile in
   `docs/live_integrations.md`; this includes settings, contacts, invoices,
   payments, bank transactions, the narrowly bounded manual-journal draft path,
   and the required reports. Never put the client secret or refresh token in
   browser variables.
3. Wire the approved production Xero source through the bounded source contract,
   including tenant identity, account list, pagination, control totals, and
   current source watermarks.
4. Wire Plaid Production Link/access-token/cursor sync and webhook verification,
   including selected-account completeness and pending-to-posted behavior.
5. Wire Google Drive/Gmail scoped search, draft, and allowlisted send clients
   (read clients implemented; draft/send worker wiring pending).
6. Wire B2 upload, signed retrieval, Object Lock, and content-addressed keys.
7. Wire Groq structured output using the bounded AI context and schema; record
   model ID, latency, token metadata, and 429/failure outcomes.
8. Add provider health checks, request/event IDs, rate-limit handling, and
   stale/partial/revoked status mapping.
9. Record provider calls and redacted outcomes in the audit ledger.

### Required external evidence

- Xero production tenant, scope, pagination, control-total, marker, and
  `DRAFT` read-back evidence.
- Plaid Production cursor, added/modified/removed, pending-to-posted, webhook
  replay, and Item-error evidence.
- Google OAuth scope, folder/mailbox scope, and allowlisted test-send evidence.
- B2 Object Lock and signed retrieval evidence.
- Groq model/schema validation evidence and a free-tier rate-limit test.

### Exit criterion

A real US organization reads current provider data and produces complete
source/evidence batches with real provider request IDs, or blocks with the true
provider condition. Transport-injected tests prove the code boundary but are not
sufficient for exit.

## Phase 10 — Worker DAG, webhooks, and recovery

### Outcome

Close runs execute the documented workflow DAG with durable leases, visible
progress, safe retries, cancellation, and restart recovery.

The deterministic task state layer, durable task/event persistence, and running
worker entrypoint are implemented. Production provider/action recovery remains
open.

### Work items

1. Implement `close-readiness-v1` task definitions and dependency transitions
   (state layer implemented in `backend/app/worker.py`; task catalog still
   needs wiring).
2. Add PostgreSQL task claim with `FOR UPDATE SKIP LOCKED`, 60-second leases,
   15-second heartbeat, per-task timeout, and bounded attempts (repository
   claim and in-memory state are implemented; durable worker wiring remains).
3. Classify retryable provider/database/network errors separately from policy,
   accounting-control, permission, and partial-data blockers (implemented in
   the state layer; provider error mapping remains).
4. Add webhook signature validation, replay protection, deduplicated receipts,
   and event-to-task dispatch for Plaid, Gmail, and provider sync notifications
   (HMAC/replay guard implemented; provider dispatch remains).
5. Add cancellation semantics before and during external actions (state
   transition implemented; action gateway integration remains).
6. Persist audit events and implement SSE replay from the last event cursor
   (in-memory replay implemented; API/Supabase persistence remains).
7. Add operator recovery commands for expired leases, stale sources, unknown
   Gmail/Xero outcomes, and revoked connections.

### Exit criterion

A worker restart, duplicate webhook, expired lease, cancellation, and provider
timeout produce deterministic state transitions without duplicate side effects.

## Phase 11 — API and web workflow integration

### Outcome

direct provider writes or secret exposure.
The controller can operate the complete US production workflow from the browser
without direct provider writes or secret exposure.
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

A controller can run a configured US production close end-to-end from the
browser and sees the same persisted state after refresh or reconnect.

## Phase 12 — US production acceptance and operational readiness

### Outcome

The US production product is reproducible, supportable, and honest about its
provider state.

### Work items

1. Onboard and verify a real US organization, its Xero tenant, selected Plaid
   Production account(s), Workspace scope, B2 bucket, and Groq configuration.
2. Capture a complete production close run, evidence batch, reconciliation result,
   reports, AI explanation, frozen package, and verified Xero `DRAFT`.
3. Run failure drills: stale/partial source, wrong tenant, duplicate webhook,
   revoked token, Gmail ambiguous send, Xero timeout, tampered read-back,
   worker restart, and cancellation.
4. Add structured logs, redaction checks, dashboards, alerts, and operator
   runbooks.
5. Verify backups, restore, retention/deletion, secret rotation, B2 retention,
   and audit export.
6. Run accessibility, dependency, security, and load checks appropriate for the
   production deployment.

### Exit criterion

The US production acceptance checklist is signed with real provider evidence;
no placeholder, sandbox, or local fixture is used to claim readiness.

## Phase 13 — US production launch readiness

### Preconditions

- Phase 12 complete.
- Separate US production account, database, secret store, callbacks, and B2
  bucket.
- Xero production source, Plaid Production, Google, B2, and Groq evidence.
- Pilot organization authorization and controller sign-off.

### Work items

1. Register the US production deployment as `production`/`live`/`US`/`USD`.
2. Complete the approved Xero source's read-only raw schema, freshness barrier,
   and direct Xero control-total verification.
3. Implement Plaid Production onboarding, refresh/webhook flow, consent,
   selected accounts, and transaction completion checks.
4. Run live Drive/Gmail policy acceptance, reports, AI, approval, and Xero
   `DRAFT` recovery tests with the pilot.
5. Review security, retention, incident response, observability, accessibility,
   and audit export before release.

### Exit criterion

US is released independently only after signed live acceptance evidence exists.

## Phase 14 — India expansion (deferred)

India is not part of the current delivery scope. Keep the existing India gate
code and documentation as a future boundary, but do not provision Setu,
India-specific credentials, or India data in this release.

### Future preconditions

- Phase 12 complete and US completion does not substitute for India approval.
- Setu agreement, FIU eligibility/certified partner path, Sahamati/ReBIT
  requirements, supported FIP, and approved processing/retention policy.
- Separate India production deployment, credentials, callbacks, database, and
  B2 bucket.

### Future work items

1. Register the India deployment as `production`/`live`/`IN`/`INR`.
2. Implement consent creation, approval/rejection, session start, notification
   deduplication, and selected-account completeness checks.
3. Block partial, failed, expired, revoked, or out-of-range FI data.
4. Run India-specific security, retention, cross-border, audit, and pilot
   acceptance tests.

### Future exit criterion

India can be reconsidered only after a new scope approval and applicable legal,
provider, and live pilot evidence are signed off.

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
Phase 9 US production providers
      ↓
Phase 10 Worker/recovery
      ↓
Phase 11 API/web integration
      ↓
Phase 12 US production acceptance
      ↓
Phase 13 US launch readiness
      ↓
Phase 15 US hardening/governance
```

Phase 9 can begin US production provider account setup in parallel with Phase 8,
but no end-to-end acceptance should be claimed until persistence and recovery
are available. India remains deferred and must not receive US data or credentials.

## Definition of done

The project is ready for a US production release only when Phase 13 and all
external provider gates are signed. India remains out of the active release
scope. Code completion, mocked clients, or a passing unit-test suite cannot
substitute for those gates.
