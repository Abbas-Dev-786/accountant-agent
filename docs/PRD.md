# AccountingOS Demo MVP and Live Product Requirements

**Version:** 1.3
**Status:** Approved for demo implementation; live product remains gated  
**Decision date:** 2026-07-18  
**Primary user:** Controller  
**Demo market:** United States  
**Later live markets:** United States and India

This document is the product source of truth. `demo_architecture.md` controls
demo boundaries, `TDD.md` controls implementation behavior, and
`live_integrations.md` controls provider-specific setup and production gates.

## 1. Product Outcome

AccountingOS prepares a reviewable month-end close package from an authenticated
provider environment:

> Prepare the selected accounting period for controller review and approval.

The demo reads synthetic records from Plaid Sandbox and a Xero Demo Company,
reconciles selected accounts, investigates exceptions, generates reports, and,
after explicit controller approval, creates balanced **draft** manual journals
in the Demo Company. The later live product performs the same workflow against
authenticated production providers and real customer data.

The MVP does not automatically post journals, move money, release payments, or
lock an accounting period. A package may be approved while its Xero journals
remain in draft status.

## 2. Environment and Data Boundary

Demo and production are separate deployments with separate databases, secrets,
callbacks, artifacts, and provider credentials.

- Demo mode uses synthetic provider data only: Plaid Sandbox, a Xero Demo
  Company/test organization, and a Google test Workspace.
- Production mode uses authenticated live systems and real customer data.
- No connector may silently fall back to local data in either mode.
- A stale, disconnected, partially synchronized, or unauthorized source blocks
  the affected workflow and identifies the real remediation.
- Every displayed record links to its provider, source identifier, tenant or
  account, and synchronization watermark.
- Every run records `demo` or `production` and `synthetic` or `live`.
- Production configuration rejects test connectors, tenants, Items, schemas,
  callbacks, and artifacts; demo configuration rejects live credentials.
- The UI labels demo runs `DEMO — SYNTHETIC DATA`.

See `demo_architecture.md` for the complete demo boundary.

## 3. Target Customer and Milestone Shape

The initial customer is a small finance team using Xero with one controller who
owns the close.

The demo milestone supports one configuration:

| Market | Accounting system | Bank source | Currency |
| --- | --- | --- | --- |
| Demo US | Xero Demo Company | Plaid Sandbox Transactions | USD |

The later live product adds US Production/Plaid and India/Xero/Setu. Each
organization has one functional currency, and a run cannot combine markets,
currencies, or accounting periods.

## 4. Required Connections

Before a close run can start, the controller must connect and validate:

1. Xero Demo Company through the direct Xero API adapter for demo reads and
   controller-approved draft-journal creation.
2. One or more Plaid Sandbox accounts for the demo.
3. Google Drive folders containing close evidence.
4. Gmail account used to search for evidence and send policy-approved requests.
5. Backblaze B2 for content-addressed close-package artifacts, with Object Lock
   retention for approved packages.
6. OpenAI for bounded exception explanations and executive summaries.

The product must show connection health, granted permissions, latest successful
sync, consent expiry, and remediation steps for each connection.

## 5. Organization Setup

The controller completes setup once per organization:

- Select the Xero tenant.
- Select the functional currency and accounting timezone.
- Select bank accounts included in reconciliation.
- Map bank accounts to Xero ledger accounts.
- Select Google Drive evidence folders and Gmail mailbox/labels.
- Define required close documents and responsible contacts.
- Add allowlisted email recipients or domains.
- Define materiality thresholds and reconciliation tolerances.
- Define which proposed journal types the product may create as Xero drafts.
- Select the approved retention-policy version for raw data, normalized snapshot
  records, evidence copies, packages, and audit metadata.
- Choose the controller approver.

Setup changes are versioned. Every close run records the configuration version
used so results remain reproducible.

## 6. End-to-End User Journey

1. The controller selects an organization and accounting period.
2. The system verifies every required connection, consent, permission, and token.
3. The system synchronizes the selected provider environment. Demo mode uses
   the direct Xero adapter and Plaid Sandbox; production later uses Fivetran and
   the applicable live bank adapter.
4. The workflow waits for successful provider completion and records source
   watermarks. Partial or stale data blocks execution.
5. A read-only source snapshot is created from the ingested records and provider
   identifiers. The configured provider environment remains authoritative.
6. The system loads the versioned checklist and inventories evidence from the
   configured test or live Workspace, Xero, and bank provider.
