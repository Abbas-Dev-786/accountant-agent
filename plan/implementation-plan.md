# AccountingOS Phase-by-Phase Implementation Plan

**Specification:** v1.3

**Product boundary:** An isolated US synthetic-data demo first. It prepares a
controller-reviewable close package and may create only controller-approved
Xero manual journals in `DRAFT` status. It never posts, pays, deletes, voids,
or locks an accounting period.

## Delivery map

```text
Phase 0  Prove external demo capabilities
    ↓
Phase 1  Isolated identity and connection platform
    ↓
Phase 2  Provider ingestion and immutable snapshots
    ↓
Phase 3  Evidence collection and controlled email
    ↓
Phase 4  Deterministic close controls and reports
    ↓
Phase 5  Evidence-grounded AI explanations
    ↓
Phase 6  Controller approval and Xero DRAFT creation
    ↓
Phase 7  Separately gated US and India live expansion
```

## Shared completion standard

Every phase must include:

- implementation tests for happy path, invalid input, empty input, and provider
  failure;
- structured audit events and visible user-facing status for new work;
- organization isolation checks;
- an updated acceptance test or runbook;
- a concise demo of the new capability using its actual environment.

## Phase 0 — Demo Capability Spikes

### Outcome

Remove every uncertain assumption about the synthetic demo providers before
application code relies on them.

### Prerequisites

- Dedicated demo domain and deployment account.
- A named controller who can authorize testing in the designated Xero Demo
  Company.
- Separate demo secret store and Backblaze B2 bucket.

### Work items

1. Create isolated demo credentials for Xero, Plaid Sandbox, Google test
   Workspace, B2, OpenAI, and managed OIDC.
2. Register the Xero demo application with the exact granular scope profile in
   `docs/live_integrations.md`; verify tenant identity, account codes,
   pagination, manual-journal `DRAFT` behavior, narration-marker lookup, and
   read-back.
3. Create the prepared Xero baseline and record its fingerprint, expected record
   IDs, reset operator, reset steps, and reset verification command. Do not
   describe reset as an API capability.
4. Configure Plaid Sandbox with a custom or dynamic Transactions user. Prove
   cursor sync, added/modified/removed records, pending-to-posted behavior,
   test webhook replay, Item failure, and supported synthetic-date limits.
5. Configure the test Google Workspace folders, mailbox, test recipients, and
   Gmail send policy. Verify the OAuth scopes are restricted to those resources.
6. Prove B2 Object Lock, signed package retrieval, and the approved retention
   settings in the demo bucket.
7. Write `demo-scenario-v1` with the fixed period, baseline fingerprint, Plaid
   transaction definitions, Workspace evidence, expected exception, and one
   permitted journal proposal.

### Verification evidence

- A provider capability report containing request IDs, scope grants, sample
  read-back IDs, rate-limit observations, and reset instructions.
- A repeatable bootstrap record that seeds Plaid/Workspace and verifies the Xero
  baseline without touching a production credential or tenant.

### Exit criterion

The scenario can be bootstrapped and read coherently from all demo providers;
no required provider behavior remains an assumption.

### Do not do

- Do not implement Fivetran, Plaid Production, Setu, or customer providers.
- Do not use a local fixture as a substitute for an unavailable demo provider.

## Phase 1 — Demo Identity, Isolation, and Connections

### Outcome

A controller can sign in, create a demo organization, connect only approved demo
providers, and see real connection health before starting a close run.

### Dependencies

Phase 0 credentials, scope evidence, and callback URLs.

### Work items

1. Replace in-memory run storage with Supabase Postgres plus timestamped
   `supabase/migrations/` (Supabase CLI) for organizations, organization users,
   connections, configurations, audit events, and deployment configuration.
2. Implement managed OIDC authorization-code + PKCE login. Map the configured
   issuer/subject into `organization_users`; do not create local passwords.
3. Add a server-owned deployment configuration. Persist deployment mode and data
   class on runs only; reject a browser-supplied mode, live credential, production
   tenant, Item, callback, artifact, or data class in the demo stack.
4. Implement OAuth callback state, PKCE, redirect-URI, and tenant checks.
   Registration is multi-tenant by default (a connection per granted Xero
   tenant); the optional `ACCOUNTINGOS_XERO_TENANT_ALLOWLIST` pins a deployment
   to specific tenants, and the demo stack sets it to the designated Demo
   Company so a production tenant is rejected. Hold OAuth transaction state in
   the durable `workflow.oauth_sessions` store so a restart or second worker
   does not drop an in-flight authorization. Store only secret-manager
   references in PostgreSQL.
