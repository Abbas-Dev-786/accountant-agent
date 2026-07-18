# AccountingOS Demo MVP Narrative

**Version:** 1.3
**Status:** Approved for synthetic demo acceptance  
**Rule:** Demo records are synthetic, but provider calls and approval/action
paths are real. The demo never touches a live customer organization.

The demo uses an isolated US deployment, a designated Xero Demo Company, Plaid
Sandbox, and a Google test Workspace. All displayed business values come from a
versioned synthetic scenario bootstrapped into Plaid and Workspace and verified
against the prepared Xero baseline. The UI must show
`DEMO — SYNTHETIC DATA` throughout the run.

## Demo Preparation

Before presenting, verify rather than fabricate:

- Xero Demo Company and Plaid Sandbox connections are healthy.
- The configured provider environments are `demo`/`sandbox`; no production
  credential, tenant, Item, callback, or artifact is reachable.
- The versioned scenario bootstrap seeded Plaid and Workspace records, verified
  the prepared Xero baseline, and recorded all expected provider identifiers.
  Xero reset, when needed, was completed through the explicit operator runbook.
- Test Drive, Gmail, B2, and OpenAI connections are healthy.
- The manifest-defined synthetic period contains enough activity to demonstrate the
  workflow.
- The controller is present and authorized to make the exact approvals required
  during the acceptance run. Journal proposals are never pre-approved before the
  reviewed package and proposal hashes exist.

If the seeded scenario contains no missing evidence or reconciliation exception,
do not manufacture one. Use another versioned scenario or present the successful
no-exception path.

## Scene 1: Connected Organization

The controller opens the organization workspace. The connection panel shows
the demo environment, provider environments, and configured scopes.

Example shape, populated from provider status:

```text
Organization: <Xero Demo Company>
Mode: DEMO — SYNTHETIC DATA
Market: United States
Functional currency: USD

Xero Demo Company     Healthy   Source: direct demo adapter
Plaid Sandbox         Healthy   Cursor: <provider cursor>
Google test Workspace Healthy  Scope: <configured folders>
Gmail test mailbox    Healthy   Sender: <connected account>
Demo B2                Healthy   Bucket: <configured value>
OpenAI                Healthy   Approved model/policy: <configured value>
```

The controller selects the seeded rolling period and clicks `Prepare close
package`.

## Scene 2: Demo Preflight and Synchronization

The UI displays persisted provider progress events:

```text
Validating Xero Demo Company and permissions
Reading Xero through the direct demo adapter
Synchronizing Plaid Sandbox transactions and cursor
Checking Google test Workspace and Gmail access
```

The plan cannot proceed until every required provider is current and complete.
The screen shows provider request IDs, timestamps, source watermarks, and any
provider warning.

If a source is stale, revoked, partial, or delayed, demonstrate the provider
blocker and remediation instead of continuing with cached or local data.

## Scene 3: Source Snapshot and Readiness

After synchronization, AccountingOS freezes a read-only snapshot of the
synthetic provider records used by the run.

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

The controller opens a seeded missing-document item. AccountingOS shows where it
searched and why the checklist still considers the document absent.

If the configured recipient and template satisfy policy, the system creates and
sends a policy-approved request to the test mailbox. The timeline records the
actual Gmail message/thread ID. Otherwise, the controller approves the message
before it is sent.

### When Evidence Is Complete

The system says the evidence check passed and proceeds. It does not create a
fake request for demo effect.

## Scene 4: Demo Reconciliation

The reconciliation screen shows the selected Sandbox bank and Xero Demo ledger
accounts, source balances, transaction counts, match groups, pending items,
excluded items, and exceptions.

```text
Bank provider/account: <Sandbox masked account>
Xero ledger account: <Demo Company account name/code>
Posted bank transactions: <actual count>
Matched: <actual count>
Exceptions: <actual count>
```

The UI explains deterministic match rules. It does not describe AI confidence as
an accounting control.

## Scene 5: Grounded Exception

When a seeded exception exists, the controller opens it and sees:

- Synthetic bank and Xero records with real provider identifiers.
- Related invoice, payment, email, or document evidence.
- AI-written cause and recommendation.
- Exact evidence citations and uncertainties.
- Deterministic checks for amount, account, date, and journal balance.

If no exception exists, the product says all selected transactions are explained.
It does not insert an artificial mismatch.

## Scene 6: Real Close Package

AccountingOS calculates the package from the frozen synthetic snapshot:

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

## Scene 8: Demo Xero Draft Creation

After approval, AccountingOS creates the approved manual journals in the Xero
Demo Company with status `DRAFT`.

The UI shows real provider confirmation:

```text
Xero journal ID: <real Demo Company identifier>
Status: DRAFT
Read-back verification: Passed
Posting action: Not available
```

If Xero fails or the read-back differs, the package remains approved but the run
enters `Action failed`. If the provider outcome is unknown, the system stops
automatic retry until reconciliation proves whether a draft exists. It never
claims success prematurely or risks a duplicate merely because the local result
is missing.

## Scene 9: Approved Demo Package

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

## Live Truth Rules

- Show only actual demo-provider data and actual provider status.
- Do not preload an expected missing document, transaction count, exception, or
  report result.
- Do not use a local fallback if a demo provider is unavailable.
- Show actual external sync time separately from product processing time.
- Show masked bank identifiers and minimum necessary personal information.
- Clearly distinguish deterministic controls from AI narrative.
- Do not expose chain-of-thought.
- Do not send an email or create a Xero draft without the configured policy or
  controller authorization.
- Never describe a `DRAFT` journal as posted.
- Never describe an approved package as a closed/locked accounting period.

## Demo Failure Is a Product State

The demo must remain truthful under provider failure:

- Xero demo adapter delayed: show the sync blocker.
- Plaid item error: show reconnect/wait guidance.
- Scenario bootstrap partial or Xero-baseline mismatch: show the affected
  provider record and remediation.
- Google token expired: show reconnection.
- OpenAI unavailable: preserve deterministic work and show AI task failure.
- Xero write failed: preserve approval, reconcile possible prior creation, and
  permit retry only after the outcome is known.

A well-explained provider blocker demonstrates reliability more credibly than a
fake successful workflow. Production/live acceptance uses the separate live
narrative and provider gates in `PRD.md` and `live_integrations.md`.
