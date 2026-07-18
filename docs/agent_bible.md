# The Agent Bible v0.1

**Status:** Future-state domain reference. For the demo MVP, agent names are
logical task-owner labels inside the modular monolith. They are not separate
services, queues, model sessions, or unrestricted autonomous employees. The
workflow supervisor is deterministic; only bounded explanation/summary tasks use
the model. `TDD.md` is the implementation authority.

> **Purpose:** Define every AI employee in AccountingOS.

The Agent Bible is **not** an implementation document.

It defines the responsibilities, authority, and collaboration model of every agent.

Think of it as our company's organizational chart.

---

# Design Principles

Before defining agents, let's establish rules that every agent must follow.

## Principle 1 — Single Responsibility

One agent owns one job.

Not ten.

Example:

❌ AP Agent

- Reads invoices
- Sends emails
- Generates reports
- Reconciles banks

Too much.

Instead:

✓ Invoice Intake Agent

✓ Payment Agent

✓ Approval Agent

---

## Principle 2 — Agents Own Outcomes

Not functions.

Example:

Bad

```
Invoice Parser
```

Good

```
Ensure all vendor invoices are ready for posting.
```

Outcome-driven agents are easier to evolve.

---

## Principle 3 — Supervisor Never Does Specialist Work

The supervisor should never parse invoices.

It delegates.

Exactly like a human manager.

---

## Principle 4 — Specialists Don't Micromanage

The AP Agent shouldn't decide when to close the books.

That's the Supervisor's responsibility.

---

# Organization Structure

I think this should look like a real finance organization.

```
Accounting Supervisor
│
├── Operations Team
│
├── Accounting Team
│
└── Intelligence Team
```

Let's define each.

---

# Level 1 — Accounting Supervisor

This is the "Finance Manager."

## Mission

Own successful completion of finance workflows.

Example request

```
Close July.
```

The Supervisor doesn't know every accounting rule.

It knows:

- what needs to happen
- in what order
- who should do it
- when to escalate
- when to stop

---

## Responsibilities

- Build execution plan
- Assign work
- Monitor progress
- Detect blockers
- Prioritize work
- Escalate
- Request approval
- Produce final outcome

---

## Never Does

- OCR
- Invoice extraction
- Reconciliation
- Journal creation

It delegates all of these.

---

# Level 2 — Operations Team

These agents keep work moving.

---

## Planning Agent

Mission

Convert goals into executable plans.

Input

```
Close July
```

Output

```
Execution Plan
```

Responsibilities

- break work into tasks
- determine dependencies
- estimate execution
- prioritize

---

## Document Intake Agent

Mission

Ensure all required documents exist.

Responsibilities

- discover files
- search systems
- classify
- validate
- request missing docs

One of the highest-ROI agents.

---

## Integration Agent

Mission

Maintain connectivity.

Responsibilities

- connect APIs
- authenticate
- retry failures
- sync data

---

## Workflow Tracker

Mission

Know the status of everything.

Questions it should answer instantly:

- What's blocking the close?
- What's left?
- Who owns it?
- ETA?

---

## Notification Agent

Mission

Communicate with humans.

Examples

- reminders
- approvals
- escalations
- summaries

This keeps communication isolated from business logic.

---

# Level 3 — Accounting Team

These agents understand finance.

---

## AP Agent

Mission

Ensure vendor obligations are accurate.

Responsibilities

- vendor invoices
- approvals
- payment readiness
- duplicate detection

---

## AR Agent

Mission

Maintain customer receivables.

Responsibilities

- invoice generation
- payment matching
- collections
- aging

---

## Reconciliation Agent

Mission

Ensure every transaction is explained.

Responsibilities

- match
- investigate
- explain
- resolve

This is a reasoning-heavy agent.

---

## Journal Agent

Mission

Prepare accounting entries.

Not post them automatically.

Prepare.

Humans approve.

---