5. Build connection settings and health screens for Xero, Plaid, Drive, Gmail,
   B2, and OpenAI. Show provider environment, scopes, latest verification,
   expiry, and remediation.
6. Implement webhook signature verification, event receipt deduplication, and
   audit events before dispatching any provider work.

### Verification evidence

- Integration tests for an unauthorized user, cross-organization read, callback
  replay, state mismatch, expired credential, and demo/production mismatch.
- A controller signs in and sees only the configured synthetic demo environment.

### Exit criterion

The controller can connect the Xero Demo Company, Plaid Sandbox, and test
Workspace and see verified health without starting a close run.

### Do not do

- Do not accept customer credentials in the demo deployment.
- Do not expose secrets, raw provider payloads, or signed B2 URLs in the UI/logs.

## Phase 2 — Ingestion, Normalization, and Snapshots

### Outcome

A close run receives fresh provider data through real demo adapters and produces
a reproducible immutable source snapshot or a visible blocker.

### Dependencies

Phase 1 connection records, secret references, webhooks, and database.

### Work items

1. Implement `XeroDirectDemoAdapter` with bounded paginated reads into
   `raw_xero_demo`; record request IDs, watermarks, tenant identity, and source
   controls.
2. Implement Plaid cursor ingestion into `raw_bank_demo`. Apply added, modified,
   and removed records with the new cursor in one transaction; restart safely if
   pagination mutates.
3. Implement the shared `SourceBatch` and normalized immutable record-version
   model. Every version must include provider identity, provider record ID,
   payload hash, source batch, observed timestamp, currency, and accounting date.
4. Implement source-batch completion and snapshot membership in one transaction.
   Snapshot records must reference immutable version IDs and reject duplicate
   provider source identities.
5. Add the fixed workflow executor for preflight, synchronization, Xero control
   verification, snapshot creation, task leases, retries, and SSE audit replay.
6. Build the run UI: provider progress, watermarks, raw counts, readiness state,
   snapshot ID, and remediation for stale/partial/revoked sources.

### Verification evidence

- Tests for duplicate/out-of-order webhook, Plaid removal, stale source, partial
  data, wrong tenant, worker restart, cancelled task, and environment mismatch.
- A run captures a reproducible synthetic snapshot from current provider reads.

### Exit criterion

A close run creates an immutable synthetic snapshot with provider watermarks, or
blocks with the true provider condition. No cached/local data is substituted.

### Do not do

- Do not call Fivetran in the demo.
- Do not compute accounting reports from mutable raw rows.

### Current implementation checkpoint

`backend/app/providers.py`, `backend/app/normalization.py`, and
`backend/app/ingestion.py` now provide the bounded Xero demo and Plaid Sandbox
contracts, deterministic normalized versions, cursor-safe recovery, and an
atomic worker-facing snapshot coordinator. See
[`phase-2-operator-runbook.md`](phase-2-operator-runbook.md) for the evidence
command and the deliberate persistence boundary.

## Phase 3 — Evidence and Policy-Controlled Email

### Outcome

The run can inventory required documents, preserve verified evidence, and send a
single safe missing-document request only when policy permits it.

### Dependencies

Phase 2 snapshots and the Phase 0 Google/B2 capability proof.

### Work items

1. Implement scoped Drive and Gmail evidence adapters using configured folders,
   labels, date range, mailbox, and contacts only.
2. Persist evidence metadata, checksum, source ID, trust/validation status, and
   permitted B2 copy. Scan/parse an attachment before model access.
3. Implement versioned close checklists and deterministic missing-document
   evaluation against snapshot evidence.
4. Implement recipient/domain allowlists, template versioning, attachment rules,
   rate limits, and the policy decision audit record.
5. Implement Gmail draft-first send with an action marker. On recovery, search
   Sent for the marker; if absence cannot be proven, set `outcome_unknown` and
   stop automatic retry.
6. Build evidence, checklist, missing-document, and audit-timeline screens.

### Verification evidence

- Tests for out-of-scope Drive/Gmail search, disallowed recipient, stale
  template, duplicate send, crash after send, ambiguous Sent search, and a
  missing attachment.
- A test-workspace request sends once, with a real Gmail message/thread ID in
  the timeline.

### Exit criterion

Evidence is traceable to the snapshot and an allowlisted test request has one
verified send effect or a visible unresolved outcome.

### Do not do

- Do not send outside the configured test allowlist.
- Do not pass an entire mailbox or unscoped attachments to a model.

## Phase 4 — Deterministic Close Controls and Reports

### Outcome

