# AccountingOS Demo MVP and Live Product Technical Design

**Version:** 1.2  
**Status:** Approved for demo implementation; live expansion remains gated  
**Decision date:** 2026-07-18

This document implements `PRD.md` and `demo_architecture.md`. Provider onboarding
details and production release gates are defined in `live_integrations.md`.

## 1. Architecture Decisions

1. Demo and production are separate deployments with immutable environment
   configuration, credentials, databases, callbacks, and artifacts.
2. Demo close runs use synthetic data from Plaid Sandbox, a Xero Demo Company,
   and a Google test Workspace. Production close runs use authenticated live
   provider data.
3. Xero is the only MVP accounting system. QuickBooks is not implemented.
4. `XeroDirectDemoAdapter` reads the Demo Company for the demo. The future
   `FivetranXeroAdapter` implements the same source contract for production.
5. Our owned Xero MCP server wraps the official Xero API for source checks and
   controller-approved `DRAFT` manual-journal creation.
6. Plaid Sandbox is the demo bank adapter. Plaid Production and Setu Account
   Aggregator are later live adapters.
7. Google Drive and Gmail use owned OAuth-backed MCP tools against a test
   Workspace in demo mode and production Workspace in live mode.
8. PostgreSQL is authoritative for workflow state, normalized data, snapshots,
   approvals, action idempotency, and audit events. Providers remain
   authoritative for their source records.
9. AI can explain and summarize cited evidence. It cannot reconcile amounts,
   approve actions, select new permissions, post journals, or move money.
10. Demo and later market deployments share code but use separate databases,
    secrets, B2 buckets, callbacks, and provider environments.
11. Controller authentication uses a managed OpenID Connect provider with the
    authorization-code flow and PKCE. AccountingOS stores organization
    membership, not local passwords; the concrete provider is selected in Phase 0.

## 2. System Context

```text
                         Controller browser
                                  |
                             HTTPS + SSE
                                  |
                                  v
                            Next.js web app
                                  |
                                  v
                         FastAPI application API
                                  |
                 +----------------+----------------+
                 |                |                |
                 v                v                v
          PostgreSQL         Workflow worker    Webhook receiver
                 |                |                |
       +---------+---------+      |      +---------+---------+
       |                   |      |      |                   |
       v                   v      v      v                   v
 Xero demo/live       Normalized data   Plaid Sandbox/Prod  Setu AA later
 adapter/Fivetran             |
       ^                       v
 Xero source              Policy/MCP gateway
                                  |
                 +----------------+----------------+
                 |                |                |
                 v                v                v
             Xero API       Google APIs        OpenAI API
                 |
                 v
          Draft journals only

Package artifacts --------------------------------------> Backblaze B2
```

The MVP is a modular monolith with separately runnable API, worker, and webhook
processes. MCP servers are owned modules deployed behind the policy gateway, not
untrusted public servers. The demo deployment is a separate stack, not a runtime
mode switch inside production.

## 3. Technology Stack

### Web

- Next.js, React, and TypeScript.
- Tailwind CSS and shadcn/ui.
- Native `EventSource` for persisted workflow events.
- Provider OAuth/onboarding redirects initiated from the connection settings UI.

### Backend

- Python 3.12.
- FastAPI, Pydantic v2, SQLAlchemy 2, and Alembic.
- PostgreSQL 16.
- OpenAI SDK with structured outputs.
- Provider clients for direct Xero demo reads, future Fivetran ingestion, Plaid,
  Setu, Google, and B2.
- MCP SDK for owned, schema-defined financial tools.

### Infrastructure

- Containerized `api`, `worker`, and `webhooks` processes from one backend image.
- Managed PostgreSQL for each market deployment.
- Managed secret store; provider tokens are never stored as plaintext database
  values.
- B2 bucket per deployment for package artifacts and permitted evidence copies.
- HTTPS callback endpoints with provider signature validation and replay defense.

## 4. PostgreSQL Schema Boundaries

Separate schemas prevent ingestion and application code from overwriting one
another:

