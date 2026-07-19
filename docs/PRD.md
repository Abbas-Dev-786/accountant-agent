# AccountingOS US Production Product Requirements

**Version:** 1.3
**Status:** Production-first scope selected; production implementation and
external onboarding remain gated
**Decision date:** 2026-07-18
**Primary user:** Controller
**Active market:** United States
**Deferred market:** India

This document is the product source of truth. `TDD.md` controls implementation
behavior, `live_integrations.md` controls provider-specific onboarding and
production gates, and `demo_architecture.md` controls fixture isolation only.

## 1. Product Outcome

AccountingOS prepares a reviewable month-end close package from an authenticated
provider environment:

> Prepare the selected accounting period for controller review and approval.

The product reads authenticated production records from a connected Xero
organization, selected Plaid Production accounts, and configured Google
Workspace scopes; reconciles selected accounts; investigates exceptions;
generates reports; and, after explicit controller approval, creates balanced
**draft** manual journals in that Xero organization.

The MVP does not automatically post journals, move money, release payments, or
lock an accounting period. A package may be approved while its Xero journals
remain in draft status.

## 2. Environment and Data Boundary

Production is the active deployment class. Any optional synthetic fixture is a
separate deployment with separate databases, secrets, callbacks, artifacts, and
provider credentials.

- Production mode uses authenticated live systems and real customer data.
- No connector may silently fall back to local data in either mode.
- A stale, disconnected, partially synchronized, or unauthorized source blocks
  the affected workflow and identifies the real remediation.
- Every displayed record links to its provider, source identifier, tenant or
  account, and synchronization watermark.
- Every production run records `production` and `live`.
- Production configuration rejects test connectors, tenants, Items, schemas,
  callbacks, and artifacts.
- Fixture runs, if enabled by engineering, are visibly labelled `DEMO —
  SYNTHETIC DATA` and are never product acceptance evidence.

See `demo_architecture.md` for fixture isolation controls.

## 3. Target Customer and Milestone Shape

The initial customer is a small finance team using Xero with one controller who
owns the close.

The active production scope supports one configuration:

| Market | Accounting system | Bank source | Currency |
| --- | --- | --- | --- |
| US Production | Xero | Plaid Production Transactions | USD |

India/Xero/Setu is deferred. Each organization has one functional currency, and
a run cannot combine markets, currencies, or accounting periods.

## 4. Required Connections

Before a close run can start, the controller must connect and validate:

1. Xero through the approved production integration for reads and
   controller-approved draft-journal creation.
2. One or more Plaid Production accounts selected by the organization.
3. Google Drive folders containing close evidence.
4. Gmail account used to search for evidence and send policy-approved requests.
5. Backblaze B2 for content-addressed close-package artifacts, with Object Lock
   retention for approved packages.
6. Groq for bounded exception explanations and executive summaries.

The product must show connection health, granted permissions, latest successful
sync, consent expiry, and remediation steps for each connection.

## 5. Organization Setup

The controller completes setup once per organization:

- Select the Xero tenant for this organization from the connected tenants.
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
3. The system synchronizes the selected production provider environment using
   the approved Xero and Plaid Production integrations.
4. The workflow waits for successful provider completion and records source
   watermarks. Partial or stale data blocks execution.
5. A read-only source snapshot is created from the ingested records and provider
   identifiers. The configured provider environment remains authoritative.
6. The system loads the versioned checklist and inventories evidence from the
   configured production Workspace, Xero, and bank provider.
7. Missing evidence is shown to the controller. Requests follow the
   outbound-email policy.
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

### Required for the US Production Release

