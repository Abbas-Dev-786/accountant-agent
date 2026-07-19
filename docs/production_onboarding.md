# US Production Onboarding Checklist

This is the required setup record for each AccountingOS organization. It turns
provider credentials into a safe, reproducible close configuration. Credentials
alone do not select bank accounts, ledger facts, or journal accounts.

## 1. Deployment boundary

- Register the deployment as `production` / `live` / `US` / `USD`.
- Use a dedicated Supabase project/database, secret store, B2 bucket, callback
  domains, and monitoring for production.
- Keep all financial tables in private schemas. The browser uses only the
  Supabase project URL and publishable key for Auth, then calls FastAPI; it must
  never receive a database URL, secret/service-role key, provider token, or B2
  credential.
- Disable or leave unused the Data API for financial schemas. If a public table
  is introduced later, use explicit grants plus organization-aware RLS.

## 2. Provider evidence

Record redacted evidence for each item before enabling a close:

- Xero production app, tenant ID, exact scopes, pagination, account list,
  control totals, and `DRAFT` manual-journal read-back.
- Approved Xero source implementation, source connection ID, private raw-data
  destination, incremental-sync completion signal, and freshness policy.
- Plaid Production Item, selected account IDs, cursor continuation, webhook
  validation, pending-to-posted behavior, and supported transaction history.
- Google Drive folder IDs, Gmail mailbox/labels, OAuth scopes, and recipient
  allowlist.
- B2 bucket, Object Lock retention period, signed-download policy, and key
  lifecycle.
- Groq model ID, structured-output validation, rate-limit behavior, and
  data-processing approval.

## 3. Accountant-approved close mapping

An accountant/controller records and approves one versioned configuration:

1. Select the Xero tenant and the production Xero source used as the ledger
   side of reconciliation.
2. Select each Plaid account included in the close and map it to the matching
   Xero cash/ledger account.
3. Set the matching rules: date window, fee tolerance, pending policy,
   materiality threshold, and allowed aggregate size.
4. Select the valid Xero account codes and permitted journal types for proposed
   adjustments. These rules do not authorize posting.
5. Define the evidence checklist, Drive/Gmail scope, allowed email recipients,
   approver, and retention-policy version.

Changing any item creates a new configuration version. A close run stores the
exact version it used; later edits cannot alter its snapshot, package, approval,
or action request.

## 4. Release gate

Before the first production close, complete a real, controller-reviewed run:

- source snapshot is complete and fresh;
- reconciliation uses the approved mapping and exposes every exception;
- reports and proposals pass deterministic controls;
- Groq explanations contain only validated citations;
- the controller approves the frozen package; and
- any permitted Xero action creates and reads back a `DRAFT` journal only.

Do not claim production readiness from a sandbox, fixture, mocked transport, or
placeholder credential.