```text
raw_xero        Fivetran-owned production Xero tables; application read-only
raw_xero_demo   Direct Xero Demo Company payloads; adapter-owned append-only
raw_bank_us     Plaid payload landing tables
raw_bank_demo   Plaid Sandbox payload landing tables for the isolated demo
raw_bank_in     Setu AA payload landing tables
normalized      Canonical accounting, bank, document, and identity records
workflow        Runs, tasks, dependencies, approvals, and packages
audit           Append-only events, provider calls, AI calls, and action ledger
```

Only Fivetran credentials may write `raw_xero`. The demo Xero adapter may write
`raw_xero_demo`. Provider webhook/ingestion roles write their regional raw-bank
schema. Normalization jobs read either raw source and write versioned normalized
records through the shared `SourceBatch` contract.

## 5. Organization and Connection Model

### `organizations`

- `id`, `name`, `market`: `US` or `IN`
- `deployment_mode`: `demo` or `production`
- `data_class`: `synthetic` or `live`
- `functional_currency`: `USD` or `INR`
- `accounting_timezone`
- `status`, `created_at`

### `organization_users`

- `organization_id`, `user_id`, `identity_issuer`, `identity_subject`, `role`
- MVP role is `controller`; least-privilege roles may be added later

### `connections`

- `id`, `organization_id`, `provider`
- `provider_tenant_id` or provider account identifier
- `credential_secret_ref`
- `status`: `connecting`, `healthy`, `delayed`, `partial`, `expired`, `revoked`,
  `failed`, or `disconnected`
- `granted_scopes`, `last_verified_at`, `last_success_at`
- `consent_expires_at`, `metadata_json`
- `provider_environment`: `sandbox`, `demo`, or `production`

Database constraints and startup checks reject environment mismatches. The
deployment mode is never supplied by a browser request.

### `close_configuration_versions`

- `id`, `organization_id`, `version`
- selected Xero tenant and ledger account mappings
- selected bank accounts
- required-document checklist and Drive/Gmail search scope
- approved email recipients/domains and template versions
- materiality, date-window, and matching tolerances
- permitted Xero draft-journal types and accounts
- retention-policy version and per-data-class retention requirements
- approver identity
- `created_by`, `created_at`, `superseded_at`

Configuration is immutable after use by a close run. Editing setup creates a new
version.

## 6. Provider Synchronization

Every close starts with a synchronization barrier. The workflow never assumes
that data is current because a connection previously succeeded.

### Shared `SourceBatch` Contract

Every source adapter returns provider/environment identity, tenant or account
IDs, immutable payloads or permitted copies, source watermarks, provider
request/event IDs, completeness, warnings, and failure details. Normalization
and snapshot code depend on this contract rather than provider SDK objects.

### Xero Direct Demo Adapter

The demo reads the Xero Demo Company through `XeroDirectDemoAdapter`. It performs
bounded paginated reads, records request IDs and source timestamps, and writes
append-only payloads to `raw_xero_demo` before normalization. It does not call
Fivetran and it never connects to a live customer tenant.

### Xero Through Fivetran

1. Validate that the Fivetran Xero connection is `connected` and its selected
   Xero organization matches the AccountingOS organization.
2. Request or await an incremental sync using the supported Fivetran control
   flow.
3. Receive `sync_end` webhook or poll connection status until completion.
4. Record connection ID, sync ID when available, start/end timestamps,
   `succeeded_at`, schema status, warnings, and affected tables.
5. Reject delayed, failed, incomplete, wrong-tenant, or warning states that make
   close data unreliable.
6. Run normalization and source-total checks after Fivetran commits its changes.

A historical re-sync is an administrative action, not a normal close task.

### Xero Direct Verification

The Xero MCP server performs a bounded read of tenant identity, organization
settings, accounts, and required control totals. In production the run blocks
when the direct tenant differs from Fivetran or material control totals do not
agree. In demo mode it verifies the Demo Company identity and direct-source
watermark without pretending that Fivetran was used.

### US Bank Through Plaid

1. The organization connects accounts through Plaid Link and grants Transactions
   access.
