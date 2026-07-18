# Workflow Bible v0.1

**Status:** Broad domain reference. It describes a real month-end close, not the
approved MVP scope. For the build boundary, use `PRD.md` and `TDD.md`.

**Demo source decision:** The demo workflow reads Xero through
`XeroDirectDemoAdapter` and an owned Xero MCP server. Demo bank data comes from
Plaid Sandbox. The later live workflow uses Fivetran/Plaid Production in the US
and Setu Account Aggregator in India.

## Purpose

The Workflow Bible is the canonical description of how accounting work gets done.

It is **not** a feature document.

It is **not** a PRD.

It is a map of reality.

Every workflow we automate must first exist here.

---

# Structure

Every workflow will follow the same template.

```text
Workflow
│
├── Goal
├── Trigger
├── Stakeholders
├── Systems
├── Inputs
├── Outputs
├── Success Criteria
├── Happy Path
├── Decision Points
├── Exceptions
├── Pain Points
├── AI Opportunities
├── Automation Opportunities
├── Risks
├── Metrics
└── Future Agent Design Notes
```

---

# Workflow #1 — Month-End Close

This is our wedge.

The MVP automates only the close-readiness subset: evidence collection, AP
readiness, bank reconciliation and any real exceptions it discovers, draft
adjustments, pro forma reports, and package approval. AR, posting, period lock,
and a legally or operationally complete close are outside the MVP.

---

# 1. Goal

Produce accurate financial statements for a completed accounting period.

Deliverables include:

- Profit & Loss
- Balance Sheet
- Cash Flow
- Trial Balance
- Journal Entries
- Management Reports

---

# 2. Trigger

Normally begins:

- End of month
- Quarter end
- Fiscal year end

Possible trigger inside our system:

> **User:** "Close July."

or

> Schedule detects the close window has started.

---

# 3. Primary Stakeholders

| Role              | Responsibility       |
| ----------------- | -------------------- |
| Staff Accountant  | Executes close tasks |
| Senior Accountant | Reviews entries      |
| Controller        | Owns the close       |
| CFO               | Reviews financials   |
| AP Team           | Vendor invoices      |
| AR Team           | Customer payments    |
| Payroll Team      | Payroll journals     |

---

# 4. Systems Involved

This is where existing software breaks down.

Possible systems:

- ERP (QuickBooks, NetSuite, Xero)
- Bank accounts
- Credit card feeds
- Stripe
- Expense systems
- Payroll
- Google Drive
- Email
- Slack / Teams
- Excel
- Vendor portals

The close spans all of them.

---

# 5. Inputs

Typical inputs:

- Vendor invoices
- Customer invoices
- Bank statements
- Credit card statements
- Payroll reports
- Expense reports
- Fixed asset updates
- Inventory reports
- Manual journals

---

# 6. Outputs

Expected outputs:

- Reconciled accounts
- Posted journal entries
- Financial statements
- Variance report
- Close checklist completed
- Audit trail

---

# 7. High-Level Workflow

This is intentionally simple. We'll decompose every step later.

```text
Close Requested
        │
        ▼
Collect Required Data
        │
        ▼
Validate Completeness
        │
        ▼
Run AP Checks
        │
        ▼
Run AR Checks
        │
        ▼
Run Reconciliation
        │
        ▼
Resolve Exceptions
        │
        ▼
Prepare Journal Entries
        │
        ▼
Generate Reports
        │
        ▼
Controller Review
        │
        ▼
Close Complete
```

This is the orchestration loop our supervisor agent will own.

---

# 8. Human Decision Points

These are the moments where people add judgment today.

Examples:

- Missing invoice acceptable?
- Material variance?
- Write-off required?
- Duplicate payment?
- Fraud suspicion?
- Manual accrual?
- Journal approval?
- Close approval?

These are not bugs—they're opportunities for AI assistance.

---

# 9. Pain Points

This is where our product earns its value.

## Coordination

Nobody knows who is blocking the close.

---

## Missing Documents

Waiting on invoices.

Waiting on statements.

Waiting on receipts.