7. Missing evidence is shown to the controller. Demo mode may send only to the
   configured test mailbox; production follows the outbound-email policy.
8. Deterministic services validate AP readiness and reconcile bank transactions
   to Xero ledger entries.
9. Exceptions are created for unmatched, duplicated, missing, or inconsistent
   records. AI may explain an exception only from cited snapshot evidence.
10. Deterministic controls validate the proposed resolution and prepare balanced
    journal drafts locally.
11. The system calculates the close reports and executive summary from the
    frozen run snapshot.
12. The controller reviews source evidence, reconciliation results, proposed
    journals, reports, connection freshness, and the audit timeline.
13. The controller approves the exact package version or requests changes.
    Approval freezes the reviewed package and its source/proposal hashes before
    any external journal write begins.
14. Approval authorizes creation of the approved journals in Xero with status
    `DRAFT`. Xero identifiers and responses are written to the audit trail.
15. After read-back, the system creates an immutable action receipt and final
    manifest that reference the frozen approved package. Existing package
    artifacts are never modified. The UI identifies whether every approved Xero
    draft was created successfully or has an unresolved provider outcome.

## 7. MVP Scope

### Required for the Demo Milestone

- One isolated US demo deployment with one controller per demo organization.
- Xero Demo Company reads through `XeroDirectDemoAdapter`.
- Plaid Sandbox Transactions with cursor synchronization and webhook replay.
- Google test Workspace evidence search and policy-controlled test email.
- Demo B2 artifact storage.
- Versioned scenario bootstrap: seed Plaid Sandbox and Workspace, and verify a
  prepared Xero Demo Company baseline through an explicit reset runbook.
- Versioned close configuration and source snapshots.
- Fixed, versioned close-readiness workflow with persisted task execution.
- Deterministic document, reconciliation, journal, and reporting controls.
- Evidence-grounded AI exception explanations and executive summaries.
- Controller approval and request-changes workflow.
- Creation of approved, balanced Xero manual journals in `DRAFT` status.
- Append-only audit timeline for every read, decision, approval, and write.
- Support for the USD demo organization through the US adapter contract.

### Later Live Product Scope

- Xero ingestion through `FivetranXeroAdapter`.
- US Plaid Production and India Setu Account Aggregator.
- Real customer organizations and production acceptance.
- Market-specific retention, processing, certification, and go-live gates.

### Explicitly Deferred

- QuickBooks support.
- Automatic posting of Xero journals.
- Bank payments, transfers, refunds, payment cancellation, or beneficiary changes.
- Automatic emails to recipients outside configured allowlists.
- Multi-currency accounting within one close run.
- Multi-entity consolidation.
- Tax filing, payroll processing, inventory, fixed assets, and revenue recognition.
- General autonomous AP or AR operations outside close readiness.
- Arbitrary workflow generation.
- Self-modifying accounting policy or autonomous learning.
- A public marketplace of third-party MCP servers.

## 8. Financial Action Policy

### Read-Only Without Per-Action Approval

- Read Xero accounting data granted by the tenant connection.
- Read selected bank balances and transactions granted by consent.
- Search configured Drive folders and Gmail labels.
- Calculate matches, exceptions, draft journals, and reports locally.

### Policy-Controlled Email

An email may be sent without per-message approval only when all are true:

- The recipient is an exact allowlisted address or approved domain contact.
- The template is approved for missing-document requests.
- The message contains no payment instruction, bank detail, credential request,
  legal commitment, or journal-posting instruction.
- The attachment set is empty unless separately approved.
- Per-recipient and per-run rate limits are not exceeded.

All other messages require controller approval. Every sent message records the
Gmail message ID, recipient, template version, approving policy, and timestamp.

### Controller Approval Required

- Create a manual journal in Xero with status `DRAFT`.
- Change a package after controller review.
- Send any email outside the automatic allowlisted policy.

### Prohibited in the MVP

- Post, void, delete, or reverse a Xero journal.
- Create or approve payments.
- Move money or change bank instructions.
- Close or lock a Xero accounting period.

The MCP tool registry must not contain tools for prohibited actions.

## 9. Functional Requirements

### FR-1: Connection Onboarding

The system must complete provider OAuth/consent, store encrypted credential
references, validate required scopes, and show the selected tenant/accounts.
Connection setup is incomplete until provider health tests pass.

### FR-2: Freshness Barrier

Every run must request or await a fresh provider update and record:

- provider connection ID
- selected tenant or account IDs
- requested accounting period
- sync/request start and completion timestamps
- provider cursor, consent ID, or equivalent watermark
- row counts and completeness status