The system prepares a controller-reviewable close package using deterministic
document, reconciliation, journal, and reporting controls.

### Dependencies

Phase 2 snapshot facts and Phase 3 evidence/checklist facts.

### Work items

1. Implement organization-versioned reconciliation rules for exact matches,
   date windows, one-to-many/many-to-one groups, fees, processor deposits,
   duplicate candidates, and pending-versus-posted transitions.
2. Make every source transaction `matched`, `excluded_by_policy`, or an open
   exception. A tolerance must never erase an exception.
3. Implement exception records with deterministic control result, source
   evidence, amount/currency, status, and required remediation.
4. Implement journal-proposal construction with valid current Xero account
   codes, evidence on every line, selected-period dates, and debit/credit checks.
5. Compute unadjusted and pro-forma adjusted trial balance, P&L, balance sheet,
   cash reconciliation, exception schedule, and change log from snapshot facts.
6. Enforce trial-balance, accounting-equation, source-control-total, and cash
   reconciliation invariants. Label all local/Xero-draft adjustments as pro
   forma.
7. Build reconciliation, exception, report, and package-review screens.

### Verification evidence

- Unit/property tests for balance, reconciliation grouping, duplicate candidate,
  pending transaction, unbalanced proposal, invalid account code, and every
  report invariant.
- The fixed demo scenario either ties fully or represents each difference as an
  explicit exception with evidence.

### Exit criterion

The package reports are reproducible from the immutable snapshot; every proposed
journal balances and every unexplained amount is an exception.

### Do not do

- Do not let AI choose a match, repair source amounts, or override a failed
  accounting control.

## Phase 5 — Grounded AI Explanations

### Outcome

AI adds concise explanation and executive-summary value without gaining
accounting-control or provider-write authority.

### Dependencies

Phase 4 fact set, exception records, evidence references, and OpenAI demo
credentials proven in Phase 0.

### Work items

1. Create bounded structured-output schemas for exception cause,
   recommendation, evidence IDs, uncertainties, and confidence label.
2. Extract the smallest relevant fact set. Treat provider text, email, and
   attachments as untrusted data; quote them and prohibit embedded instructions.
3. Validate every citation against the current snapshot. Deterministically reject
   any model-supplied amount, account, date, or conclusion absent from facts.
4. Persist model, prompt/schema version, evidence IDs, hashes, latency/token
   metadata, validation outcome, and concise rationale. Never persist
   chain-of-thought.
5. Retry invalid structured output once, then fail closed without inventing a
   fallback explanation.
6. Build exception explanation and executive-summary review UI that shows cited
   evidence and uncertainty separately from deterministic checks.

### Verification evidence

- Tests containing prompt injection, unknown evidence ID, unsupported amount,
  malformed schema, model timeout, and a valid cited response.
- A valid exception explanation cites only the selected synthetic snapshot.

### Exit criterion

Unsupported AI output cannot enter controller review; valid output is fully
traceable to snapshot evidence.

### Do not do

- Do not give the model OAuth tokens, arbitrary SQL, arbitrary web access, or
  MCP tool discovery.
- Do not use confidence as an accounting verification signal.

## Phase 6 — Approval and Xero `DRAFT` Creation

### Outcome

The controller can approve an exact frozen package and AccountingOS can create
the approved Xero demo manual journal once, in `DRAFT` status, with verified
read-back.

### Dependencies

Phases 2–5 and the Phase 0 Xero marker/read-back capability proof.

### Work items

1. Persist immutable package versions, proposal hashes, controller decisions,
   comments, approval timestamp, and approved snapshot hash.
2. Require the configured controller and a matching frozen package/proposal hash
   before the worker can prepare an external action.
3. Implement the owned Xero policy gateway and expose only the documented
   bounded read tools plus `xero.create_draft_manual_journal`. Do not create a
   browser-callable Xero action endpoint.
4. Use the server-generated `AOSMJv1/<action_execution_uuid>/<proposal_hash>`
   narration marker, advisory lock, persisted request hash, exact narration
   lookup, `DRAFT` request, and line-by-line read-back.
5. On a timeout, query for the exact marker. If Xero state cannot prove absence
   or identity, mark `outcome_unknown`; do not retry automatically. Mark altered
   or mismatched draft responses as `action_failed` for controller review.
6. Create a separate immutable action manifest referencing the frozen review
   package. Store it in the locked B2 location without modifying the approved
   artifact.
7. Build package review, approval/request-change, action status, read-back, and
   recovery UI.

### Verification evidence

- Tests for duplicate worker claim, crash after create/before persistence,
  timeout, unknown marker search, tampered draft, read-back mismatch,
  cancellation during action, and no-proposal approval.