---

## Exceptions

Transactions don't match.

Nobody knows why.

---

## Manual Investigation

Searching:

- Email
- Slack
- ERP
- Bank
- Excel

to understand one discrepancy.

---

## Status Visibility

Controller asks:

> "What's left?"

Nobody has a single answer.

---

## Reporting

Reports are generated.

Then someone manually explains them.

---

# 10. Automation Opportunities

Tasks that can likely be automated:

- Document collection
- Checklist tracking
- Data validation
- Bank imports
- Variance detection
- Journal drafting
- Report generation
- Reminder emails
- Progress updates

---

# 11. AI Opportunities

This is where modern reasoning models may change the workflow.

Instead of just automating steps, the system can:

- Plan the close sequence
- Prioritize blockers
- Decide which agent to invoke
- Investigate mismatches
- Explain anomalies
- Draft stakeholder communication
- Escalate intelligently
- Learn recurring patterns

The AI isn't a feature—it's the operations manager.

---

# 12. Failure Scenarios

Our agents must expect these.

Examples:

- Missing bank feed
- Duplicate invoice
- Vendor sends incorrect file
- Payroll delayed
- API unavailable
- Human rejects journal
- Approval timeout

This section will grow over time.

---

# 13. Success Metrics

Operational metrics:

- Close duration
- Number of manual tasks
- Exception resolution time
- Human approvals
- Automation rate
- Number of blockers
- Accuracy
- Reopened closes

---

# 14. Agent Responsibilities

This is the bridge to our technical design.

### Supervisor Agent

Owns the outcome:

> "Close July."

Coordinates all work.

---

### AP Agent

Ensures payable data is complete.

---

### AR Agent

Ensures receivables are up to date.

---

### Reconciliation Agent

Confirms financial consistency.

---

### Reporting Agent

Produces management-ready outputs.

---

### Exception Agent

Investigates and proposes resolutions.

---

# Here's the key insight I want us to adopt

Most teams would stop here and start implementing.

I don't think we're ready yet.

This workflow is still **too high level**.

## The next step is where the real product insight lives

We're going to **explode each box** in the workflow into its own detailed workflow.

For example:

```
Collect Required Data
```

sounds like one step.

In reality, it might contain:

- Identify required documents
- Check which are already available
- Determine document owners
- Search connected systems
- Request missing documents
- Validate uploaded files
- Extract metadata
- Classify documents
- Detect duplicates
- Route to the correct downstream process

That single box can become an autonomous agent.

---

## This is how we'll build our product

We'll recursively decompose every workflow until each step is small enough to be owned by a single specialized agent.

At a future platform stage, we may have something like:

- **1 Supervisor Agent**
- **4–6 Workflow Agents**
- **Multiple narrowly scoped task executors**

Do not implement this agent count for the MVP. The approved design uses logical
task executors inside one modular monolith and a fixed workflow DAG. Future
decomposition should be driven by observed product needs and independently
testable boundaries, not by an agent-count target.

Now we're entering what I think will become the **most valuable document in the entire company**.

Not the PRD.

Not the architecture.

This one.

Because this is where we discover **what the agents actually do**.

---

# Workflow Bible — Level 2 Decomposition

We're going to decompose **Month-End Close** until every task could be assigned to an AI employee.

Think of this like creating an org chart for a finance department.

---

# Level 1

```text
Close July
```

Too big.

---

Break it down.

```text
Close July
│
├── Prepare Close
├── Accounts Payable
├── Accounts Receivable
├── Reconciliation
├── Journal Entries
├── Financial Review
├── Reporting
└── Final Approval
```

Better.

Still too big.

---

# Let's explode "Prepare Close"

Most companies ignore this.

I think this is actually where the close begins.

---

## Workflow

```text
Prepare Close

↓

Determine Close Period

↓

Load Close Checklist

↓

Connect Data Sources

↓

Verify Integrations

↓

Check Missing Documents

↓

Check Outstanding Tasks

↓

Identify Risks

↓

Generate Close Plan

↓

Begin Execution
```

Notice something?