The run cannot continue when a required provider reports delayed, partial,
failed, expired, revoked, or unauthorized data.

### FR-3: Immutable Run Snapshot

The system must assign every normalized source record to a run snapshot with its
original provider ID, provider update timestamp, ingestion timestamp, and
content hash. Snapshot membership must point to an immutable normalized record
version or contain a permitted immutable copy of the facts used; a pointer and
hash to a mutable provider/raw row are not sufficient. Later provider changes do
not silently alter an existing package; the controller must refresh and create a
new package version.

### FR-4: Document Collection

The system must evaluate the organization's versioned close checklist against
the configured Workspace, Xero, and bank environment. Demo missing-document
requests use only the test mailbox; production requests comply with the
outbound-email policy.

### FR-5: Reconciliation

Bank-to-ledger matching must be deterministic and configurable by organization.
It must handle exact matches, date-window matches, aggregated deposits, bank
fees, pending-versus-posted transactions, duplicate candidates, and unmatched
items without using model confidence as a control.

### FR-6: Exception Investigation

AI output must contain a concise cause, recommendation, evidence IDs,
uncertainties, and confidence label. Every cited record must belong to the run
snapshot. Amounts, account codes, dates, and proposed journal lines are verified
deterministically before the output is marked verified.

### FR-7: Journal Preparation and Xero Creation

Local journal proposals must balance and cite supporting evidence. After package
approval, only approved journal proposals may be sent through the Xero MCP tool,
and the request must force `DRAFT` status. The system verifies the returned Xero
journal ID and reads it back before marking creation successful. If a timeout or
lost response leaves the provider outcome ambiguous, the action is marked
`outcome_unknown`; automatic retry is prohibited until provider reconciliation
proves that no journal was created.

### FR-8: Reports

Reports must be calculated from the run snapshot and approved local adjustments.
At minimum, the package includes adjusted trial balance, adjusted P&L, adjusted
balance sheet, cash reconciliation, exception schedule, and change log. All
reports identify their source watermarks and whether Xero drafts were created,
failed, or remain outcome-unknown.

### FR-9: Approval

Only the configured controller may approve or request changes. Approval records
the actor, package version, source snapshot, journal set, timestamp, and comment.
Approval cannot occur while required sources are stale or controls are failing.
Recording approval freezes the reviewed package before approved external actions
start; later action receipts reference it instead of mutating it.

### FR-10: Audit Trail

Every connection event, sync, read, MCP call, email, model invocation, control
result, state transition, approval, Xero write, and read-back verification emits
an append-only audit event. Store structured rationale, not chain-of-thought.

### FR-11: Consent and Disconnection

The controller can disconnect a provider and revoke product access. New runs are
blocked immediately. Existing audit records and approved packages follow the
configured retention policy; access tokens and refresh tokens are deleted or
revoked according to provider requirements.

## 10. Non-Functional Requirements

- **Correctness:** Accounting outputs are deterministic and independently
  verified. Provider data is never silently repaired by AI.
- **Security:** OAuth tokens and bank consent secrets are encrypted, never enter
  model context, and are accessible only to the relevant connector service.
  Controller login uses a managed OpenID Connect provider; every request is
  authorized against an explicit AccountingOS organization membership.
- **Reliability:** Syncs, tasks, uploads, and external actions use deterministic
  idempotency keys and provider reconciliation to produce an effectively-once
  effect. When Gmail or Xero cannot prove whether an action happened, recovery
  stops in an outcome-unknown state instead of risking a duplicate.
- **Transparency:** The UI shows actual provider, tenant/account, freshness,
  source watermark, and action status. It never claims success before provider
  confirmation.
- **Performance:** Product-controlled processing should begin within 5 seconds
  after all providers satisfy the freshness barrier. External sync time is shown
  separately and is never represented by a fabricated ETA.
- **Regional isolation:** Each organization has a market, currency, timezone,
  bank provider, and retention policy. Data from different organizations cannot
  share credentials, snapshots, evidence, or model context.
- **Accessibility:** Connection, review, and approval workflows meet WCAG AA for
  keyboard access, focus, labels, and contrast.

## 11. Production Access Gates

Implementation can begin before these gates complete, but a market cannot be
called live until its gates pass.

### Shared

- Xero production application and OAuth credentials.
- Fivetran account, production Xero connection, and PostgreSQL destination.
- Google OAuth application with the required Drive/Gmail verification and scopes.
- OpenAI and B2 production credentials, approved data-processing/retention
  configuration, and B2 Object Lock for approved package artifacts.
