# Phase 9 US Production and Groq Runbook

Phase 9 supplies server-side runtime adapters for US production. The adapters
are deliberately transport-injected: unit tests can prove
headers, scopes, cursors, and structured-output behavior without pretending
that a provider account is connected.

## Configuration

Keep these values in the backend environment or a managed secret store only:

```sh
ACCOUNTINGOS_XERO_CLIENT_ID=...
ACCOUNTINGOS_XERO_CLIENT_SECRET_REF=secret://xero/production/client-secret
ACCOUNTINGOS_XERO_REFRESH_TOKEN_SECRET_REF=secret://xero/production/refresh-token
ACCOUNTINGOS_XERO_REDIRECT_URI=http://localhost:8000/api/v1/connections/xero/callback
ACCOUNTINGOS_XERO_SCOPES="offline_access accounting.settings.read accounting.contacts.read accounting.invoices.read accounting.payments.read accounting.banktransactions.read accounting.manualjournals accounting.reports.trialbalance.read accounting.reports.profitandloss.read accounting.reports.balancesheet.read"
GROQ_API_KEY_REF=secret://groq/production/api-key
GROQ_MODEL=openai/gpt-oss-20b
GROQ_TIMEOUT_SECONDS=30
SUPABASE_DB_URL=postgresql://...?sslmode=require
```

Provider connection records contain `secret://...` references, never plaintext
tokens. The browser must not receive any provider secret, database URL, or
Groq key. `NEXT_PUBLIC_GROQ_*` variables are rejected by configuration
validation.

Production source synchronization also requires
`ACCOUNTINGOS_XERO_TENANT_ID` and
`ACCOUNTINGOS_PLAID_SELECTED_ACCOUNT_IDS`. The worker compares every Xero
connection and Plaid transaction to those approved values before it writes a
snapshot; it rejects a mismatch rather than falling back to a fixture source.

For the standard Xero OAuth/Auth Code app, `backend/app/xero_oauth.py` provides
the server-side token boundary. The backend must generate a
one-time state and PKCE verifier, send the user to Xero authorization, exchange
the returned code at `https://identity.xero.com/connect/token`, and persist the
rotating refresh token through the secret manager. After authorization, call
`GET https://api.xero.com/connections` to enumerate the granted tenants. The
callback registers a connection for every granted tenant (multi-tenant);
`ACCOUNTINGOS_XERO_TENANT_ALLOWLIST`, when set, restricts registration to named
tenant ids to the organization-approved production tenant. Use each
tenant ID with the `xero-tenant-id` header for that organization's Accounting
API calls. The exact granular scope profile is maintained in
`docs/live_integrations.md`; onboarding must record and compare the granted
scope set rather than accepting extra permissions. The adapter uses direct
HTTPS API calls; an SDK is not required.

The FastAPI callback boundary is exposed at:

```text
GET /api/v1/organizations/{organization_id}/connections/xero/authorize
GET /api/v1/connections/xero/callback
```

The callback session store is durable when `SUPABASE_DB_URL` is configured:
OAuth transaction state (PKCE verifier + state) is written to the private
`workflow.oauth_sessions` table, so a restart or a second worker does not
invalidate an in-flight authorization. It falls back to a process-local store
only when no database is configured (pure-domain tests).
`XeroBaselineHttpClient` remains a fixture helper. Production onboarding instead
verifies the selected tenant, accounts, scopes, source watermark, and
accounting-mapping configuration.

## Runtime adapters

- Production Xero source reads the approved organization with tenant isolation,
  explicit pagination, and immutable source metadata.
- `PlaidHttpSandboxClient` is fixture-only; production sends server-side
  credentials to Plaid Transactions Sync
  and preserves added/modified/removed changes plus cursors.
- `GoogleDriveHttpClient` limits searches to configured folder IDs and returns
  immutable metadata hashes.
- `GmailHttpClient` limits searches to the configured mailbox and date range;
  the existing evidence collector applies the allowlisted-label policy.
- `GroqExplanationModel` requests strict JSON Schema output and records usage
  metadata. Deterministic citation, amount, date, account, and injection
  checks remain authoritative after the model response.

## Local verification

```sh
cd backend
.venv/bin/python -m unittest discover -s tests -v
```

The tests use fake transports and therefore prove contract behavior only. They
do not count as provider capability evidence.

## Required external evidence before Phase 9 exit

Capture redacted request/event IDs and screenshots or provider exports for:

1. Xero OAuth redirect/state/PKCE validation, approved granular scopes,
   production tenant selection, pagination, control totals, and a read-back
   showing the allowed `DRAFT` action.
2. Plaid Production cursor continuation, added/modified/removed transactions,
   pending-to-posted behavior, webhook replay, and an Item error.
3. Google OAuth scopes, Drive folder restriction, Gmail mailbox/date/label
   restriction, and an allowlisted test draft/send.
4. Groq model/schema acceptance, usage metadata, and a bounded 429/rate-limit
   failure that leaves the explanation blocked rather than fabricated.

Do not mark Phase 9 complete until the evidence is captured with the approved
US production credentials. Missing credentials or provider outages remain visible
blocked states.