This isn't accounting.

This is planning.

That's a suitable use of a constrained reasoning model.

---

# Every box becomes an AI responsibility.

---

## Step 1

### Determine Close Period

Input

```text
Today

Company Calendar

Accounting Calendar
```

Output

```text
July Close
```

---

Possible reasoning

- Monthly?
- Quarterly?
- Year-end?
- Adjustments?
- Reopened period?

---

AI Agent

Close Planner

---

## Step 2

### Load Close Checklist

Example

```text
Need

Bank Statement

Payroll

Inventory

Expenses

Fixed Assets

Sales

Tax
```

Every company differs.

Checklist should be dynamic.

---

## Step 3

### Connect Data Sources

Agent checks

✓ QuickBooks

✓ Stripe

✓ Bank

✓ Payroll

✓ Google Drive

✓ Email

✓ Slack

---

Failures

Disconnected

Expired Token

Permission Error

API Down

---

This is already one independent workflow.

---

## Step 4

### Check Missing Documents

This is huge.

Example

Need

```text
Payroll

×

Missing

Vendor Invoice

×

Missing

Credit Card

✓

Present
```

Current software

Stops.

Our system

Acts.

---

Agent should

Search

Email

Drive

Dropbox

Previous uploads

ERP

Vendor Portal

---

Still missing?

Request it.

---

# Future Product Hypothesis: Document Intake Agent

This is not the MVP build order. `TDD.md` Phase 0 and Phase 1 come first so
demo-provider capability, identity, isolation, and connection health are proven
before document automation is implemented. Live-provider access is a later gate.

Document Intake Agent.

---

Responsibilities

- discover documents

- search systems

- validate files

- classify

- extract metadata

- verify completeness

- request missing files

---

This alone is a startup.

---

## Step 5

### Outstanding Tasks

Example

```text
Payroll

Waiting

Inventory

Waiting

Manager Approval

Waiting
```

Agent asks

Who owns this?

Can I unblock it?

Should I escalate?

---

Again

Planning.

Not bookkeeping.

---

## Step 6

### Risk Detection

Imagine

```text
Bank not synced

Payroll delayed

Inventory incomplete

5 invoices missing

Stripe mismatch
```

Instead of

```text
Error.
```

Agent says

> Closing today is likely to fail because payroll data has not arrived and three vendor invoices remain outstanding. Estimated delay: one business day. Recommended action: request payroll export now and notify the AP manager about missing invoices.

That's a finance manager.

---

## Step 7

### Generate Close Plan

At a future platform stage, a constrained model may help explain or propose a
plan. The MVP executes the fixed, versioned DAG in `TDD.md`.

Output

```text
Step 1

Collect payroll

↓

Step 2

Collect invoices

↓

Step 3

Run AP

↓

Step 4

Run reconciliation

↓

Step 5

Review journals

↓

Reports

↓

Approval
```

Future-state: policy-bounded and configurable. This is deliberately not the MVP,
whose workflow is fixed and versioned for auditability.

---

# Let's look at this from the agent perspective.

```text
Supervisor Agent

│

├── Planning Agent

├── Document Intake Agent

├── Integration Agent

├── Risk Agent

├── Checklist Agent

├── Notification Agent
```

Notice something?

None of these are accounting.

They're operations.

That's our differentiation.

---

# This changes our architecture.

Originally we thought:

```text
AP Agent

AR Agent

Recon Agent

Close Agent
```

Now I think that's incomplete.

I think the architecture should be **layered**.

```text
                Supervisor Agent
                       │
      ┌────────────────┼────────────────┐
      │                │                │
 Operations      Accounting      Intelligence
      │                │                │
Planning      AP Agent        Risk Agent
Checklist     AR Agent        Exception Agent
Documents     Recon Agent     Reporting Agent
Integrations  Journal Agent   Memory Agent
Notifications Close Agent     Learning Agent
```

This separation matters:

- **Operations agents** orchestrate and gather information.
- **Accounting agents** perform finance-specific work.
- **Intelligence agents** reason, investigate, explain, and learn.

---
