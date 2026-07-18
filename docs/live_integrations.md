# Demo and Live Integration Specification

**Version:** 1.2  
**Status:** Required companion to `PRD.md` and `TDD.md`  
**Rule:** Demo runs use isolated synthetic providers; production and live
acceptance runs use live authenticated providers only.
**Decision date:** 2026-07-18

This document records what must be configured, verified, and operated for each
provider. It is intentionally explicit about external dependencies that the
application team cannot solve in code.

## 1. Provider Matrix

| Capability | Demo US | Production US | Production India | Write capability |
| --- | --- | --- | --- | --- |
| Accounting | Xero Demo Company + direct adapter | Xero + Fivetran | Xero + Fivetran | Xero draft journal through owned MCP |
| Bank transactions | Plaid Sandbox | Plaid Transactions | Setu Account Aggregator | Read-only |
| Evidence | Google test Workspace | Google Drive/Gmail | Google Drive/Gmail | Allowlisted Gmail requests |
| Artifacts | Demo B2 bucket | Backblaze B2 | Backblaze B2 | Upload/download only |
| AI | OpenAI with synthetic evidence | OpenAI | OpenAI | No provider permissions |

The deployment environment and organization market determine the adapter. A
connection for the wrong environment, tenant, Item, or currency is rejected
during onboarding. Demo credentials and artifacts cannot be used by production.

## 1A. Demo Stack

The demo stack is a separate deployment with its own database, secret store,
B2 bucket, OAuth callbacks, webhook URL, and provider credentials. It uses
Plaid Sandbox, a Xero Demo Company through `XeroDirectDemoAdapter`, a Google
test Workspace, a demo B2 bucket, and OpenAI with synthetic evidence only.

Fivetran, Setu, Plaid Production, and real customer Xero organizations are not
part of the demo. A versioned scenario manifest seeds or verifies coherent
records across the demo providers. A partial seed blocks the run.

## 2. Xero and Fivetran

### Roles

In the later production deployment, Fivetran is the ingestion path for Xero
history and incremental changes. It lands provider data in a PostgreSQL
`raw_xero` schema that the application can read but not mutate.

The demo direct adapter and the owned Xero MCP server are the controlled API
paths for the Demo Company. The production MCP server is the controlled API path
for:

- Tenant identity and source control totals.
- Bounded reads needed for freshness or read-back verification.
- Creating an approved manual journal with status `DRAFT`.

Fivetran must never be treated as a write-back or journal-posting mechanism.

### Demo Xero Setup

- Register a non-production Xero app and callback for the demo stack.
- Connect only the designated Demo Company/test organization.
- Record the provider environment as `demo` and reject production tenants.
- Verify account codes, pagination, rate limits, draft status semantics,
  deterministic proposal markers, and line-by-line read-back.
- Run the scenario seeder and verify every expected provider identifier before a
  close run can start.

### Demo Runtime Checks

1. The selected Xero tenant is the designated Demo Company.
2. The scenario version and provider IDs match across Xero, Plaid, and the test
   Workspace.
3. The direct adapter completed its bounded reads and recorded a source
   watermark.
4. No production credential, tenant, Item, callback, or artifact is reachable.

### Production Setup Checklist

- Register a Xero production app and configure OAuth callback URLs for US and IN
  deployments.
- Request only the scopes required for organization, accounting reads, and
  approved draft-journal creation.
- Complete the OAuth authorization-code flow with state, PKCE, redirect URI, and
  tenant-selection validation. Validate issuer and nonce only if the selected
  provider flow uses OpenID Connect.
- Create a Fivetran Xero connection using the selected tenant/organization.
- Configure the correct PostgreSQL destination and `raw_xero` schema.
- Enable incremental updates and Fivetran sync completion webhooks.
- Record Fivetran connection ID, tenant ID, destination schema, connection owner,
  sync frequency, and alert thresholds.
- Store Xero and Fivetran credentials only in the secret manager.

### Production Runtime Checks

Before a run:

