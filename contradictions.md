# Documentation Contradictions: Final Decision Ledger

**Revalidated:** 2026-07-18  
**Current specification:** Demo MVP and live expansion v1.3
**Demo market:** United States  
**Later live markets:** United States and India

## Current Product Decision

AccountingOS first prepares a close package from synthetic records in isolated
provider environments. The demo uses a Xero Demo Company through a direct
adapter, Plaid Sandbox, a Google test Workspace, demo B2, and OpenAI with
synthetic evidence. The later live product uses Xero/Fivetran, Plaid Production
for the US, Setu Account Aggregator for India, production Google, B2, and OpenAI.

Production and live acceptance runs contain no fixture, dummy, generated,
replayed, or simulated business data. Demo runs use only explicitly labeled
synthetic provider data. If a provider is stale, partial, revoked, or
unavailable, the workflow blocks and reports the actual condition in either mode.

After explicit controller approval, AccountingOS may create balanced manual
journals in Xero with status `DRAFT`. It cannot post journals, move money, or
lock an accounting period.

## Decisions That Supersede the Fixture MVP

### 1. Xero Replaces QuickBooks

QuickBooks is removed from the MVP. Fivetran ingests live Xero data into
PostgreSQL. An owned Xero MCP server performs bounded verification reads and
creates controller-approved draft journals through the Xero API.

### 2. Bank Integrations Are Regional

- United States: Plaid Transactions.
- India: Setu Account Aggregator.

The two adapters normalize into one canonical bank model but preserve provider
IDs, consent, account, timestamp, and currency provenance.

### 3. Fivetran Is Ingestion, Not an Action Layer

Fivetran owns the `raw_xero` ingestion schema and exposes connection/sync status.
It does not send email, write journals, post transactions, or move money.

### 4. MCP Is a Controlled Interface, Not Authorization

The project owns its MCP servers. Provider OAuth, organization/user identity,
tool allowlists, policy checks, typed inputs, rate limits, output sanitization,
and audit events remain mandatory. Public MCP servers do not receive customer
credentials or financial data.

### 5. External Actions Have Explicit Policies

- Live reads are allowed within configured source scope.
- Missing-document email may auto-send only to allowlisted recipients using an
  approved low-risk template; all other email requires controller approval.
- Xero draft creation always requires controller approval tied to the exact
  package and proposal hash.
- Journal posting, payment activity, deletion, voiding, and period locking are
  absent from the tool registry.

### 6. Tests and Production Have a Hard Boundary

Production and live acceptance are live-only. The demo is a separate synthetic
deployment using Plaid Sandbox and a Xero Demo Company. Unit and integration
tests still use generated records and isolated provider test organizations.
Production configuration rejects every test namespace, tenant, connector, and
local store; demo configuration rejects live credentials.

## Revalidation of the Original 12 Findings

All original findings remain valid as architectural lessons, but their
fixture-specific resolutions are superseded.

| Original finding | v1.2 resolution |
| --- | --- |
| MVP overclaimed a completed close | Product prepares a package and creates approved Xero drafts; it does not post or lock the period |
| Phase boundaries conflicted | One fixed live workflow and logical executors; no agent-count target |
| Completion preceded approval | `awaiting_approval` precedes approved actions and final `approved` |
| State machines were linear/invalid | Conditional sync, blocked, input, approval, action-failure, retry, and cancellation paths |
| Inputs could not produce reports | Versioned live Xero/bank/document snapshot with source totals and watermarks |
| Confidence was treated as verification | Deterministic reconciliation, journal, trial-balance, accounting-equation, and cash controls |
| Runtime was overbuilt/underspecified | PostgreSQL authority, explicit provider sync, leases, idempotency, webhooks, SSE replay, and recovery |
| AI trust boundary was missing | Bounded evidence, owned MCP, policy gateway, prompt-injection controls, and fail-closed validation |
| Demo data contradicted itself | Versioned synthetic scenario; all values come from the selected demo snapshot |
| Initial screen revealed future results | Readiness remains unknown until live synchronization and inventory finish |
| Real versus mock was undefined | All production providers and actions are named, authenticated, and audited |
| Audit data was incomplete | Provider IDs, watermarks, model/tool versions, approvals, action results, and read-back verification |

## Live Architecture Resolution