2. Access tokens are stored in the secret store; the database stores an opaque
   secret reference and Plaid Item ID.
3. Webhooks trigger cursor-based transaction synchronization.
4. At close start, request refresh when production access permits, then consume
   all transaction pages until the cursor is current.
5. Read all pages from the original cursor, collect `added`, `modified`, and
   `removed`, and commit those changes plus the new cursor in one transaction.
   Restart from the original cursor if Plaid reports mutation during pagination.
6. Record Item ID, account IDs, cursor, webhook/request IDs, transaction date
   range, and completion time.
7. Pending and posted transactions are distinct. Pending transactions are never
   silently treated as final ledger evidence.

### India Bank Through Setu Account Aggregator

1. The customer grants consent for selected current/savings accounts, purpose,
   date range, data types, and duration.
2. Store the Setu consent ID and encrypted/opaque credential references.
3. Start the FI data session for the requested close range.
4. Process consent and `FI_DATA_READY` notifications idempotently.
5. Accept the data only when all selected accounts are delivered with a
   `COMPLETED` result for the requested range.
6. A `PARTIAL`, failed, expired, or revoked consent blocks reconciliation and
   identifies the affected FIP/account.
7. Record consent ID, session ID, FIP IDs, masked accounts, range, delivery
   status, and notification/request IDs.

### Google Drive and Gmail

Drive and Gmail are queried at workflow execution time using the configured
folders, mailbox, labels, contacts, and time range. Each result records Google
resource ID, modification/internal date, owner/sender, content hash, and OAuth
connection. Searches outside configured scope are prohibited.

## 7. Source Snapshot

After all required providers pass synchronization, the system creates an
immutable `source_snapshot`.

### `source_snapshots`

- `id`, `organization_id`, `period_start`, `period_end`
- `configuration_version`
- provider watermarks and completion timestamps
- `status`: `building`, `complete`, `invalidated`
- `created_at`, `invalidated_at`, `invalidation_reason`

### `snapshot_records`

- `snapshot_id`, `record_type`, `normalized_record_id`
- provider, provider tenant/account, provider record ID
- provider updated timestamp, ingestion timestamp, content hash
- unique source identity within the snapshot

The snapshot contains permitted copies or references to immutable, append-only
normalized record versions obtained from live systems. A provider ID and hash
that point only to a mutable raw/provider row are not a reproducible snapshot.
If a provider record changes after the snapshot, normalization creates a new
record version; the existing package remains reproducible and the controller
must refresh to create a new snapshot/package version.

## 8. Canonical Normalized Records

All regional/provider records map to typed canonical models:

- Organization and accounting period.
- Xero account, contact, invoice, payment, bank transaction, manual journal,
  journal line, and report control total.
- Bank account, balance, posted transaction, pending transaction, and provider
  status.
- Document, email, attachment, evidence reference, and checksum.
- Reconciliation match group, exception, journal proposal, report fact, and
  package artifact.

Every normalized financial record includes organization, currency, accounting
date, provider, provider ID, original amount, original payload hash, and
ingestion source. Normalization cannot change source amounts to force a match.

## 9. Live Workflow DAG

The only MVP template is `live-close-readiness-v1`.

```text
T01 Validate organization configuration and connections
  |
  +--> T02 Synchronize and normalize Xero through the configured source adapter
  |         |
  |         +--> T05 Verify Xero tenant and current control totals --+
  |                                                                 |
  +--> T03 Refresh regional bank source ----------------------------+--> T06 Build source snapshot
  |                                                                 |
  +--> T04 Query Drive and Gmail evidence --------------------------+
                                                                      |
                                                                      +--> T07 Evaluate checklist/AP readiness --+
                                                                      |                                         |
                                                                      +--> T08 Reconcile bank and Xero ---------+
                                                                                                                |
                                                                                                                v
                                                                                                      T09 Investigate exceptions
                                                                                                                |
                                                                                                                v
                                                                                                      T10 Prepare journal proposals
                                                                                                                |
                                                                                                                v
                                                                                                      T11 Calculate reports
                                                                                                                |
                                                                                                                v
                                                                                                      T12 Generate executive summary
                                                                                                                |
                                                                                                                v
                                                                                                      T13 Assemble review package
                                                                                                                |
                                                                                                                v
                                                                                                      T14 Await controller decision
                                                                                                                |
                                                                                           approval ------------+---- changes requested
                                                                                              |                            |
                                                                                              v                            v
                                                                                   T15 Create Xero drafts          rerun affected tasks
                                                                                              |
                                                                                              v
                                                                                   T16 Read back and create
                                                                                       final action manifest
```

