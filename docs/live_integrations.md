# US Production Integration Specification

**Version:** 1.3
**Status:** Required companion to `PRD.md` and `TDD.md`  
**Rule:** US production and acceptance runs use live authenticated providers
only. An isolated synthetic stack may be used for engineering verification but
can never satisfy a production gate.
**Decision date:** 2026-07-18

This document records what must be configured, verified, and operated for each
provider. It is intentionally explicit about external dependencies that the
application team cannot solve in code.

## 1. Provider Matrix

| Capability | Production US | Test fixture | Deferred India | Write capability |
| --- | --- | --- | --- | --- |
| Accounting | Xero through the approved production ingestion path | Xero Demo Company + direct adapter | Xero integration TBD | Xero draft journal through owned MCP |
| Bank transactions | Plaid Production Transactions | Plaid Sandbox | Setu Account Aggregator | Read-only |
| Evidence | Google Drive/Gmail | Google test Workspace | Google Drive/Gmail | Allowlisted Gmail requests |
| Artifacts | Backblaze B2 | Test B2 bucket | Backblaze B2 | Upload/download only |
| AI | Groq | Groq with synthetic evidence | Groq | No provider permissions |

The deployment environment and organization market determine the adapter. A
connection for the wrong environment, tenant, Item, or currency is rejected
during onboarding. Demo credentials and artifacts cannot be used by production.

## 1A. Optional Test-Fixture Stack

The test-fixture stack is a separate deployment with its own database, secret store,
B2 bucket, OAuth callbacks, webhook URL, and provider credentials. It uses
Plaid Sandbox, a Xero Demo Company through `XeroDirectDemoAdapter`, a Google
test Workspace, a demo B2 bucket, and Groq with synthetic evidence only.

Plaid Production and real customer Xero organizations are not part of the test
fixture. A versioned scenario manifest seeds or verifies coherent records across
fixture providers. A partial seed blocks the fixture run.

## 2. Xero Production Ingestion and Draft Actions

### Roles

The production Xero ingestion path is selected during production onboarding. It
must land immutable source records in the private `raw_xero` schema that the
application can read but not mutate, provide a freshness watermark, and support
control-total verification. A source implementation is not authorized to post
journals or otherwise modify the Xero organization.

The owned production Xero policy gateway is the controlled API path for:

- Tenant identity and source control totals.
- Bounded reads needed for freshness or read-back verification.
- Creating an approved manual journal with status `DRAFT`.

No ingestion implementation may be treated as a write-back or journal-posting
mechanism.

### Demo Xero Setup

- Register a non-production Xero app and callback for the demo stack.
- Connect only the designated Demo Company/test organization. Connection
  registration is multi-tenant by default, so the demo stack sets
  `ACCOUNTINGOS_XERO_TENANT_ALLOWLIST` to the Demo Company tenant id; any other
  granted tenant is skipped and never registered.
- Record the provider environment as `demo` and reject production tenants. The
  registry derives the environment from the deployment mode, so a demo
  deployment can only record `demo` connections.
- Verify account codes, pagination, rate limits, draft status semantics,
  deterministic proposal markers, and line-by-line read-back.
- Run the scenario bootstrap: seed Plaid and Workspace data, then verify every
  expected Xero-baseline provider identifier before a close run can start. A
  Xero Demo Company reset is an explicit operator runbook step.

### Demo Runtime Checks

1. The registered Xero tenant(s) are within the configured allowlist; in the
   demo stack that allowlist is the designated Demo Company alone.
2. The scenario version and provider IDs match across Xero, Plaid, and the test
   Workspace.
3. The direct adapter completed its bounded reads and recorded a source
   watermark.
4. No production credential, tenant, Item, callback, or artifact is reachable.

### Required Xero Scope Profile

Controller login is handled by Supabase Auth; Xero OAuth requests
only organization-data scopes. New Xero applications use these granular scopes:

