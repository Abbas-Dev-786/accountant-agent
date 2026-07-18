# Vision & Product Strategy v0.1

**Status:** Long-term product strategy. `PRD.md` defines the narrower demo
milestone and later live expansion.

**Demo integration decision:** Xero Demo Company is the initial accounting
system and Plaid Sandbox is the initial bank source. The later live expansion
adds Xero/Fivetran, Plaid Production, and Setu Account Aggregator. Other ERPs
and bank providers remain future expansion.

## Working Title

**AccountingOS**

> **An autonomous operating system for finance teams.**

_(We may rename this later. The strategy matters more than the name.)_

---

# 1. Executive Summary

## The Problem

Finance teams don't suffer from a lack of software.

They suffer from a lack of execution.

A modern finance team uses dozens of systems:

- ERP (QuickBooks, NetSuite, Xero)
- Banks
- Payment platforms
- Email
- Cloud storage
- Slack/Teams
- Excel
- Expense tools
- Payroll systems

Each system manages data, but **none owns the work**.

As a result, finance teams spend an enormous amount of time:

- collecting missing information
- following up with people
- reconciling transactions
- investigating exceptions
- coordinating approvals
- tracking task status
- manually closing books

Most of their effort is operational coordination rather than accounting expertise.

---

## Our Thesis

The next generation of finance software will not be another ERP.

It will consist of **AI agents that own business outcomes instead of software modules.**

Instead of opening an "Accounts Payable" screen, a finance manager will simply say:

> "Close July."

The AI system will determine the required work, coordinate specialist
capabilities, interact with permitted tools, and return the outcome for any
required human approval.

---

## Our Vision

Build the AI operating system that runs finance operations.

---

# 2. Why Now?

Several technology shifts have converged.

### Shift 1 — Mature Finance APIs

Modern accounting software exposes APIs.

Banks expose APIs.

Payment providers expose APIs.

Document systems expose APIs.

The infrastructure now exists for software to act across multiple systems.

---

### Shift 2 — Foundation Models

Earlier AI could classify documents.

Modern models can:

- reason
- plan
- call tools
- recover from failures
- explain decisions

This transforms AI from a prediction engine into an execution engine.

---

### Shift 3 — Agentic Workflows

The next leap isn't better chat.

It's software that can execute multi-step workflows while involving humans only when necessary.

Finance operations are one of the clearest applications of this capability.

---

# 3. The Future We Believe In

Today's software is interface-driven.

```text
User
↓

Clicks buttons

↓

Software updates records
```

Tomorrow's software is outcome-driven.

```text
User

↓

Defines outcome

↓

AI plans work

↓

AI executes work

↓

AI verifies

↓

Human approves exceptions

↓

Done
```

The interface becomes conversation.

The product becomes execution.

---

# 4. Initial Customer

We need discipline here.

Our first customer is **not "everyone with accounting."**

## Primary ICP

**SMBs with small finance teams (roughly 5–50 finance employees) using modern cloud accounting software.**

Why this segment?

- Frequent month-end closes
- Lean teams with repetitive work
- Cloud-native tooling
- Faster buying cycles than large enterprises
- Significant operational pain
- Good API ecosystems

We can expand later to CPA firms and larger enterprises.

---

# 5. The Core Problem

Finance teams don't need another dashboard.

They need another worker.

Current software stores information.

Our system completes work.

That is our positioning.

---

# 6. Product Philosophy

These principles guide every product decision.

### Principle 1 — Outcomes Over Features

Users request outcomes.

Not modules.

---

### Principle 2 — AI Executes

Humans shouldn't coordinate repetitive workflows.

The AI should.

---

### Principle 3 — Human Approval for Financial Risk

AI may prepare.

Humans approve.

Especially for payments, journal entries, and compliance-sensitive actions.

---

### Principle 4 — Explain Every Decision

Every action must answer:

- Why?
- Based on what evidence?
- What confidence?
- What alternatives?

Trust is a product feature.

---

### Principle 5 — Integrate, Don't Replace

We do not compete with ERPs initially.

We orchestrate them.

---

# 7. Product Wedge

This is where we need to stay focused.

## We are NOT building

- ERP software
- General bookkeeping
- Another accounting dashboard
- AI chat for accountants

## We ARE building

An autonomous **Close Readiness Agent**.

The first version owns one high-value outcome:

> Prepare a month-end close package for controller approval.

To achieve that, it coordinates:

- Accounts Payable readiness
- Reconciliation
- Pro forma reporting
- Exception handling

Notice that AP and Reconciliation are supporting capabilities—not separate products.

The first version does not post journals or close an ERP period. A true close
becomes possible only after live integrations and posting controls are proven.

---

# 8. Long-Term Roadmap

This is how the company grows.

### Phase 1

Close Readiness Agent

One versioned workflow controller coordinating logical task executors for a
reviewable close package.

---

### Phase 2

Accounting Operations Platform

Additional fixed workflows and specialist capabilities for:

- AP
- AR
- Reconciliation
- Reporting
- Audit preparation

---

### Phase 3

AccountingOS

A general supervisor can select and coordinate workflows across accounting
operations. This is broader than the Phase 1 close workflow controller.

---

### Phase 4

FinanceOS

Expand beyond accounting into procurement, treasury, payroll, forecasting, compliance, and FP&A.

---

# 9. Why We'll Win

We are not trying to build better accounting software.

We are building software that behaves like an experienced finance operations manager.

Our advantage comes from:

- Deep workflow understanding
- Agent orchestration
- Cross-system execution
- Institutional memory
- Continuous learning from repeated finance operations

The more workflows the system completes, the better it becomes at anticipating blockers and coordinating work.

---

# 10. Success Metrics

For the MVP, success is not revenue.

It's proof that an autonomous workflow is valuable.

We'll measure:

- Time to prepare a close package
- Percentage of tasks completed autonomously
- Number of human interventions required
- Exception resolution time
- User trust (approval rate vs. overrides)
- Demo quality and end-to-end completion

---