T02-T04 execute concurrently when connections permit. T05 depends on T02 because
it compares direct Xero totals with the newly normalized source result. In demo
mode T02 uses `XeroDirectDemoAdapter`; production later uses Fivetran. T07 and
T08 may run concurrently after T06. T14 is a persisted wait state, not a worker
task holding a lease. Recording approval freezes the T13 review package before
T15 begins. T15 never runs without approval tied to the exact source snapshot,
package hash, and journal proposal set.

## 10. Run State Machine

### States

- `created`
- `preflight`
- `synchronizing`
- `running`
- `awaiting_input`
- `blocked`
- `awaiting_approval`
- `changes_requested`
- `applying_approved_actions`
- `cancellation_requested`
- `action_failed`
- `approved`
- `failed`
- `cancelled`

### Allowed Transitions

```text
created -> preflight -> synchronizing -> running
preflight | synchronizing | running -> blocked -> preflight | synchronizing | running
running -> awaiting_input -> running
running -> awaiting_approval
awaiting_approval -> changes_requested -> synchronizing | running
awaiting_approval -> applying_approved_actions
applying_approved_actions -> cancellation_requested | approved
applying_approved_actions -> action_failed -> applying_approved_actions
cancellation_requested -> cancelled | action_failed
preflight | synchronizing | running | awaiting_input | blocked | changes_requested -> failed
failed -> preflight | synchronizing | running
created | preflight | synchronizing | running | awaiting_input | blocked | awaiting_approval | changes_requested -> cancelled
```

Approval is persisted before external Xero writes. Once an external action has
started, cancellation stops new actions, reconciles started actions, and only
then permits a terminal result. `action_failed` means the
controller decision and frozen review package remain valid but one or more
approved actions need reconciliation or safe retry. An action with an unknown
provider outcome cannot retry automatically. `approved` means all intended Xero
drafts were created and read back, or the approved package contained no journal
proposals.

## 11. Task Claims, Leases, and Recovery

The worker claims a dependency-ready task using PostgreSQL
`SELECT ... FOR UPDATE SKIP LOCKED`.

- Lease duration: 60 seconds.
- Heartbeat: every 15 seconds.
- Task-specific timeout and maximum attempts.
- Only transient network, provider, or database failures retry automatically.
- Accounting-control, permission, partial-data, and policy failures block or
  fail without blind retry.
- Restart recovery scans expired leases and checks the external-action ledger
  before deciding whether to retry.

Task idempotency key:

```text
{organization_id}:{run_id}:{workflow_version}:{task_key}:{package_version}
```

## 12. External-Action Idempotency

### `action_executions`

- `id`, `organization_id`, `run_id`, `task_id`
- provider, operation, idempotency key, request hash
- approval ID and policy decision where required
- status: `prepared`, `started`, `succeeded`, `failed`, `outcome_unknown`, or
  `reconciled`
- provider request/response identifiers
- created, started, completed timestamps
- unique idempotency key

### Gmail Send

1. Create and persist a Gmail draft with an AccountingOS action marker.
2. Re-evaluate recipient/template policy immediately before send.
3. Send the persisted draft.
4. Store Gmail message/thread IDs.
5. If a crash occurs after sending but before persistence, search the Sent
   mailbox for the action marker before retrying.
6. If the search is unavailable or ambiguous, mark `outcome_unknown` and stop;
   never send again merely because the local success record is missing.

### Xero Draft Journal Creation