- Deployed HTTPS callback and webhook endpoints.

### United States

- Plaid production access for Transactions and required refresh/webhook features.
- At least one consenting pilot organization with a supported bank account.

### India

- Setu production agreement.
- FIU eligibility and Sahamati certification, or access through an already
  certified FIU partner.
- At least one consenting pilot organization whose selected bank participates as
  a supported Financial Information Provider.

## 12. Demo Acceptance Criteria

The demo milestone is complete when all of the following pass using only the
isolated demo deployment and synthetic provider records:

1. A versioned scenario bootstrap seeds Plaid Sandbox and the test Workspace,
   verifies the prepared Xero Demo Company baseline, and records every provider
   ID. Xero reset is an explicit operator runbook step, not an unverified API
   capability.
2. Plaid cursor synchronization applies added, modified, and removed records
   atomically and safely handles duplicate/out-of-order webhooks.
3. Xero Demo Company reads pass tenant, account-code, pagination, and source
   control checks through `XeroDirectDemoAdapter`.
4. A reproducible immutable snapshot is created with provider watermarks and
   data class `synthetic`.
5. Evidence, reconciliation, journal, trial-balance, accounting-equation, and
   cash controls pass or create explicit exceptions.
6. AI explanations cite only snapshot evidence and fail closed on invalid or
   unsupported output.
7. Controller approval freezes the exact package and proposal hashes.
8. One approved Xero `DRAFT` journal is created in the Demo Company, read back,
   and recorded in a separate action manifest.
9. Duplicate webhook, provider timeout, ambiguous action outcome, worker
   restart, and cancellation paths are visible and do not create duplicates.
10. The demo stack rejects production credentials, tenants, Items, callbacks,
    artifacts, and data.

## 13. Live Acceptance Criteria

The later live product is complete only when all of the following pass without
production fixtures or simulated providers:

1. A real Xero organization completes Fivetran historical and incremental sync.
2. A fresh close run records a successful Fivetran watermark and reads the same
   tenant through the Xero MCP server.
3. A real US pilot connects a bank through Plaid and ingests posted transactions.
4. A real India pilot grants AA consent and returns `COMPLETED` financial data
   through Setu; partial account delivery is handled as a blocker.
5. Real Drive and Gmail connections discover evidence using configured scopes.
6. At least one policy-compliant missing-document email is sent through Gmail and
   its real message ID is audited.
7. Reconciliation results tie to the selected live bank and Xero accounts, with
   every unmatched amount represented by an exception.
8. AI explanations contain no uncited record or unsupported amount.
9. Every proposed journal balances and uses valid Xero account codes.
10. Package approval creates the approved journal in the real Xero organization
    with status `DRAFT`, then reads it back and records its Xero ID.
11. No endpoint or MCP tool can post the journal or move money.
12. Reports pass trial-balance, accounting-equation, and cash-reconciliation
    controls against the frozen live snapshot.
13. Provider token expiry, consent revocation, stale sync, partial bank delivery,
    rate limiting, webhook replay, and API/worker restart are tested.
14. Retrying a task does not send a duplicate email, create a duplicate Xero
    journal, or duplicate an artifact; an ambiguous provider outcome blocks
    automatic retry until reconciliation resolves it.
15. The complete audit history survives refresh and service restart.
16. Production configuration rejects any test connector, test tenant, test data
    namespace, or local artifact store.

## 14. Success Measures

- Every close package is traceable to its provider records and watermarks.
- Zero uncited AI claims reach controller review.
- Zero duplicate external actions across retries.
- Zero unapproved or posted Xero journals created by AccountingOS.
- Controllers can identify source freshness, blockers, evidence, adjustments,
  and write status without examining logs.
- One US and one India pilot complete the live acceptance flow.

## 15. Test-Data Boundary

The synthetic-data rule applies to the isolated demo deployment; the live-data
rule applies to production and live acceptance. Engineering still requires
controlled tests:

- Unit tests use generated records for deterministic accounting functions.
- Integration tests use isolated provider test organizations or sandboxes where
  available.
- Demo smoke tests may create test Gmail messages and Xero drafts in the Demo
  Company. Production smoke tests are read-only except for explicitly approved
  Gmail and Xero draft-journal acceptance tests in designated pilot organizations.
- Test credentials, schemas, tenants, and artifacts are physically and logically
  separated from production.

This boundary is necessary to prove correctness without risking a customer's
ledger or sending unintended real-world actions during routine test execution.
