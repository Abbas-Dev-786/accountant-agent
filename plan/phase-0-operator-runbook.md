# Phase 0 Operator Runbook

This runbook produces the evidence required to complete Phase 0. It is not a
substitute for provider configuration: each item must be verified against the
isolated demo environment with actual provider request IDs.

## 1. Create the isolated demo environment

Create separate demo credentials, callbacks, secret references, database, and
B2 bucket. Never reuse a production tenant, Item, callback, bucket, or OAuth
client.

Populate the non-secret identifiers in `backend/.env` from
[`backend/.env.example`](../backend/.env.example). Do not commit this file.

## 2. Record the prepared Xero baseline

For the standard Xero OAuth/Auth Code app, authorize the isolated Demo Company
using a one-time state/PKCE transaction and the exact granular scope profile in
[`docs/live_integrations.md`](../docs/live_integrations.md). Store
the client secret and rotating refresh token through the server-side secret
manager. Do not place either token in browser variables or evidence files. The
resulting access token is used to call `/connections`; select the returned
`tenantId` before calling tenant-scoped Accounting API endpoints.

Use the designated Xero Demo Company to collect the tenant ID, required account
codes, and provider IDs. Write the observation in the shape shown by
[`example-xero-baseline.json`](../backend/evidence/example-xero-baseline.json).

Calculate the fingerprint locally before setting the environment value:

```sh
.venv/bin/python -m app.capability_check \
  --xero-baseline /secure/path/xero-baseline.json \
  --print-xero-fingerprint
```

Copy the returned fingerprint into
`ACCOUNTINGOS_XERO_DEMO_BASELINE_FINGERPRINT`. Keep the baseline observation in
the approved private demo-evidence location, not in this repository.

If the Demo Company must be reset, do it through the Xero operator interface.
Recollect and approve the baseline before presenting another close run.

## 3. Collect provider capability evidence

For Xero, prove OAuth, tenant identity, pagination, required account codes,
manual journal `DRAFT` creation/read-back, narration-marker lookup, and rate
limit behavior.

For Plaid, prove Sandbox Transactions, cursor sync, added/modified/removed
changes, pending-to-posted behavior, webhook replay, and an Item error.

For the Google test Workspace, prove scoped Drive search, Gmail draft creation,
and a send only to an allowlisted test recipient. For B2, prove Object Lock and
signed-object retrieval. For OIDC and OpenAI, prove the demo-only configuration
and a non-secret request.

Create an evidence JSON file following
[`example-capability-evidence.json`](../backend/evidence/example-capability-evidence.json).
Every provider entry requires an evidence reference and at least one real request
or event ID.

## 4. Validate readiness

Run the verifier from `backend/`:

```sh
.venv/bin/python -m app.capability_check \
  --scenario scenarios/demo-scenario-v1.json \
  --xero-baseline /secure/path/xero-baseline.json \
  --evidence /secure/path/capability-evidence.json
```

Expected success output includes `"ready": true`, `"mode": "demo"`, and
`"data_class": "synthetic"`.

## 5. Attach phase evidence

Attach the verifier output, provider evidence references, baseline approval,
scope grants, scenario ID/version, and reset runbook to the Phase 0 issue.
Phase 0 is complete only when all entries pass. A placeholder, Sandbox default,
or local fixture does not count as provider capability evidence.