1. Persist the approved action and a deterministic AccountingOS proposal marker.
2. Acquire a database advisory lock for the organization/proposal.
3. Query Xero for an existing manual journal carrying the marker.
4. If found, compare all lines and adopt its Xero ID only when identical.
5. Otherwise create the journal with status forced to `DRAFT`.
6. Persist the Xero ID and response, then read it back and compare status, date,
   narration, accounts, amounts, and lines.
7. A mismatch moves the run to `action_failed` and requires controller review.
8. A timeout or failed reconciliation that cannot prove absence or identity
   marks `outcome_unknown`; creation cannot retry until reconciliation resolves
   the provider outcome.

No retry path can change the action from draft creation to posting.

### B2 Artifacts

Artifact keys include organization, run, package version, artifact type, and
SHA-256. Upload retries return the existing object when content matches.
Approved review packages and their action manifests use B2 Object Lock for the
configured retention window. Approval freezes the review package; later action
results create a separate content-addressed manifest and never replace it.

## 13. MCP and Policy Gateway

MCP standardizes tool schemas; it does not replace provider authentication or
authorization. Every MCP request carries organization, user, run, task, and
approved policy context.

### Xero MCP Tools

- `xero.connection_status`
- `xero.get_organization`
- `xero.get_accounts`
- `xero.get_control_totals`
- `xero.get_manual_journal`
- `xero.create_draft_manual_journal`

There is no create-posted, update-status, delete, void, payment, or period-lock
tool.

### Bank MCP Tools

- `bank.connection_status`
- `bank.refresh`
- `bank.list_accounts`
- `bank.list_balances`
- `bank.list_transactions`

Bank tools are read-only and dispatch to Plaid or Setu by organization market.

### Google MCP Tools

- `drive.search_evidence`
- `drive.fetch_evidence`
- `gmail.search_evidence`
- `gmail.create_request_draft`
- `gmail.send_approved_request`

### Fivetran Control Tools

- `fivetran.connection_status`
- `fivetran.request_sync`
- `fivetran.sync_result`

Tool schemas use strict enums and identifiers. Servers validate inputs, enforce
access control and rate limits, sanitize outputs, and audit calls. The model can
request a tool, but the policy gateway authorizes and executes it.

## 14. AI Trust Boundary

Provider records, invoices, emails, attachments, and document text are untrusted
model input and may contain prompt injection.

- Extract data into bounded schemas before model use.
- Supply the minimum evidence required for the current exception.
- Wrap evidence as quoted data and prohibit obeying embedded instructions.
- Never expose OAuth tokens, provider secrets, unrestricted SQL, raw mailbox
  access, arbitrary URLs, or arbitrary MCP tool discovery to the model.
- Validate tool arguments independently of model output.
- Validate every cited record against the run snapshot.
- Reject amounts, accounts, dates, or conclusions absent from the supplied fact
  set.
- Store model, prompt version, schema version, evidence IDs, input/output hashes,
  token/latency metadata, validation result, and concise rationale.
- Do not store chain-of-thought.

AI-dependent tasks retry once for invalid structured output. They fail closed
after validation failure; they do not substitute invented fallback content.

## 15. Deterministic Accounting Controls

### Source Controls

- All required connections healthy and authorized.
- Source-adapter/Xero tenant identity agreement.
- Correct organization, currency, timezone, period, and selected accounts.
- Complete regional bank delivery for selected accounts/date range.
- Source watermarks newer than the run synchronization request.
- Provider row counts and totals recorded before normalization.

### Reconciliation Controls

Rules are organization-versioned and support:

- exact account/currency/amount/reference matches
- configurable posting-date windows
- one-to-many and many-to-one groups
- merchant processor batch deposits
- bank fees and interest
- duplicate candidates
- pending-to-posted transitions

Every source transaction is `matched`, `excluded_by_policy`, or represented by
an open exception. Tolerances cannot silently erase an exception.

### Journal Controls

- Valid Xero account codes from the current snapshot.
- Total debits equal total credits per proposal and in aggregate.
- Journal date falls within the selected period or an explicitly approved
  adjustment period.
