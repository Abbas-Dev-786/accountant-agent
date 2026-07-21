# DEBUG REPORT — Xero, Plaid, and Google onboarding returns 503

- **Symptom:** Authenticated requests to the Xero authorization URL, Plaid Link
  token URL, and Google authorization URL returned HTTP 503. Their CORS
  preflight requests returned 200.
- **Root cause:** This is provider configuration drift, not CORS. The API builds
  each provider client at startup and disables it when its configuration is
  invalid. Direct validation found that the Xero and Plaid client-secret
  environment values are raw values, but the application requires `secret://`
  references to named Supabase Vault secrets. Google has a placeholder client
  ID. The Xero and Google callback URLs plus the Plaid webhook URL still use
  template placeholders; the existing structural validators accept those HTTPS
  placeholders, but the OAuth providers will reject them. Xero's required
  production tenant allow-list is also missing.
- **Evidence:** The three routes return 503 only when their in-memory client is
  `None`. Startup builders return `None` when their respective configuration
  constructors fail. The direct constructors reported: Xero client secret must
  be a `secret://` reference; Plaid client secret must be a `secret://`
  reference; Google client ID must be configured. The preflight 200 responses
  show that local browser-to-API CORS is functioning.
- **Required operator actions:** Create named Vault secrets for the Xero,
  Plaid, and Google client secrets; put only their `secret://` references in
  `backend/.env`; replace callback/webhook template URLs with provider-registered
  values; configure the Xero tenant allow-list; then restart the API. Provider
  portal credentials and tenant identity must be supplied by the operator and
  cannot be safely invented by an agent.
- **Regression test:** Not added or run at the user's explicit request not to
  rely on test cases.
- **Status:** DONE_WITH_CONCERNS — the diagnosis is confirmed, but provider
  account setup and private credentials are required before live verification.
