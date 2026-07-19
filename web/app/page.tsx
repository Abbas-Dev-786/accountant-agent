"use client";

import { FormEvent, useEffect, useMemo, useState } from "react";
import type { Session } from "@supabase/supabase-js";

import { supabaseBrowserClient, supabaseBrowserIsConfigured } from "../lib/supabase";

type Organization = { id: string; name: string; role: "controller" | "operator" | "viewer" };
type Connection = {
  id: string;
  provider: string;
  provider_environment: string;
  provider_tenant_or_account_id: string;
  status: string;
  last_success_at: string | null;
  remediation: string | null;
};
type CloseRun = {
  id: string;
  organization_id: string;
  period: { start: string; end: string };
  status: string;
  deployment: { mode: string; data_class: string };
  snapshot_id: string | null;
  package_hash: string | null;
};
type CloseTask = {
  id: string;
  run_id: string;
  key: string;
  status: string;
  attempt: number;
  last_error: string | null;
  dependencies: string[];
};
type CloseEvent = {
  id: number;
  run_id: string;
  task_id: string | null;
  type: string;
  payload: Record<string, unknown>;
  created_at: string;
};
type Workspace = { id: string; email: string | null; organizations: Organization[] };

const providerCards = [
  { provider: "xero", name: "Xero Production", detail: "Approved tenant, read-only source, DRAFT-only journals" },
  { provider: "plaid", name: "Plaid Production", detail: "Selected-account, cursor-based bank synchronization" },
  { provider: "drive", name: "Google Workspace", detail: "Scoped Drive and Gmail evidence" },
  { provider: "b2", name: "Backblaze B2", detail: "Immutable package artifacts" },
  { provider: "groq", name: "Groq", detail: "Bounded, cited explanations" },
];

const apiBaseUrl = (process.env.NEXT_PUBLIC_API_BASE_URL || "http://localhost:8000").replace(/\/$/, "");

async function api<T>(path: string, token: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  headers.set("Authorization", `Bearer ${token}`);
  if (init.body && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");
  const response = await fetch(`${apiBaseUrl}${path}`, { ...init, cache: "no-store", headers });
  if (!response.ok) {
    const body = (await response.json().catch(() => null)) as { detail?: string } | null;
    throw new Error(body?.detail || `Request failed (${response.status})`);
  }
  return response.json() as Promise<T>;
}

function readableStatus(status: string): string {
  return status.replaceAll("_", " ");
}

function statusClass(status: string): string {
  return ["healthy", "authorized", "succeeded", "approved"].includes(status)
    ? "good"
    : ["blocked", "failed", "delayed", "partial", "expired", "revoked"].some((value) => status.includes(value))
      ? "warn"
      : "quiet";
}

function previousMonthRange(now = new Date()): { start: string; end: string } {
  const start = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth() - 1, 1));
  const end = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), 0));
  return { start: start.toISOString().slice(0, 10), end: end.toISOString().slice(0, 10) };
}

function closePeriodTitle(periodEnd: string): string {
  const timestamp = Date.parse(`${periodEnd}T00:00:00Z`);
  if (Number.isNaN(timestamp)) return "this period";
  return new Intl.DateTimeFormat("en-US", { month: "long", year: "numeric", timeZone: "UTC" }).format(timestamp);
}

