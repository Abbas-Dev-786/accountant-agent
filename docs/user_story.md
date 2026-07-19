# AccountingOS US Production Controller Narrative

**Version:** 1.3
**Status:** Production-first target narrative
**Rule:** Production records and provider calls are live, while financial actions
remain bounded to approved Xero `DRAFT` journals. Synthetic fixtures are not
customer-facing acceptance evidence.

The product uses a US production deployment, the organization's connected Xero
tenant, selected Plaid Production account(s), and configured Google Workspace
scopes. All displayed values come from the frozen live source snapshot. The UI
must identify the organization, source freshness, and configuration version.

## Production Preparation

Before presenting, verify rather than fabricate:

- The organization's Xero tenant and selected Plaid Production accounts are healthy.
- The configured providers are live production resources; no test credential,
  tenant, Item, callback, artifact, or local store is reachable.
- The accountant-approved configuration version selects the bank accounts,
  Xero ledger source, account mappings, tolerances, evidence scope, and approver.
- Drive, Gmail, B2, and Groq connections are healthy.
- The requested accounting period is open and supported by every selected source.
- The controller is present and authorized to make the exact approvals required
  during the acceptance run. Journal proposals are never pre-approved before the
  reviewed package and proposal hashes exist.

If a close contains no missing evidence or reconciliation exception, do not
manufacture one. Present the successful no-exception path.

## Scene 1: Connected Organization

The controller opens the organization workspace. The connection panel shows
the production environment, provider environments, mapping version, and configured scopes.

Example shape, populated from provider status:

```text
Organization: <organization name>
Mode: PRODUCTION — LIVE DATA
Market: United States
Functional currency: USD

Xero organization      Healthy   Source: <configured ingestion path>
Plaid Production       Healthy   Cursor: <provider cursor>
Google Workspace       Healthy   Scope: <configured folders>
Gmail mailbox          Healthy   Sender: <connected account>
Production B2          Healthy   Bucket: <configured value>
Groq                  Healthy   Approved model/policy: <configured value>
```

The controller selects the accounting period and clicks `Prepare close package`.

## Scene 2: Production Preflight and Synchronization

The UI displays persisted provider progress events:

```text
Validating Xero organization and permissions
Reading Xero through the configured production source
Synchronizing Plaid Production transactions and cursor
Checking Google Workspace and Gmail access
```

The plan cannot proceed until every required provider is current and complete.
The screen shows provider request IDs, timestamps, source watermarks, and any
provider warning.

If a source is stale, revoked, partial, or delayed, demonstrate the provider
blocker and remediation instead of continuing with cached or local data.

## Scene 3: Source Snapshot and Readiness

After synchronization, AccountingOS freezes a read-only snapshot of the
live provider records used by the run.

The readiness view shows actual values:

```text
Xero records included: <actual count>
Bank transactions included: <actual count>
Required documents found: <actual count>
Required documents missing: <actual count>
Snapshot cutoff: <actual provider watermarks>
```

Every count links to source records and provider identifiers.

### When Evidence Is Missing

The controller opens a missing-document item. AccountingOS shows where it
searched and why the checklist still considers the document absent.

If the configured recipient and template satisfy policy, the system creates and
sends a policy-approved request to an allowlisted recipient. The timeline records the
actual Gmail message/thread ID. Otherwise, the controller approves the message
before it is sent.

### When Evidence Is Complete

The system says the evidence check passed and proceeds. It does not create a
fake request for demo effect.

## Scene 4: Production Reconciliation

The reconciliation screen shows the selected production bank and Xero ledger
accounts, source balances, transaction counts, match groups, pending items,
excluded items, and exceptions.

```text
Bank provider/account: <masked selected account>
Xero ledger account: <configured account name/code>
Posted bank transactions: <actual count>
Matched: <actual count>
Exceptions: <actual count>
```

The UI explains deterministic match rules. It does not describe AI confidence as
an accounting control.

## Scene 5: Grounded Exception

When an exception exists, the controller opens it and sees:

- Live bank and Xero records with provider identifiers.
- Related invoice, payment, email, or document evidence.
- AI-written cause and recommendation.
- Exact evidence citations and uncertainties.
- Deterministic checks for amount, account, date, and journal balance.

If no exception exists, the product says all selected transactions are explained.
It does not insert an artificial mismatch.

## Scene 6: Real Close Package

AccountingOS calculates the package from the frozen live snapshot:

- Unadjusted and pro forma adjusted trial balance.
- Pro forma P&L and balance sheet.
- Cash reconciliation.
- Exception schedule.
- Proposed journal drafts, if required.
- Executive summary citing computed fact IDs.
- Source watermarks and change log.

Proposed adjustments are clearly labeled as unposted. Reports that include them
are clearly labeled pro forma.

## Scene 7: Controller Approval

The run pauses at `Awaiting controller approval`.

The controller reviews:

- Provider freshness and snapshot cutoff.
- Evidence and reconciliation controls.
- Every proposed journal line.
- Email actions already taken.
- Reports and executive summary.
- Complete audit timeline.

The controller approves the exact package version and journal proposal hashes.
That approval freezes the reviewed package before any Xero write begins.

## Scene 8: Xero Draft Creation

After approval, AccountingOS creates the approved manual journals in the Xero
connected organization with status `DRAFT`.

The UI shows real provider confirmation:

```text
Xero journal ID: <provider identifier>
Status: DRAFT
Read-back verification: Passed
Posting action: Not available
```

If Xero fails or the read-back differs, the package remains approved but the run
enters `Action failed`. If the provider outcome is unknown, the system stops
automatic retry until reconciliation proves whether a draft exists. It never
claims success prematurely or risks a duplicate merely because the local result
is missing.

## Scene 9: Approved Production Package

After all approved actions are verified, the final screen says:

```text
Close package approved
Source snapshot: <snapshot ID and cutoff>
Reconciliation: <actual result>
Exceptions: <actual result>
Xero drafts created: <actual result>
Package version: <actual version>
```

It does not claim that Xero journals were posted or that the accounting period
was locked or closed.

The final action manifest references the frozen approved package. It does not
replace or mutate the artifact the controller reviewed.

## Production Truth Rules

- Show only actual production-provider data and actual provider status.
- Do not preload an expected missing document, transaction count, exception, or
  report result.
- Do not use a local fallback if a production provider is unavailable.
- Show actual external sync time separately from product processing time.
- Show masked bank identifiers and minimum necessary personal information.
- Clearly distinguish deterministic controls from AI narrative.
- Do not expose chain-of-thought.
- Do not send an email or create a Xero draft without the configured policy or
  controller authorization.
- Never describe a `DRAFT` journal as posted.
- Never describe an approved package as a closed/locked accounting period.

## Production Failure Is a Product State

The production product must remain truthful under provider failure:

- Xero source delayed: show the sync blocker.
- Plaid item error: show reconnect/wait guidance.
- Xero source/control-total mismatch: show the affected provider record and
  remediation.
- Google token expired: show reconnection.
- Groq unavailable: preserve deterministic work and show AI task failure.
- Xero write failed: preserve approval, reconcile possible prior creation, and
  permit retry only after the outcome is known.

A well-explained provider blocker demonstrates reliability more credibly than a
fake successful workflow. Production acceptance uses the provider gates in
`PRD.md` and `live_integrations.md`.