This profile follows Xero's current [OAuth scopes
documentation](https://developer.xero.com/documentation/guides/oauth2/scopes/)
and the [Accounting API overview](https://developer.xero.com/documentation/api/accounting/overview).

- `offline_access`
- `accounting.settings.read`
- `accounting.contacts.read`
- `accounting.invoices.read`
- `accounting.payments.read`
- `accounting.banktransactions.read`
- `accounting.manualjournals` for the narrowly bounded draft-journal create and
  read-back path
- `accounting.reports.trialbalance.read`
- `accounting.reports.profitandloss.read`
- `accounting.reports.balancesheet.read`

The MVP does not request the Xero Journals endpoint or `accounting.journals.read`.
That endpoint has separate Xero access requirements and is unnecessary for the
defined direct-verification/report-control path. The Phase 0 capability spike
must record the exact granted scope set and fail onboarding if it differs from
this profile.

### Production Setup Checklist

- Register a Xero production app and configure OAuth callback URLs for US and IN
  deployments.
- Request exactly the Xero scope profile above; no broad deprecated scope or
  additional write scope is permitted.
- Complete the OAuth authorization-code flow with state, PKCE, redirect URI, and
  tenant-selection validation. Validate issuer and nonce only if the selected
  provider flow uses OpenID Connect.
- Configure the selected Xero ingestion implementation and private `raw_xero`
  destination.
- Enable its verified incremental update/completion mechanism.
- Record the source implementation, connection ID, tenant ID, destination
  schema, connection owner, sync frequency, and alert thresholds.
- Store Xero and ingestion credentials only in the secret manager.

### Production Runtime Checks

Before a run:

1. The configured Xero source connection is healthy and its schema status is ready.
2. The selected Xero tenant ID equals the configured tenant.
3. The latest sync completed after the run's sync request, or the configured
   freshness policy explicitly accepts the latest completed sync.
4. No sync warning affects a required table.
5. Xero control totals agree with the normalized production source data.

After an approved journal write:

1. The policy gateway confirms the controller approval and exact proposal hash.
2. The MCP server queries for the exact stored AccountingOS narration/marker
   before creating anything, then verifies the full approved request hash.
3. The journal request sets status to `DRAFT`.
4. The returned journal identifier is persisted.
5. The journal is read back and compared line-by-line with the approved proposal.

The implementation must run a capability spike against the target Xero plan and
production app before promising any field or status behavior. The spike records
the exact API version, scopes, draft status semantics, rate limits, pagination,
and read-back behavior.

### Xero Failure Handling

- OAuth expiry or revoked tenant: block new runs and request reconnection.
- Delayed or failed Xero source sync: block until a successful sync is observed.
- Tenant mismatch: fail closed; never process the returned tenant.
- API rate limit: bounded retry with provider-respecting backoff.
- Draft creation timeout: search by the exact stored narration/marker before
  retry; a malformed, duplicated, or altered draft is an `action_failed` state.
- Unknown or ambiguous search result: mark the action outcome unknown and stop
  automatic retry until an operator can reconcile the provider state.
- Read-back mismatch: mark action failed and require controller review.

## 3. United States Banking: Plaid Transactions

### Demo Setup Checklist

- Create Plaid Sandbox credentials and use a Sandbox Link token.
- Use a Sandbox Item configured for Transactions and the scenario bootstrap's
  supported custom or dynamic test user.
- Configure the demo webhook URL and verification/replay checks.
- Store the Sandbox access token in the demo secret store; store only opaque
  Item/account references in the demo database.
- Generate the manifest-defined synthetic period and record the Item, accounts,
  cursor, scenario version, and provider request IDs.

### Production Setup Checklist

- Create a Plaid production application with Transactions access.
- Configure the production webhook URL and signature verification.
- Use Plaid Link for customer account connection and consent.
- Store Plaid access tokens in the secret manager; store only Item/account
  references in PostgreSQL.
- Record the selected accounts, institution metadata, currency, and timezone.
- Confirm the production institution supports the required transaction history
  and refresh behavior before onboarding the pilot.

### Runtime Flow

1. Link creates or updates the customer connection.
2. The webhook receiver validates the event and stores a deduplicated receipt.
3. The bank adapter calls cursor-based transaction synchronization until the
   provider cursor is current.
4. The adapter persists pending and posted transactions separately.
5. The close workflow requests refresh only when the production account has that
   capability; otherwise it waits for the latest webhook update.
6. The snapshot records Item ID, account IDs, cursor, transaction range, and
   completion time.

In demo mode, refresh and transaction generation use Sandbox capabilities. The
workflow never presents Sandbox data as a live bank balance or production
statement.

### Plaid Controls

- Pending transactions cannot satisfy a final close match without an explicit
  organization policy.
- A removed, revoked, or errored Item blocks the selected account.
- Webhook retries and duplicate events are safe through provider event identity
  and cursor idempotency.
- The product never sends payments or changes bank instructions through Plaid.

## 4. India Banking: Setu Account Aggregator

### External Go-Live Requirement

India production access is not merely an API key. The company must be eligible
as a Financial Information User and complete the applicable Account Aggregator
ecosystem onboarding/certification, or contract with an already-certified FIU
partner. The selected bank must participate as a supported Financial Information
Provider for the pilot accounts.

This gate is owned by the business/compliance team. Engineering cannot bypass it
with code.

### Setup Checklist

- Execute a Setu production agreement and obtain FIU credentials.
- Complete required Sahamati/ReBIT certification or document the certified FIU
  partner relationship.
- Configure FIU webhook URLs and signature verification.
- Define consent purpose, data types, selected account types, date range, and
  consent duration in the organization setup.
- Store consent and API secrets in the secret manager.
- Confirm each pilot bank/FIP is supported and returns the required account
  summary and transaction information.

### Runtime Flow

1. Create a consent request with the accounting-close purpose and requested date
   range.
2. Present the consent journey to the customer and record approval/rejection.
3. Start the data-fetch session only after consent is approved.
4. Validate `FI_DATA_READY` notifications and deduplicate them by provider event
   identity/consent/session.
5. Accept data only when all selected accounts are delivered successfully for the
   requested range.
6. Store consent ID, session ID, FIP IDs, masked account references, range,
   status, payload hash, and delivery timestamps.

### Setu Controls

- `PARTIAL`, failed, expired, or revoked delivery blocks the run.
- Consent must cover the requested period and account type.
- No customer profile data is passed to AI when transaction facts are sufficient.
- Raw financial information is encrypted and retained only under the approved
  policy.
- The product never initiates payments or changes account instructions.

## 5. Google Drive and Gmail

### Demo Setup Checklist

- Use a Google test Workspace and a demo-only OAuth application/callback.
- Select only test Drive folders and a test Gmail mailbox.
- Allowlist test recipients/domains and keep attachments synthetic.
- Store refresh tokens only in the demo secret store.

### Production Setup Checklist

- Create a Google OAuth application with verified redirect URIs.
- Request the minimum Drive and Gmail scopes needed for selected folders,
  labels, search, draft creation, and policy-approved send.
- Let the controller choose the evidence folders and mailbox labels.
- Store Google refresh tokens in the secret manager.
- Configure a real sender identity and approved recipient/domain list.

### Evidence Flow

- Search is constrained by folder, label, date, sender, and configured account.
- Every result records Google file/message ID, modified/internal timestamp,
  owner/sender, content hash, and connection ID.
- Attachments are fetched only for the current task and are scanned/parsed before
  model access.
- A missing-document request first creates a Gmail draft with an action marker.
- The policy gateway reevaluates recipient and template restrictions immediately
  before sending.
- The sent message ID and thread ID are audited.

## 6. Owned MCP Deployment

The project owns and deploys the MCP servers. Public or unreviewed MCP servers
must not receive customer credentials or financial data.

Every request includes:

- authenticated user and organization
- provider connection ID
- close run, task, and approval context
- requested tool and validated JSON input
- policy decision and idempotency key

The MCP server must validate tool input, enforce access controls, rate-limit
requests, sanitize outputs, and emit an audit event. The model requests a tool;
the policy gateway decides whether it is allowed.

## 7. Secret and Callback Requirements

Required secret classes:

- Supabase Auth project configuration for controller login.
- Xero client secret and refresh tokens.
- Credentials for the approved Xero ingestion implementation.
- Plaid client/access tokens.
- Google OAuth client secret and refresh tokens.
- Groq API key.
- B2 credentials.

Required callback protections:

- HTTPS only.
- OAuth state, PKCE, and redirect URI validation; issuer/nonce validation for
  provider flows that use OpenID Connect.
- Provider webhook signature validation.
- Timestamp/replay window and event-identity deduplication.
- Request ID and provider event ID in logs without raw payload leakage.

## 8. Regional Deployment Requirements

```text
US: accounts-us.example.com
    US Postgres + raw_xero/raw_bank_us + US B2 + US secrets

TEST: accounts-test.example.com
      Test Postgres + raw_xero_demo/raw_bank_demo + Test B2 + Test secrets
```

The same application version is deployed to both stacks. Test and production
provider credentials, webhooks, source destinations, databases, and artifacts
do not cross stacks. Production hosting,
B2 location, Groq processing, retention, and cross-border transfer settings
must be approved for the applicable market; the design does not assume every
vendor offers an India storage region.

## 9. Operational Runbooks

### Stale Xero Source Data

1. Show the configured source connection ID, last success, affected tables, and warning.
2. Request or resume the approved incremental sync.
3. Wait for verified completion and rerun normalization.
4. Create a new source snapshot; never mutate an approved snapshot.

### US Bank Refresh Failure

1. Show Plaid Item/institution and provider error class without exposing tokens.
2. Ask the controller to reconnect or wait for provider recovery.
3. Keep the run blocked; do not reconcile from stale data.

### India Consent or FIP Failure

1. Show consent/session/FIP and affected masked account.
2. Request consent renewal or remove the affected account from a new setup
   version.
3. Do not mark the close ready while selected accounts are partial.

### Xero Draft Write Failure

1. Keep the approval decision and package immutable.
2. Query Xero for the proposal marker.
3. Retry only if no identical draft exists.
4. If the query is unavailable or cannot prove absence, mark the outcome unknown
   and do not retry automatically.
5. If an inconsistent draft exists, stop and require manual controller review.

### Provider Revocation

1. Verify the provider webhook or failed API response.
2. Mark the connection revoked.
3. Block new and in-flight tasks requiring that connection.
4. Retain the audit event and allow the controller to reconnect.

## 10. Production Go-Live Checklist

- Xero production app, tenant, scopes, and draft-journal capability verified.
- Supabase Auth issuer, audience, callbacks, logout, session expiry, and
  organization-membership authorization verified in both market stacks.
- Approved Xero ingestion path, destination permissions, incremental sync, and
  completion event verified.
- US Plaid production access and pilot institution verified.
- Google OAuth verification and sender/domain policy completed.
- B2 buckets, lifecycle, signed URLs, deletion policy, and Object Lock retention
  for approved review packages/action manifests configured and tested.
- Market data-processing, retention, vendor, and cross-border transfer policies
  approved for PostgreSQL, B2, Groq, and provider data.
- Secret store, key rotation, callback domains, and alerting configured.
- Provider outage, token expiry, consent revocation, webhook replay, and action
  retry runbooks rehearsed.
- At least one US production close run passes all acceptance criteria in `PRD.md`.
- No test connector, fixture namespace, local artifact store, or prohibited MCP
  tool is present in either production deployment.