export default function Home() {
  const [session, setSession] = useState<Session | null>(null);
  const [workspace, setWorkspace] = useState<Workspace | null>(null);
  const [organizationId, setOrganizationId] = useState("");
  const [connections, setConnections] = useState<Connection[]>([]);
  const [run, setRun] = useState<CloseRun | null>(null);
  const [tasks, setTasks] = useState<CloseTask[]>([]);
  const [events, setEvents] = useState<CloseEvent[]>([]);
  const [email, setEmail] = useState("");
  const [organizationName, setOrganizationName] = useState("");
  const [organizationSlug, setOrganizationSlug] = useState("");
  const [period, setPeriod] = useState(previousMonthRange);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const selectedOrganization = useMemo(
    () => workspace?.organizations.find((organization) => organization.id === organizationId) || null,
    [organizationId, workspace],
  );

  async function loadConnections(token: string, nextOrganizationId: string) {
    if (!nextOrganizationId) {
      setConnections([]);
      return;
    }
    const nextConnections = await api<Connection[]>(
      `/api/v1/organizations/${encodeURIComponent(nextOrganizationId)}/connections`,
      token,
    );
    setConnections(nextConnections);
  }

  async function loadWorkspace(nextSession: Session) {
    const nextWorkspace = await api<Workspace>("/api/v1/me", nextSession.access_token);
    setWorkspace(nextWorkspace);
    const nextOrganizationId =
      nextWorkspace.organizations.find((organization) => organization.id === organizationId)?.id ||
      nextWorkspace.organizations[0]?.id ||
      "";
    setOrganizationId(nextOrganizationId);
    await loadConnections(nextSession.access_token, nextOrganizationId);
  }

  async function loadRunDetails(token: string, runId: string) {
    const [nextRun, nextTasks, nextEvents] = await Promise.all([
      api<CloseRun>(`/api/v1/close-runs/${encodeURIComponent(runId)}`, token),
      api<CloseTask[]>(`/api/v1/close-runs/${encodeURIComponent(runId)}/tasks`, token),
      api<CloseEvent[]>(`/api/v1/close-runs/${encodeURIComponent(runId)}/events`, token),
    ]);
    setRun(nextRun);
    setTasks(nextTasks);
    setEvents(nextEvents);
  }

  useEffect(() => {
    const client = supabaseBrowserClient();
    if (!client) return;
    let active = true;
    void client.auth.getSession().then(({ data, error: authError }) => {
      if (!active) return;
      if (authError) setError(authError.message);
      setSession(data.session);
      if (data.session) {
        void loadWorkspace(data.session).catch((loadError: unknown) => {
          if (active) setError(loadError instanceof Error ? loadError.message : "Could not load workspace");
        });
      }
    });
    const { data: listener } = client.auth.onAuthStateChange((_event, nextSession) => {
      if (!active) return;
      setSession(nextSession);
      if (!nextSession) {
        setWorkspace(null);
        setConnections([]);
        setRun(null);
        setTasks([]);
        setEvents([]);
        return;
      }
      void loadWorkspace(nextSession).catch((loadError: unknown) => {
        if (active) setError(loadError instanceof Error ? loadError.message : "Could not load workspace");
      });
    });
    return () => {
      active = false;
      listener.subscription.unsubscribe();
    };
    // The client is stable. Re-establishing this listener for every workspace
    // refresh would create duplicate auth notifications.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (!session || !organizationId) return;
    void loadConnections(session.access_token, organizationId).catch((loadError: unknown) => {
      setError(loadError instanceof Error ? loadError.message : "Could not load connections");
    });
  }, [organizationId, session]);

  useEffect(() => {
    if (!session || !run || ["approved", "cancelled", "failed"].includes(run.status)) return;
    const timer = window.setInterval(() => {
      void loadRunDetails(session.access_token, run.id).catch(() => undefined);
    }, 10_000);
    return () => window.clearInterval(timer);
  }, [run, session]);

  async function sendMagicLink(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const client = supabaseBrowserClient();
    if (!client) return;
    setBusy(true);
    setError("");
    setMessage("");
    const { error: signInError } = await client.auth.signInWithOtp({
      email,
      options: { emailRedirectTo: window.location.origin },
    });
    setBusy(false);
    if (signInError) setError(signInError.message);
    else setMessage("Check your inbox for a secure sign-in link.");
  }

  async function bootstrapOrganization(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!session) return;
    setBusy(true);
    setError("");
    setMessage("");
    try {
      await api<Organization>("/api/v1/organizations/bootstrap", session.access_token, {
        method: "POST",
        body: JSON.stringify({ organization_id: organizationSlug, name: organizationName }),
      });
      await loadWorkspace(session);
      setMessage("US organization is ready. Connect the approved Xero tenant when production credentials are configured.");
    } catch (bootstrapError) {
      setError(bootstrapError instanceof Error ? bootstrapError.message : "Could not bootstrap the organization");
    } finally {
      setBusy(false);
    }
  }

  async function createRun() {
    if (!session || !selectedOrganization) return;
    setBusy(true);
    setError("");
    setMessage("");
    try {
      const key = globalThis.crypto?.randomUUID?.() || `close-${Date.now()}`;
      const nextRun = await api<CloseRun>("/api/v1/close-runs", session.access_token, {
        method: "POST",
        headers: { "Idempotency-Key": key },
        body: JSON.stringify({
          organization_id: selectedOrganization.id,
          period_start: period.start,
          period_end: period.end,
        }),
      });
      await loadRunDetails(session.access_token, nextRun.id);
      setMessage("Close run created. It will wait safely until verified provider data is available.");
    } catch (runError) {
      setError(runError instanceof Error ? runError.message : "Could not create close run");
    } finally {
      setBusy(false);
    }
  }

  async function connectXero() {
    if (!session || !selectedOrganization) return;
    setBusy(true);
    setError("");
    try {
      const authorization = await api<{ authorization_url: string }>(
        `/api/v1/organizations/${encodeURIComponent(selectedOrganization.id)}/connections/xero/authorize`,
        session.access_token,
      );
      window.location.assign(authorization.authorization_url);
    } catch (connectionError) {
      setError(connectionError instanceof Error ? connectionError.message : "Could not start Xero authorization");
      setBusy(false);
    }
  }

  async function signOut() {
    const client = supabaseBrowserClient();
    if (!client) return;
    await client.auth.signOut();
  }

  async function changeRun(path: "retry" | "cancel") {
    if (!session || !run) return;
    setBusy(true);
    setError("");
    try {
      const nextRun = await api<CloseRun>(`/api/v1/close-runs/${run.id}/${path}`, session.access_token, { method: "POST" });
      await loadRunDetails(session.access_token, nextRun.id);
      setMessage(path === "retry" ? "The blocked tasks were returned to the worker queue." : "The close run was cancelled.");
    } catch (changeError) {
      setError(changeError instanceof Error ? changeError.message : "Could not update the close run");
    } finally {
      setBusy(false);
    }
  }

  async function freezeReviewPackage() {
    if (!session || !run) return;
    setBusy(true);
    setError("");
    try {
      await api<{ package_hash: string }>(`/api/v1/close-runs/${run.id}/prepare-review`, session.access_token, {
        method: "POST",
        body: JSON.stringify([]),
      });
      await loadRunDetails(session.access_token, run.id);
      setMessage("The evidence-bound review package is frozen and ready for controller approval.");
    } catch (packageError) {
      setError(packageError instanceof Error ? packageError.message : "Could not freeze the review package");
    } finally {
      setBusy(false);
    }
  }

  async function approvePackage() {
    if (!session || !run?.package_hash) return;
    setBusy(true);
    setError("");
    try {
      await api<CloseRun>(`/api/v1/close-runs/${run.id}/approvals`, session.access_token, {
        method: "POST",
        body: JSON.stringify({ package_hash: run.package_hash }),
      });
      await loadRunDetails(session.access_token, run.id);
      setMessage("The frozen package was approved. Any allowed external action remains worker-controlled.");
    } catch (approvalError) {
      setError(approvalError instanceof Error ? approvalError.message : "Could not approve the review package");
    } finally {
      setBusy(false);
    }
  }

  if (!supabaseBrowserIsConfigured()) {
    return (
      <main className="setup-page">
        <section className="setup-card">
          <p className="eyebrow">ACCOUNTINGOS / CONFIGURATION REQUIRED</p>
          <h1>Connect Supabase Auth first.</h1>
          <p>
            Add the Supabase project URL and publishable key to <code>web/.env.local</code>, then restart the web app.
            The browser never needs the database URL or any provider secret.
          </p>
        </section>
      </main>
    );
  }

  if (!session) {
    return (
      <main className="auth-page">
        <section className="auth-copy">
          <div className="banner">US PRODUCTION — LIVE DATA ONLY</div>
          <p className="eyebrow">ACCOUNTINGOS / CLOSE READINESS</p>
          <h1>Close with evidence, not assumptions.</h1>
          <p className="subtitle">
            Sign in with Supabase Auth to prepare a controlled US close. Financial data and provider credentials remain
            server-side, and the workflow blocks until every required source is verified.
          </p>
        </section>
        <form className="auth-card" onSubmit={sendMagicLink}>
          <p className="eyebrow">SECURE SIGN-IN</p>
          <h2>Send a magic link</h2>
          <label htmlFor="email">Work email</label>
          <input
            id="email"
            type="email"
            autoComplete="email"
            value={email}
            onChange={(event) => setEmail(event.target.value)}
            placeholder="you@example.com"
            required
          />
          <button type="submit" disabled={busy}>{busy ? "Sending…" : "Email me a sign-in link"}</button>
          {message && <p className="notice good">{message}</p>}
          {error && <p className="notice error">{error}</p>}
        </form>
      </main>
    );
  }

  return (
    <main>
      <div className="topline">
        <div className="banner">US PRODUCTION — LIVE DATA ONLY</div>
        <div className="account-menu"><span>{workspace?.email || session.user.email}</span><button className="text-button" onClick={signOut}>Sign out</button></div>
      </div>
      <header>
        <div>
          <p className="eyebrow">ACCOUNTINGOS / CLOSE READINESS</p>
          <h1>Prepare {closePeriodTitle(period.end)} for review.</h1>
          <p className="subtitle">Every result must be traceable to a verified provider read and a frozen source snapshot.</p>
        </div>
        {selectedOrganization && <div className="close-setup">
          <label>Period start<input type="date" value={period.start} onChange={(event) => setPeriod({ ...period, start: event.target.value })} /></label>
          <label>Period end<input type="date" value={period.end} onChange={(event) => setPeriod({ ...period, end: event.target.value })} /></label>
          <button onClick={createRun} disabled={busy || period.end < period.start}>{busy ? "Working…" : "Prepare close package"}</button>
        </div>}
      </header>

      {error && <p className="notice error">{error}</p>}
      {message && <p className="notice good">{message}</p>}

      {!workspace?.organizations.length ? (
        <form className="empty card" onSubmit={bootstrapOrganization}>
          <p className="eyebrow">FIRST-TIME SETUP</p>
          <h2>Set up the US organization.</h2>
          <p>Your email must match <code>ACCOUNTINGOS_BOOTSTRAP_CONTROLLER_EMAIL</code> on the API. This creates one controller organization.</p>
          <label>Organization name<input value={organizationName} onChange={(event) => setOrganizationName(event.target.value)} placeholder="Acme US" required /></label>
          <label>Organization ID<input value={organizationSlug} onChange={(event) => setOrganizationSlug(event.target.value.toLowerCase().replace(/[^a-z0-9-]/g, ""))} placeholder="acme-us" pattern="[a-z0-9][a-z0-9-]*" required /></label>
          <button type="submit" disabled={busy}>{busy ? "Creating…" : "Create US organization"}</button>
        </form>
      ) : (
        <>
          <section className="workspace-bar" aria-label="Organization selector">
            <div><p className="eyebrow">ACTIVE ORGANIZATION</p><strong>{selectedOrganization?.name}</strong></div>
            <label>
              <span>Role: {selectedOrganization?.role}</span>
              <select value={organizationId} onChange={(event) => setOrganizationId(event.target.value)}>
                {workspace.organizations.map((organization) => <option key={organization.id} value={organization.id}>{organization.name}</option>)}
              </select>
            </label>
          </section>

          <section className="grid" aria-label="US production workspace">
            <article className="card sources">
              <p className="eyebrow">SOURCE READINESS</p>
              <h2>Configured sources</h2>
              <ul>
                {providerCards.map((card) => {
                  const connection = connections.find((item) => item.provider === card.provider);
                  const providerStatus = connection?.status || "not connected";
                  return (
                    <li key={card.provider}>
                      <span className={`dot ${statusClass(providerStatus)}`} aria-hidden="true" />
                      <span><strong>{card.name}</strong><small>{connection?.provider_environment || card.detail}</small></span>
                      <b className={statusClass(providerStatus)}>{readableStatus(providerStatus)}</b>
                    </li>
                  );
                })}
              </ul>
              <button className="secondary" onClick={connectXero} disabled={busy || !selectedOrganization || selectedOrganization.role === "viewer"}>
                Connect approved Xero tenant
              </button>
            </article>

            <article className="card progress">
              <p className="eyebrow">CLOSE RUN</p>
              <h2>{run ? `Run ${readableStatus(run.status)}` : "Waiting to synchronize"}</h2>
              <p>{run ? "The worker creates a snapshot only after required sources are complete and policy-valid." : "Create a run after connecting the approved production providers."}</p>
              <div className="steps"><span className={run ? "active" : ""}>1</span><i /><span>2</span><i /><span>3</span></div>
              <div className="labels"><span>Synchronize</span><span>Review</span><span>Approve draft</span></div>
              {run && <p className="run-id">Run ID: <code>{run.id}</code></p>}
              {run && <div className="run-actions">
                {run.status === "blocked" && <button className="secondary" onClick={() => changeRun("retry")} disabled={busy}>Retry blocked work</button>}
                {["synchronizing", "running", "blocked"].includes(run.status) && <button className="secondary" onClick={() => changeRun("cancel")} disabled={busy}>Cancel run</button>}
                {run.status === "running" && selectedOrganization?.role !== "viewer" && <button className="secondary" onClick={freezeReviewPackage} disabled={busy}>Freeze review package</button>}
                {run.status === "awaiting_approval" && selectedOrganization?.role === "controller" && <button className="secondary" onClick={approvePackage} disabled={busy}>Approve frozen package</button>}
              </div>}
            </article>

            <article className="card policy">
              <p className="eyebrow">ACTION POLICY</p>
              <h2>Bounded by design</h2>
              <p>Only a controller may approve a frozen package. The sole accounting write is a balanced Xero manual journal in <strong>DRAFT</strong> status.</p>
              <div className="forbidden">Posting, payments, deletion, and period locking are unavailable.</div>
            </article>

            {run && <article className="card timeline">
              <p className="eyebrow">WORKFLOW TIMELINE</p>
              <h2>Tasks and blockers</h2>
              <ul className="task-list">
                {tasks.map((task) => <li key={task.id}>
                  <span className={`dot ${statusClass(task.status)}`} aria-hidden="true" />
                  <span><strong>{readableStatus(task.key)}</strong><small>{task.last_error || (task.dependencies.length ? `After ${task.dependencies.join(", ")}` : "Ready to start")}</small></span>
                  <b className={statusClass(task.status)}>{readableStatus(task.status)}</b>
                </li>)}
              </ul>
              {events.length > 0 && <div className="event-log">
                {events.slice(-4).reverse().map((event) => <p key={event.id}><strong>{readableStatus(event.type)}</strong> <span>{new Date(event.created_at).toLocaleString()}</span></p>)}
              </div>}
            </article>}
          </section>
        </>
      )}
    </main>
  );
}