- A controller creates one real `DRAFT` journal in the Xero Demo Company and the
  package/action manifest remain immutable and separately addressable.

### Exit criterion

One approved proposal produces exactly one verified Xero Demo Company `DRAFT`
journal, or a visible state that cannot create a duplicate. No posting tool
exists.

### Do not do

- Do not add a Xero post/update-status/delete/void tool.
- Do not mutate the package after approval to append action results.

## Phase 7 — Live Product Expansion and Hardening

### Outcome

Deliver real US and India pilot workflows only after each market's provider,
security, retention, and compliance gates are externally satisfied.

### Dependencies

All demo acceptance criteria, plus the production gates in `docs/PRD.md` and
`docs/live_integrations.md`.

### Work streams

#### 7A. US live expansion

1. Create the separate US deployment, database, B2 bucket, callbacks, and
   secret store. Enforce `production`/`live` deployment configuration.
2. Implement Fivetran Xero ingestion into application-read-only `raw_xero`,
   incremental-sync completion, normalization, direct Xero control-total
   verification, and stale-sync blocking.
3. Implement Plaid Production onboarding and transactions refresh/webhook flow
   with a supported consenting pilot institution.
4. Complete real Drive/Gmail evidence, policy-compliant email, Xero `DRAFT`
   journal, report, recovery, accessibility, security, and load acceptance tests.

#### 7B. India live expansion

1. Do not start production data access until the Setu agreement, FIU/Sahamati
   path, approved data-processing policy, and supported pilot FIP are confirmed.
2. Create the separate India deployment and retention/cross-border controls.
3. Implement Setu consent, FI data session, notification deduplication, complete
   selected-account delivery requirement, and partial/expired/revoked blocker.
4. Run the full live acceptance suite with a consenting pilot organization.

### Cross-market hardening

- Add managed PostgreSQL backups/recovery tests, B2 lifecycle and Object Lock
  tests, secret rotation, audit retention/deletion, provider outage runbooks,
  observability dashboards/alerts, and WCAG AA verification.
- Test cross-organization access, SSE replay, provider rate limits, service
  restart, token expiry, consent revocation, and no-test-data enforcement.

### Verification evidence

- Separate US and India deployment inventories showing distinct credentials,
  callbacks, databases, artifact buckets, retention settings, and provider
  tenants.
- Signed-off live acceptance evidence for each market: pilot authorization,
  provider completion/watermarks, controller-approved email and `DRAFT` journal,
  recovery tests, audit export, and all applicable data-processing gates.

### Exit criterion

Each market is released independently only after its real pilot passes the live
acceptance criteria. An India release cannot be implied by US completion.

### Do not do

- Do not call a market live based on a sandbox/demo success.
- Do not copy financial records between demo, US, or India stacks.

## Release checklist

Before declaring the demo milestone done, confirm:

- [ ] Phase 0 capability evidence is current and attached.
- [ ] One isolated US scenario bootstraps Plaid/Workspace and verifies the Xero
  baseline.
- [ ] All selected sources synchronize, normalize, and freeze into one snapshot.
- [ ] Deterministic controls pass or create explicit exceptions.
- [ ] AI output is cited and validated, or visibly fails closed.
- [ ] Controller approval freezes the exact package.
- [ ] One Xero `DRAFT` journal is created, read back, and recorded separately.
- [ ] Duplicate webhook, ambiguous action, cancellation, and worker restart
  paths are demonstrated.
- [ ] No demo pathway can access production credentials, data, callbacks, or
  artifacts.

## Implementation checkpoints

The remaining phase foundations are now present in the backend:

- Phase 3: `backend/app/evidence.py` — scoped evidence, checklists, and Gmail
  policy/idempotency.
- Phase 4: `backend/app/reconciliation.py` and `backend/app/reports.py` —
  deterministic matches, exceptions, journals, and report invariants.
- Phase 5: `backend/app/ai.py` — bounded structured explanations and fail-closed
  citation validation.
- Phase 6: `backend/app/actions.py` — frozen approvals and Xero DRAFT gateway.
- Phase 7: `backend/app/expansion.py` — separate US/India release gates.

The external provider clients, PostgreSQL persistence, B2 Object Lock, and
market/compliance evidence remain explicit release prerequisites; the code uses
injected clients so these boundaries are testable without pretending that
external sign-off has occurred.

## Definition of progress

A phase is **done** only when its exit criterion and verification evidence are
complete. A coded screen, mocked integration, or passing happy-path test is
progress, but is not phase completion.