- Every line cites source evidence and a verified exception or policy basis.
- Xero request status is always `DRAFT`.
- Read-back matches the approved proposal exactly.

### Report Controls

- Xero/source-adapter control totals agree within documented source semantics.
- Unadjusted trial balance debits equal credits.
- Pro forma adjusted trial balance debits equal credits.
- Adjusted balance sheet satisfies assets = liabilities + equity.
- Cash reconciliation explains the difference between selected bank balances
  and Xero cash ledgers at the snapshot cutoff.
- Reports label unposted local/Xero draft adjustments as pro forma.

## 16. Primary Data Tables

In addition to organization, connection, configuration, raw, and snapshot tables:

### `close_runs`

- organization, period, workflow/configuration version
- source snapshot ID, status, active package version
- controller, row version, timestamps

### `source_syncs`

- provider, connection, request type, request/provider IDs
- requested range, cursor/consent/sync watermarks
- status, row counts, warnings, started/completed timestamps

### `webhook_receipts`

- provider, event type, provider event/request ID
- signature verification result, payload hash, received/processed timestamps
- unique provider event identity where available

### `tasks` and `task_dependencies`

- typed inputs/outputs, status, required flag
- attempts, leases, timeout, idempotency key, error code
- unique task key per run

### `evidence`

- snapshot/source record, provider identity, B2 object when retained
- checksum, content type, source URI/ID, validation and trust status

### `exceptions`

- type, amount/currency, status, rationale, recommendation
- cited evidence, uncertainties, deterministic control results

### `journal_proposals` and `journal_proposal_lines`

- package version, date, narration, status
- Xero account IDs/codes, debit, credit, evidence references
- approval and Xero write/read-back status

### `reports` and `close_packages`

- calculation/source-snapshot versions, fact set, artifact checksum
- package status `draft`, `review_frozen`, or `finalized`
- approved review-package hash, Object Lock retention, and frozen timestamp
- action manifest/receipt that references rather than mutates the approved package
- provider watermarks and action completion summary

### `approvals`, `ai_invocations`, `action_executions`, `audit_events`

- immutable decision, reproducibility, idempotency, and timeline records
- globally increasing audit sequence for SSE replay

## 17. API Surface

All application endpoints are under `/api/v1`.

| Method and path | Purpose |
| --- | --- |
| `POST /organizations` | Create organization shell |
| `GET /organizations/{id}/connections` | Health, scopes, consent, freshness |
| `POST /organizations/{id}/connections/{provider}/start` | Start provider onboarding |
| `GET /oauth/{provider}/callback` | Complete verified OAuth flow |
| `POST /webhooks/{provider}` | Verified provider event ingestion |
| `POST /organizations/{id}/configurations` | Create a versioned close setup |
| `POST /close-runs` | Create a live run for a period |
| `GET /close-runs/{id}` | State, source watermarks, package/action status |
| `GET /close-runs/{id}/tasks` | Dependencies, attempts, blockers |
| `GET /close-runs/{id}/events` | SSE replay using `Last-Event-ID` |
| `GET /close-runs/{id}/evidence` | Authorized evidence metadata/downloads |
| `GET /close-runs/{id}/exceptions` | Cited explanations and controls |
| `GET /close-runs/{id}/packages/{version}` | Package manifest and artifacts |
| `POST /close-runs/{id}/approvals` | Approve or request changes |
| `POST /close-runs/{id}/retry` | Policy-safe retry |
| `POST /close-runs/{id}/refresh` | Create a new source snapshot/package version |
| `POST /close-runs/{id}/cancel` | Cancel a nonterminal run |
| `DELETE /organizations/{id}/connections/{provider}` | Disconnect/revoke provider |

All state-changing endpoints require authentication, CSRF protection where
applicable, `Idempotency-Key`, organization authorization, and optimistic
concurrency. Stale writes return HTTP 409.

## 18. Security and Privacy