- US production deployment with tenant-isolated organizations and controllers.
- Authenticated Xero production reads and controller-approved `DRAFT` journals.
- Plaid Production Transactions with cursor synchronization and webhook replay.
- Google Workspace evidence search and policy-controlled email.
- Production B2 artifact storage with Object Lock.
- Versioned close configuration and source snapshots.
- Fixed, versioned close-readiness workflow with persisted task execution.
- Deterministic document, reconciliation, journal, and reporting controls.
- Evidence-grounded AI exception explanations and executive summaries.
- Controller approval and request-changes workflow.
- Creation of approved, balanced Xero manual journals in `DRAFT` status.
- Append-only audit timeline for every read, decision, approval, and write.
- Support for USD organizations through the US adapter contract.

### Optional Test-Fixture Scope

- Isolated Xero Demo Company, Plaid Sandbox, and Google test Workspace only for
  automated and operator verification.
- No synthetic run, fixture tenant, or sandbox result can satisfy a production
  release gate.

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
references, validate required scopes, and show the connected tenant(s)/accounts.
Xero authorization discovers every granted tenant and registers a connection per
tenant; an optional tenant allowlist restricts which are registered. Connection
setup is incomplete until provider health tests pass.

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
the configured production Workspace, Xero, and bank environment. Missing-
document requests comply with the outbound-email policy.

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
- Approved production Xero ingestion path and verified source/control-total
  behavior.
- Google OAuth application with the required Drive/Gmail verification and scopes.
- Groq and B2 production credentials, approved data-processing/retention
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

## 12. Production Acceptance Criteria

The US production product is complete only when all of the following pass
without fixtures, sandbox providers, or simulated business data:

1. A real Xero organization completes the approved historical and incremental
   source synchronization.
2. A fresh close run records a successful source watermark and verifies the same
   tenant through the controlled Xero action path.
3. A real US pilot connects a bank through Plaid and ingests posted transactions.
4. Real Drive and Gmail connections discover evidence using configured scopes.
5. At least one policy-compliant missing-document email is sent through Gmail and
   its real message ID is audited.
6. Reconciliation results tie to the selected live bank and Xero accounts, with
   every unmatched amount represented by an exception.
7. AI explanations contain no uncited record or unsupported amount.
8. Every proposed journal balances and uses valid Xero account codes.
9. Package approval creates the approved journal in the real Xero organization
   with status `DRAFT`, then reads it back and records its Xero ID.
10. No endpoint or MCP tool can post the journal or move money.
11. Reports pass trial-balance, accounting-equation, and cash-reconciliation
   controls against the frozen live snapshot.
12. Provider token expiry, consent revocation, stale sync, partial bank delivery,
   rate limiting, webhook replay, and API/worker restart are tested.
13. Retrying a task does not send a duplicate email, create a duplicate Xero
   journal, or duplicate an artifact; an ambiguous provider outcome blocks
   automatic retry until reconciliation resolves it.
14. The complete audit history survives refresh and service restart.
15. Production configuration rejects any test connector, test tenant, test data
   namespace, or local artifact store.

## 14. Success Measures

- Every close package is traceable to its provider records and watermarks.
- Zero uncited AI claims reach controller review.
- Zero duplicate external actions across retries.
- Zero unapproved or posted Xero journals created by AccountingOS.
- Controllers can identify source freshness, blockers, evidence, adjustments,
  and write status without examining logs.
- At least one US production organization completes the production acceptance
  flow. India requires a separate future scope approval.

## 13. Test-Data Boundary

The synthetic-data rule applies to the isolated demo deployment; the live-data
rule applies to production and live acceptance. Engineering still requires
controlled tests:

- Unit tests use generated records for deterministic accounting functions.
- Integration tests use isolated provider test organizations or sandboxes where
  available.
- Fixture smoke tests may create test Gmail messages and Xero drafts in an
  isolated test organization. Production smoke tests are read-only except for explicitly approved
  Gmail and Xero draft-journal acceptance tests in designated pilot organizations.
- Test credentials, schemas, tenants, and artifacts are physically and logically
  separated from production.

This boundary is necessary to prove correctness without risking a customer's
ledger or sending unintended real-world actions during routine test execution.