```text
Live Xero -> Fivetran -> raw_xero -> normalization --+
                                                     |
US Plaid or India Setu -> raw bank -> normalization -+--> immutable run snapshot
                                                     |             |
Drive/Gmail live evidence ---------------------------+             v
                                                              deterministic controls
                                                                    + grounded AI
                                                                          |
                                                                          v
                                                               controller approval
                                                                          |
                                                                          v
                                                              frozen review package
                                                                          |
                                                                          v
                                                           Xero DRAFT journal creation
                                                                          |
                                                                          v
                                                        read-back + action manifest
```

## Demo-First Architecture Resolution

The first implementation is an isolated US demo stack. It exercises real
provider calls and action controls with synthetic records:

```text
Plaid Sandbox + Xero Demo Company + Google test Workspace
                         |
              versioned scenario bootstrap
                         |
                 canonical SourceBatch
                         |
                 immutable demo snapshot
                         |
              fixed workflow + deterministic controls
                         |
                controller approval and package freeze
                         |
               Xero Demo Company DRAFT + read-back
```

`XeroDirectDemoAdapter` and `PlaidSandboxAdapter` implement the same source
contract that later production adapters use. The demo bootstrap seeds Plaid and
the test Workspace, then verifies a prepared Xero Demo Company baseline; a Xero
reset is an explicit operator runbook step. Demo and production have separate
databases, secrets, callbacks, artifacts, and environment guards. Fivetran,
Setu, Plaid Production, and real customer organizations remain later live
milestones.

## External Release Gates

These are not coding tasks and must not be represented as already solved:

- Xero production app, scopes, tenant, and draft-journal capability verification.
- Fivetran production Xero connector and PostgreSQL destination.
- Plaid Transactions production access and supported US pilot institution.
- Setu production agreement plus FIU/Sahamati certification or a certified FIU
  partner, and a supported India pilot FIP.
- Google OAuth verification for the required Drive/Gmail scopes.
- Real US and India pilot organizations and controller authorization.

India production onboarding is likely the longest external dependency and
should begin during Phase 0, not after the application is built.

## Document Disposition

| Document | Role |
| --- | --- |
| `docs/PRD.md` | Live product source of truth |
| `docs/live_integrations.md` | Provider and go-live source of truth |
| `docs/TDD.md` | Live technical source of truth |
| `docs/user_story.md` | Truthful live acceptance narrative |
| `docs/README.md` | Authority and reading order |
| `docs/vision_product_strategy.md` | Long-term strategy |
| `docs/workflow_bible.md` | Broad accounting workflow reference |
| `docs/agent_bible.md` | Future-state domain reference |
| `docs/system_architecture.md` | Superseded pointer |

## Final Cross-Document Consistency Pass

The final pass resolved the remaining implementation-level conflicts:

- Xero direct control-total verification now depends on completion of the fresh
  Fivetran sync and normalization instead of racing it.
- Approval freezes the exact reviewed package before Xero writes. Read-back
  results create a separate action manifest rather than mutating approved content.
- Snapshots reference immutable normalized record versions or permitted copies,
  never only mutable provider/raw rows plus a hash.
- Gmail and Xero actions use deterministic keys and provider reconciliation for
  effectively-once behavior. Ambiguous outcomes stop automatic retry instead of
  making an impossible strict exactly-once promise.
- OAuth nonce checks are limited to OpenID Connect flows, and B2 Object Lock plus
  market-specific data-processing/retention approval are explicit release gates.
- Controller authentication now has a fixed managed-OIDC protocol and
  organization-membership boundary; only the provider vendor remains a Phase 0
  selection.
- Fixture-era claims about one expected exception, dynamic MVP planning, and the
  first component to build were removed from the supporting workflow document.

## Build-Readiness Amendments

The v1.3 amendment resolves the remaining build-blocking ambiguities without
expanding product scope:

- Deployment mode and data class are immutable deployment configuration, never
  mutable organization attributes; they are copied into server-created runs.
- The demo bootstrap seeds Plaid and the test Workspace, but verifies a prepared
  Xero Demo Company baseline. Xero reset is an explicit operator runbook step.
- Snapshots now name immutable normalized record versions, source batches,
  atomic membership/cutoff semantics, and the constraints required for
  reproducibility.
- Xero draft creation now has a concrete server-generated narration marker,
  stored request hash, exact-marker lookup, and altered-draft failure path.
- The exact granular Xero scope profile is defined, and the separate Journals
  endpoint is explicitly excluded from the MVP.

## Build Decision

Implementation may begin with the demo capability spikes and demo foundation in
TDD Phase 0 and Phase 1. The product may not be called live in a market until
every provider gate and live acceptance criterion for that market passes.