1. Fivetran connection setup state is connected and schema status is ready.
2. The selected Xero tenant ID equals the configured tenant.
3. The latest sync completed after the run's sync request, or the configured
   freshness policy explicitly accepts the latest completed sync.
4. No sync warning affects a required table.
5. Xero direct control totals agree with the normalized Fivetran data.

After an approved journal write:

1. The policy gateway confirms the controller approval and exact proposal hash.
2. The MCP server queries for an existing AccountingOS proposal marker before
   creating anything.
3. The journal request sets status to `DRAFT`.
4. The returned journal identifier is persisted.
5. The journal is read back and compared line-by-line with the approved proposal.

The implementation must run a capability spike against the target Xero plan and
production app before promising any field or status behavior. The spike records
the exact API version, scopes, draft status semantics, rate limits, pagination,
and read-back behavior.

### Xero Failure Handling

- OAuth expiry or revoked tenant: block new runs and request reconnection.
- Fivetran delayed/failed sync: block until a successful sync is observed.
- Tenant mismatch: fail closed; never process the returned tenant.
- API rate limit: bounded retry with provider-respecting backoff.
- Draft creation timeout: search by deterministic proposal marker before retry.
- Unknown or ambiguous search result: mark the action outcome unknown and stop
  automatic retry until an operator can reconcile the provider state.
- Read-back mismatch: mark action failed and require controller review.

## 3. United States Banking: Plaid Transactions

### Demo Setup Checklist

- Create Plaid Sandbox credentials and use a Sandbox Link token.
- Use a Sandbox Item configured for Transactions and the scenario seeder's
  supported dynamic test user.
- Configure the demo webhook URL and verification/replay checks.
- Store the Sandbox access token in the demo secret store; store only opaque
  Item/account references in the demo database.
- Generate a rolling synthetic period and record the Item, accounts, cursor,
  scenario version, and provider request IDs.

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

- Managed OIDC client secret/signing configuration for controller login.
- Xero client secret and refresh tokens.
- Fivetran API credentials.
- Plaid client/access tokens.
- Setu FIU credentials and consent/session secrets.
- Google OAuth client secret and refresh tokens.
- OpenAI API key.
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
DEMO: accounts-demo.example.com
      Demo Postgres + raw_xero_demo/raw_bank_demo + Demo B2 + Demo secrets

US: accounts-us.example.com
    US Postgres + raw_xero/raw_bank_us + US B2 + US secrets

IN: accounts-in.example.com
    IN Postgres + raw_xero/raw_bank_in + IN B2 + IN secrets
```

The same application version is deployed to all stacks. Demo and production
provider credentials, webhooks, Fivetran destinations, databases, and artifacts
do not cross stacks. The labels `DEMO`, `US`, and `IN` identify deployment
stacks. Production hosting,
B2 location, OpenAI processing, retention, and cross-border transfer settings
must be approved for the applicable market; the design does not assume every
vendor offers an India storage region.

## 9. Operational Runbooks

### Stale Xero/Fivetran Data

1. Show the Fivetran connection ID, last success, affected tables, and warning.
2. Request or resume an incremental sync.
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
- Managed OIDC issuer, audience, callbacks, logout, session expiry, and
  organization-membership authorization verified in both market stacks.
- Fivetran Xero connector, destination permissions, incremental sync, and
  `sync_end` event verified.
- US Plaid production access and pilot institution verified.
- India Setu production/FIU/Sahamati gate passed and pilot FIP verified.
- Google OAuth verification and sender/domain policy completed.
- B2 buckets, lifecycle, signed URLs, deletion policy, and Object Lock retention
  for approved review packages/action manifests configured and tested.
- Market data-processing, retention, vendor, and cross-border transfer policies
  approved for PostgreSQL, B2, OpenAI, and provider data.
- Secret store, key rotation, callback domains, and alerting configured.
- Provider outage, token expiry, consent revocation, webhook replay, and action
  retry runbooks rehearsed.
- One US and one India live close run passes all acceptance criteria in `PRD.md`.
- No test connector, fixture namespace, local artifact store, or prohibited MCP
  tool is present in either production deployment.