## Reporting Agent

Mission

Transform accounting data into financial insight.

Outputs

- P&L
- Balance Sheet
- Cash Flow
- Variance Analysis
- Executive Summary

---

# Level 4 — Intelligence Team

This is where a strong reasoning model may provide the most leverage.

---

## Risk Agent

Mission

Predict problems before they happen.

Examples

- missing payroll
- delayed bank feeds
- incomplete documents

Instead of reacting, it forecasts.

---

## Exception Investigator

Mission

Answer

```
Why?
```

Example

Bank mismatch.

Instead of

```
Mismatch found.
```

Return

```
Vendor renamed.

Duplicate refund.

Wrong account.

Suggested correction.
```

This is one of our strongest differentiators.

---

## Memory Agent

Mission

Remember organizational context.

Examples

Vendor A always submits invoices late.

Controller approves after 4 PM.

Payroll arrives on Thursdays.

This enables proactive behavior.

---

## Learning Agent

Mission

Improve future execution.

Examples

- recurring blockers
- approval patterns
- workflow optimization
- automation recommendations

This is long-term intelligence.

---

# Agent Communication

This is a rule that will shape our architecture.

Agents should **never talk directly to every other agent**.

Instead:

```
Supervisor
    │
Task Queue
    │
Specialists
```

Why?

Because direct agent-to-agent communication creates tangled dependencies, makes debugging difficult, and becomes hard to scale.

The Supervisor becomes the orchestrator.

---

# Human-in-the-Loop Rules

Every action falls into one of three categories.

### Autonomous

Low risk.

Examples:

- classify documents
- collect files
- send reminders

For the MVP, `PRD.md` overrides this future-state shorthand: Gmail may send only
an approved missing-document template to an allowlisted recipient under the
configured policy. Every other message requires controller approval.

---

### Approval Required

Medium risk.

Examples:

- journal entries
- reconciliation adjustments
- write-offs

---

### Human Only

High risk.

Examples:

- payment release
- tax filing
- financial statement approval

This framework keeps the product trustworthy.

---

# Agent Scorecard

Every agent should have measurable KPIs.

| Agent                  | Primary KPI                  |
| ---------------------- | ---------------------------- |
| Planning               | Plans completed successfully |
| Document Intake        | Missing documents resolved   |
| AP                     | Invoice processing accuracy  |
| Reconciliation         | Transactions reconciled      |
| Reporting              | Reports generated correctly  |
| Exception Investigator | Resolution rate              |
| Supervisor             | Time to complete workflow    |

We should treat agents like employees—measure their performance and continuously improve them.

---

# 🚨 I think we've reached an inflection point

Up to now, we've been designing **the organization**.

The next decision determines whether this becomes an elegant system or a maintenance nightmare.

We need to decide **how agents collaborate**.

There are three fundamentally different architectures:

### 1. Supervisor Pattern (manager → specialists)

- Simple
- Reliable
- Easy to debug
- Best for enterprise workflows

### 2. Collaborative Swarm

- Agents communicate freely
- Flexible
- Hard to reason about
- Difficult to audit

### 3. Event-Driven Workflow Graph

- Agents react to events
- Highly scalable
- Excellent for long-running workflows
- More engineering complexity

## My recommendation

For the long-term product, a hybrid may be appropriate:

- A **Supervisor Agent** owns the business goal and planning.
- **Specialist Agents** perform domain-specific work.
- An **event bus/task queue** coordinates execution and state changes.

The MVP deliberately does not implement that event bus. PostgreSQL owns run,
task, lease, approval, and audit state, and one worker executes the fixed task
graph. Revisit a separate queue or event bus only after measured load or
integration needs justify it.

That gives us:

- Clear ownership
- Auditability (critical in finance)
- Scalability
- The ability to add new specialist agents without rewriting the system

This future architecture may align with enterprise finance requirements, but it
must be justified by product usage and operational load before implementation.