- Provider access/refresh tokens live in the managed secret store.
- Database records contain opaque secret references only.
- Human identity tokens are accepted only from configured OIDC issuers and are
  validated for signature, issuer, audience, expiry, and authorization-code/PKCE
  binding. Organization access comes from `organization_users`, never from an
  organization ID supplied by the browser.
- OAuth state, PKCE, redirect URI, and tenant selection are validated. Issuer and
  nonce are also validated only for provider flows that use OpenID Connect.
- Webhook signatures are validated before persistence; payload hashes and event
  identities prevent replay.
- PostgreSQL row-level security or equivalent repository guards enforce
  organization isolation.
- B2 objects are organization/run scoped; downloads use short-lived signed URLs.
- Logs redact tokens, email/document bodies, bank account numbers, PAN, personal
  profile fields, signed URLs, and provider payloads.
- Only masked account identifiers appear in the UI except where the provider and
  user authorization explicitly permit more.
- Evidence retention and deletion follow organization policy and provider/legal
  requirements; approved audit records retain hashes and minimum metadata.
- Model inputs exclude unnecessary personal data and entire documents when
  extracted facts suffice.

## 19. Observability

Structured events include organization, run, snapshot, task, attempt, provider,
connection, action, approval, and request IDs.

Metrics:

- provider onboarding success/failure
- sync duration, freshness age, partial delivery, consent expiry
- Fivetran warnings and source-total mismatches
- webhook validation/replay failures
- task duration, retries, lease expiry, and blocker duration
- reconciliation match/exception rates without exposing financial values
- AI latency and validation rejection rate
- email and Xero action success/read-back mismatch
- SSE replay and disconnected clients

The user timeline is built from persisted audit events, not process logs.

## 20. Testing Strategy

### Unit

- State transitions and approval/action guards.
- Normalization through the shared source contract for Xero and Plaid; add Setu
  when the India live adapter begins.
- Reconciliation algorithms and pending/posted handling.
- Journal and report accounting invariants.
- Email policy and MCP tool authorization.
- AI evidence and amount validation.
- Idempotency and deterministic action markers.

### Provider Contract

- Xero Demo Company for OAuth, reads, draft creation, deterministic marker
  lookup, and read-back. Never post journals.
- Plaid sandbox/development for webhooks, cursor sync, refresh, pending/posted,
  token errors, and institution failures.
- Setu sandbox for consent, partial/complete FI delivery, notification replay,
  expiry, and revocation.
- Google test Workspace for scoped Drive/Gmail actions.
- Fivetran non-production destination for setup, incremental sync, webhook, and
  schema-change tests.

### Live Acceptance

- Designated US and India pilot organizations only.
- Production data and provider credentials.
- Read-only smoke tests by default.
- Explicit controller approval for the live allowlisted email and Xero draft
  journal acceptance steps.
- Verification that no production test/fixture configuration is enabled.

### Recovery and Security

- Crash after Gmail send and after Xero draft creation before local persistence.
- Provider timeout with an unsearchable or ambiguous external-action result.
- Package immutability before approval, during action failure, and after retry.
- Duplicate/out-of-order provider webhooks.
- Expired OAuth, revoked Plaid Item, demo scenario seed failure, and later
  expired/partial Setu consent.
- Prompt injection in email/document evidence.
- Cross-organization record, evidence, tool, and SSE access attempts.
- Provider rate limits, network timeouts, and stale Fivetran data.

## 21. Deployment

Use a separate demo stack and separate later production stacks:

```text
accounts-demo.example.com -> Demo API/worker/webhooks -> Demo Postgres/B2/secrets
accounts-us.example.com    -> US API/worker/webhooks    -> US Postgres/B2/secrets
accounts-in.example.com    -> IN API/worker/webhooks    -> IN Postgres/B2/secrets
```

The demo stack has Plaid Sandbox, Xero Demo Company, test Workspace, and demo
artifact credentials. Production stacks have their own Fivetran destinations and
regional provider credentials. No credentials, callbacks, databases, or
artifacts cross stacks.
The codebase and migrations are shared. Cross-region operational dashboards may
use aggregate non-financial metrics only; financial records are not copied
between deployments by the MVP. Physical database, B2, and model-processing
locations must match the approved market data-processing policy; `IN` is a
logical deployment name and does not imply that every vendor offers an India
storage region.

## 22. Implementation Order

### Phase 0: Demo Capability Spikes

- Create isolated Plaid Sandbox, Xero Demo Company, Google test Workspace, B2,
  OpenAI, and managed OIDC credentials.
- Confirm Xero Demo Company OAuth, pagination, account codes, draft status,
  proposal markers, rate limits, and read-back behavior.
- Confirm Plaid Sandbox Link, dynamic transaction seeding, cursor sync,
  pending/posted behavior, webhook replay, and Item error behavior.
- Verify demo scenario seeding and reset/repeat behavior.

Exit: the synthetic scenario can be seeded and read coherently from both
providers; no unverified demo-provider assumption remains.

### Phase 1: Demo Organization and Connection Platform

- Demo deployment, identity, organization isolation, environment guards, secret store,
  connection records, OAuth callbacks, webhook verification, and connection UI.

Exit: a controller can connect Xero Demo Company, Plaid Sandbox, and test
Workspace and see verified health/scopes without starting a close.

### Phase 2: Demo Ingestion and Snapshots

- Direct Xero demo and Plaid ingestion, normalization, source-total
  verification, freshness barrier, immutable snapshot, and audit/SSE timeline.

Exit: a close run produces a reproducible synthetic snapshot and blocks correctly
on stale, partial, or mismatched provider data.

### Phase 3: Evidence and Real Email

- Drive/Gmail MCP tools, checklist, evidence validation, recipient/template
  policy, Gmail draft/send idempotency, and evidence UI.

Exit: a test Workspace allowlisted request has one verified send effect and is audited;
an ambiguous provider outcome stops for reconciliation instead of resending.

### Phase 4: Reconciliation and Reports

- Bank/Xero matching, exception model, journal proposal controls, trial balance,
  P&L, balance sheet, cash reconciliation, and package facts.

Exit: synthetic account totals tie or every difference is represented by evidence or
an exception; all proposed journals balance.

### Phase 5: Grounded AI

- Plan explanation, exception recommendation, executive summary, prompt
  injection controls, citation/amount validation, and model audit records.

Exit: invalid or unsupported output fails closed; valid output cites only the
synthetic snapshot.

### Phase 6: Approval and Xero Draft Creation

- Package review, controller decision, approved action ledger, Xero draft tool,
  crash recovery, read-back verification, and frozen package.

Exit: a controller-approved proposal produces one verified Xero `DRAFT` manual
journal or a visibly unresolved outcome that cannot auto-retry; no posting tool
exists and tested recovery paths create no duplicate.

### Phase 7: Production Expansion and Hardening

- Complete US live acceptance while India production onboarding proceeds.
- Complete India live acceptance after FIU/Sahamati and bank-FIP gates pass.
- Security, load, accessibility, retention, disconnect/revoke, runbooks, and
  provider outage exercises.

Exit: the later production gates and live acceptance criteria in `PRD.md` pass
for the applicable market.

## 23. Definition of Done

The demo milestone is complete when:

- The isolated demo stack cannot select production credentials or data.
- One seeded US scenario passes synchronization, snapshot, reconciliation,
  approval, Xero `DRAFT` creation, read-back, and audit verification.
- Failure, duplicate webhook, ambiguous action, and cancellation paths are tested.

The later live product is complete only when:

- One US and one India pilot pass the full production acceptance flow.
- No production code path or configuration can select fixture/test data.
- Every package identifies live source records and provider watermarks.
- Stale, partial, revoked, and inconsistent sources fail visibly.
- External actions are approval/policy authorized and effectively-once through
  deterministic keys, provider reconciliation, and fail-closed unknown outcomes.
- Xero journals are created only as approved drafts and read back successfully.
- No payment, posting, deletion, voiding, or period-lock tool exists.
- Accounting and security tests pass.
- Provider revocation and service restart preserve correctness and auditability.
