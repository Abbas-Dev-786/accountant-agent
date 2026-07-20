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
type CloseMapping = {
  id: string;
  organization_id: string;
  version: number;
  status: string;
  configuration: {
    xero_tenant_id: string;
    bank_mappings: { plaid_account_id: string; xero_account_code: string; xero_account_name: string }[];
    matching_rules: {
      date_window_days: number;
      fee_tolerance: string;
      materiality_threshold: string;
      pending_policy: "exclude" | "exception";
      max_aggregate_size: number;
    };
    permitted_journal_account_codes: string[];
    journal_adjustment_account_code?: string | null;
    evidence: {
      drive_folder_ids: string[];
      gmail_mailbox: string;
      gmail_labels: string[];
      allowed_recipients: string[];
      retention_policy_version: string;
    };
  };
  approved_by_subject: string;
  created_at: string | null;
};
type ReviewData = {
  run_id: string;
  snapshot_id: string | null;
  mapping: CloseMapping | null;
  source_batches: { provider: string; environment: string; watermark: string; completed_at: string | null; complete: boolean; warnings: string[] }[];
  evidence_items: { id: string; provider: string; source_id: string; observed_at: string | null; kind: string; scope_reference: string; tags: string[] }[];
  review_package: { id: string; package_hash: string; status: string; summary: Record<string, unknown>; frozen_at: string | null } | null;
  journal_proposals: { id: string; date: string; narration: string; proposal_hash: string; status: string; lines: { account_code: string; debit: string; credit: string; evidence_ids: string[] }[] }[];
  reconciliation_matches: { id: string; kind: string; amount: string; currency: string; bank_transaction_ids: string[]; ledger_transaction_ids: string[]; evidence_ids: string[] }[];
  reconciliation_exceptions: { id: string; control_code: string; source_transaction_ids: string[]; evidence_ids: string[]; amount: string; currency: string; remediation: string; status: string; explanation: { cause: string; recommendation: string; evidence_ids: string[]; confidence_label: string; uncertainties: string[] } | null; explanation_status: string; resolution_comment: string | null; resolved_at: string | null }[];
  report: { data: Record<string, unknown>; hash: string; control_status: string; created_at: string | null } | null;
  artifacts: { id: string; type: string; object_key: string; content_hash: string; retention_mode: string; retain_until: string; status: string; provider_file_id: string | null }[];
  actions: { id: string; provider: string; operation: string; status: string; marker: string; provider_object_id: string | null; completed_at: string | null }[];
};
type MappingForm = {
  xeroTenantId: string;
  bankAccounts: Record<string, { xeroAccountCode: string; xeroAccountName: string }>;
  dateWindowDays: string;
  feeTolerance: string;
  materialityThreshold: string;
  pendingPolicy: "exclude" | "exception";
  maxAggregateSize: string;
  journalCodes: string;
  journalAdjustmentCode: string;
  driveFolderIds: string;
  gmailMailbox: string;
  gmailLabels: string;
  allowedRecipients: string;
  retentionPolicyVersion: string;
};

type PlaidHandler = { open: () => void };
type PlaidFactory = {
  create: (configuration: {
    token: string;
    onSuccess: (publicToken: string, metadata: { accounts?: { id?: string; account_id?: string }[] }) => void;
    onExit: (error: { display_message?: string } | null) => void;
  }) => PlaidHandler;
};

declare global {
  interface Window { Plaid?: PlaidFactory }
}

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

function csv(value: string): string[] {
  return value.split(",").map((item) => item.trim()).filter(Boolean);
}

function blankMappingForm(): MappingForm {
  return {
    xeroTenantId: "", bankAccounts: {}, dateWindowDays: "3", feeTolerance: "0", materialityThreshold: "0",
    pendingPolicy: "exception", maxAggregateSize: "10", journalCodes: "", journalAdjustmentCode: "", driveFolderIds: "", gmailMailbox: "",
    gmailLabels: "", allowedRecipients: "", retentionPolicyVersion: "v1",
  };
}

function loadPlaidLink(): Promise<PlaidFactory> {
  if (window.Plaid) return Promise.resolve(window.Plaid);
  return new Promise((resolve, reject) => {
    const existing = document.querySelector<HTMLScriptElement>('script[data-accountingos-plaid-link="true"]');
    if (existing) {
      existing.addEventListener("load", () => window.Plaid ? resolve(window.Plaid) : reject(new Error("Plaid Link did not load")), { once: true });
      existing.addEventListener("error", () => reject(new Error("Plaid Link could not load")), { once: true });
      return;
    }
    const script = document.createElement("script");
    script.src = "https://cdn.plaid.com/link/v2/stable/link-initialize.js";
    script.async = true;
    script.dataset.accountingosPlaidLink = "true";
    script.onload = () => window.Plaid ? resolve(window.Plaid) : reject(new Error("Plaid Link did not load"));
    script.onerror = () => reject(new Error("Plaid Link could not load"));
    document.head.appendChild(script);
  });
}

export default function Home() {
  const [session, setSession] = useState<Session | null>(null);
  const [workspace, setWorkspace] = useState<Workspace | null>(null);
  const [organizationId, setOrganizationId] = useState("");
  const [connections, setConnections] = useState<Connection[]>([]);
  const [run, setRun] = useState<CloseRun | null>(null);
  const [tasks, setTasks] = useState<CloseTask[]>([]);
  const [events, setEvents] = useState<CloseEvent[]>([]);
  const [mapping, setMapping] = useState<CloseMapping | null>(null);
  const [review, setReview] = useState<ReviewData | null>(null);
  const [mappingForm, setMappingForm] = useState<MappingForm>(blankMappingForm);
  const [exceptionNotes, setExceptionNotes] = useState<Record<string, string>>({});
  const [recoveryRecipient, setRecoveryRecipient] = useState("");
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

  async function loadMapping(token: string, nextOrganizationId: string) {
    if (!nextOrganizationId) {
      setMapping(null);
      return;
    }
    const nextMapping = await api<CloseMapping | null>(
      `/api/v1/organizations/${encodeURIComponent(nextOrganizationId)}/close-mapping`, token,
    );
    setMapping(nextMapping);
    if (nextMapping) {
      const config = nextMapping.configuration;
      setMappingForm({
        xeroTenantId: config.xero_tenant_id,
        bankAccounts: Object.fromEntries(config.bank_mappings.map((item) => [item.plaid_account_id, {
          xeroAccountCode: item.xero_account_code, xeroAccountName: item.xero_account_name,
        }])),
        dateWindowDays: String(config.matching_rules.date_window_days),
        feeTolerance: config.matching_rules.fee_tolerance,
        materialityThreshold: config.matching_rules.materiality_threshold,
        pendingPolicy: config.matching_rules.pending_policy,
        maxAggregateSize: String(config.matching_rules.max_aggregate_size),
        journalCodes: config.permitted_journal_account_codes.join(", "),
        journalAdjustmentCode: config.journal_adjustment_account_code || "",
        driveFolderIds: config.evidence.drive_folder_ids.join(", "),
        gmailMailbox: config.evidence.gmail_mailbox,
        gmailLabels: config.evidence.gmail_labels.join(", "),
        allowedRecipients: config.evidence.allowed_recipients.join(", "),
        retentionPolicyVersion: config.evidence.retention_policy_version,
      });
    }
  }

  async function loadWorkspace(nextSession: Session) {
    const nextWorkspace = await api<Workspace>("/api/v1/me", nextSession.access_token);
    setWorkspace(nextWorkspace);
    const nextOrganizationId =
      nextWorkspace.organizations.find((organization) => organization.id === organizationId)?.id ||
      nextWorkspace.organizations[0]?.id ||
      "";
    setOrganizationId(nextOrganizationId);
    await Promise.all([loadConnections(nextSession.access_token, nextOrganizationId), loadMapping(nextSession.access_token, nextOrganizationId)]);
  }

  async function loadRunDetails(token: string, runId: string) {
    const [nextRun, nextTasks, nextEvents, nextReview] = await Promise.all([
      api<CloseRun>(`/api/v1/close-runs/${encodeURIComponent(runId)}`, token),
      api<CloseTask[]>(`/api/v1/close-runs/${encodeURIComponent(runId)}/tasks`, token),
      api<CloseEvent[]>(`/api/v1/close-runs/${encodeURIComponent(runId)}/events`, token),
      api<ReviewData>(`/api/v1/close-runs/${encodeURIComponent(runId)}/review`, token),
    ]);
    setRun(nextRun);
    setTasks(nextTasks);
    setEvents(nextEvents);
    setReview(nextReview);
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
        setMapping(null);
        setReview(null);
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
    setRun(null);
    setReview(null);
    void Promise.all([loadConnections(session.access_token, organizationId), loadMapping(session.access_token, organizationId)]).catch((loadError: unknown) => {
      setError(loadError instanceof Error ? loadError.message : "Could not load connections");
    });
  }, [organizationId, session]);

  useEffect(() => {
    if (!session || !run?.id) return;
    const runId = run.id;
    const accessToken = session.access_token;
    const controller = new AbortController();
    let active = true;
    async function followProgress() {
      try {
        const after = events.length ? events[events.length - 1].id : 0;
        const response = await fetch(`${apiBaseUrl}/api/v1/close-runs/${encodeURIComponent(runId)}/events/stream?after=${after}`, {
          headers: { Authorization: `Bearer ${accessToken}` }, signal: controller.signal, cache: "no-store",
        });
        if (!response.ok || !response.body) throw new Error(`Live progress unavailable (${response.status})`);
        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = "";
        while (active) {
          const chunk = await reader.read();
          if (chunk.done) break;
          buffer += decoder.decode(chunk.value, { stream: true });
          const frames = buffer.split("\n\n");
          buffer = frames.pop() || "";
          for (const frame of frames) {
            const data = frame.split("\n").find((line) => line.startsWith("data: "))?.slice(6);
            if (!data) continue;
            const event = JSON.parse(data) as CloseEvent;
            setEvents((current) => current.some((item) => item.id === event.id) ? current : [...current, event]);
            void loadRunDetails(accessToken, runId).catch(() => undefined);
          }
        }
      } catch (streamError) {
        if (active && !(streamError instanceof DOMException && streamError.name === "AbortError")) {
          setError(streamError instanceof Error ? streamError.message : "Live progress stream disconnected");
        }
      }
    }
    void followProgress();
    return () => { active = false; controller.abort(); };
    // Events are replayed with their durable cursor; reconnect only when the run changes.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [run?.id, session?.access_token]);

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

  async function connectGoogle() {
    if (!session || !selectedOrganization) return;
    setBusy(true);
    setError("");
    try {
      const authorization = await api<{ authorization_url: string }>(
        `/api/v1/organizations/${encodeURIComponent(selectedOrganization.id)}/connections/google/authorize`,
        session.access_token,
      );
      window.location.assign(authorization.authorization_url);
    } catch (connectionError) {
      setError(connectionError instanceof Error ? connectionError.message : "Could not start Google authorization");
      setBusy(false);
    }
  }

  async function connectPlaid() {
    if (!session || !selectedOrganization) return;
    setBusy(true);
    setError("");
    setMessage("");
    try {
      const [{ link_token }, Plaid] = await Promise.all([
        api<{ link_token: string }>(
          `/api/v1/organizations/${encodeURIComponent(selectedOrganization.id)}/connections/plaid/link-token`, session.access_token,
        ),
        loadPlaidLink(),
      ]);
      setBusy(false);
      Plaid.create({
        token: link_token,
        onSuccess: (publicToken, metadata) => {
          const selectedAccountIds = (metadata.accounts || [])
            .map((account) => account.id || account.account_id || "").filter(Boolean);
          void (async () => {
            setBusy(true);
            try {
              await api<Connection[]>(
                `/api/v1/organizations/${encodeURIComponent(selectedOrganization.id)}/connections/plaid/exchange`, session.access_token,
                { method: "POST", body: JSON.stringify({ public_token: publicToken, selected_account_ids: selectedAccountIds }) },
              );
              await loadConnections(session.access_token, selectedOrganization.id);
              setMessage("Selected production bank accounts are connected. Map each one to its Xero ledger account before a close run.");
            } catch (exchangeError) {
              setError(exchangeError instanceof Error ? exchangeError.message : "Could not complete the Plaid connection");
            } finally {
              setBusy(false);
            }
          })();
        },
        onExit: (exitError) => {
          if (exitError?.display_message) setError(exitError.display_message);
        },
      }).open();
    } catch (connectionError) {
      setError(connectionError instanceof Error ? connectionError.message : "Could not start Plaid Link");
      setBusy(false);
    }
  }

  function updateBankMapping(accountId: string, field: "xeroAccountCode" | "xeroAccountName", value: string) {
    setMappingForm((current) => {
      const existing = current.bankAccounts[accountId] || { xeroAccountCode: "", xeroAccountName: "" };
      return {
        ...current,
        bankAccounts: { ...current.bankAccounts, [accountId]: { ...existing, [field]: value } },
      };
    });
  }

  async function saveMapping(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!session || !selectedOrganization) return;
    const plaidAccounts = connections.filter((connection) => connection.provider === "plaid" && connection.status === "healthy");
    setBusy(true);
    setError("");
    setMessage("");
    try {
      const saved = await api<CloseMapping>(
        `/api/v1/organizations/${encodeURIComponent(selectedOrganization.id)}/close-mapping`, session.access_token,
        {
          method: "POST",
          body: JSON.stringify({
            xero_tenant_id: mappingForm.xeroTenantId,
            bank_mappings: plaidAccounts.map((account) => ({
              plaid_account_id: account.provider_tenant_or_account_id,
              xero_account_code: mappingForm.bankAccounts[account.provider_tenant_or_account_id]?.xeroAccountCode || "",
              xero_account_name: mappingForm.bankAccounts[account.provider_tenant_or_account_id]?.xeroAccountName || "",
            })),
            matching_rules: {
              date_window_days: Number(mappingForm.dateWindowDays),
              fee_tolerance: mappingForm.feeTolerance,
              materiality_threshold: mappingForm.materialityThreshold,
              pending_policy: mappingForm.pendingPolicy,
              max_aggregate_size: Number(mappingForm.maxAggregateSize),
            },
            permitted_journal_account_codes: csv(mappingForm.journalCodes),
            journal_adjustment_account_code: mappingForm.journalAdjustmentCode.trim() || null,
            evidence: {
              drive_folder_ids: csv(mappingForm.driveFolderIds),
              gmail_mailbox: mappingForm.gmailMailbox,
              gmail_labels: csv(mappingForm.gmailLabels),
              allowed_recipients: csv(mappingForm.allowedRecipients),
              retention_policy_version: mappingForm.retentionPolicyVersion,
            },
          }),
        },
      );
      setMapping(saved);
      setMessage(`Close mapping version ${saved.version} is approved for the next close run.`);
    } catch (mappingError) {
      setError(mappingError instanceof Error ? mappingError.message : "Could not save the close mapping");
    } finally {
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

  async function resolveException(exceptionId: string, status: "resolved" | "ignored") {
    if (!session || !run) return;
    const comment = (exceptionNotes[exceptionId] || "").trim();
    if (comment.length < 3) {
      setError("Add a brief resolution note before closing an exception.");
      return;
    }
    setBusy(true); setError("");
    try {
      await api(`/api/v1/close-runs/${run.id}/exceptions/${encodeURIComponent(exceptionId)}/resolve`, session.access_token, {
        method: "POST", body: JSON.stringify({ status, comment }),
      });
      await loadRunDetails(session.access_token, run.id);
      setMessage(`Exception ${status}. The decision and note are now in the durable close record.`);
    } catch (actionError) {
      setError(actionError instanceof Error ? actionError.message : "Could not resolve the exception");
    } finally { setBusy(false); }
  }

  async function requestRecoveryEmail(exceptionId: string) {
    if (!session || !run) return;
    const recipient = recoveryRecipient || mapping?.configuration.evidence.allowed_recipients[0] || "";
    if (!recipient) {
      setError("Enter an allowlisted recovery recipient.");
      return;
    }
    setBusy(true); setError("");
    try {
      await api(`/api/v1/close-runs/${run.id}/exceptions/${encodeURIComponent(exceptionId)}/recovery-email`, session.access_token, {
        method: "POST", body: JSON.stringify({ recipient }),
      });
      await loadRunDetails(session.access_token, run.id);
      setMessage("Approved recovery email queued. The worker will create, send, and reconcile it.");
    } catch (actionError) {
      setError(actionError instanceof Error ? actionError.message : "Could not queue recovery email");
    } finally { setBusy(false); }
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
          <button onClick={createRun} disabled={busy || !mapping || period.end < period.start}>{busy ? "Working…" : "Prepare close package"}</button>
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
              <div className="connection-actions">
                <button className="secondary" onClick={connectXero} disabled={busy || !selectedOrganization || selectedOrganization.role === "viewer"}>Connect Xero tenant</button>
                <button className="secondary" onClick={connectPlaid} disabled={busy || !selectedOrganization || selectedOrganization.role === "viewer"}>Connect bank with Plaid</button>
                <button className="secondary" onClick={connectGoogle} disabled={busy || !selectedOrganization || selectedOrganization.role === "viewer"}>Connect Google Workspace</button>
              </div>
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
                {run.status === "awaiting_approval" && selectedOrganization?.role === "controller" && <button className="secondary" onClick={approvePackage} disabled={busy}>Approve frozen package</button>}
              </div>}
            </article>

            <article className="card policy">
              <p className="eyebrow">ACTION POLICY</p>
              <h2>Bounded by design</h2>
              <p>Only a controller may approve a frozen package. The sole accounting write is a balanced Xero manual journal in <strong>DRAFT</strong> status.</p>
              <div className="forbidden">Posting, payments, deletion, and period locking are unavailable.</div>
            </article>

            <article className="card mapping">
              <p className="eyebrow">ACCOUNTANT-APPROVED CONFIGURATION</p>
              <h2>Bank-to-ledger mapping</h2>
              <p className="card-copy">This versioned configuration is required before a close run. It binds selected Plaid accounts to Xero ledger accounts, matching rules, evidence scope, and permitted journal codes.</p>
              {selectedOrganization?.role === "controller" ? (
                <form className="mapping-form" onSubmit={saveMapping}>
                  <label>Xero tenant
                    <select value={mappingForm.xeroTenantId} onChange={(event) => setMappingForm({ ...mappingForm, xeroTenantId: event.target.value })} required>
                      <option value="">Choose connected tenant</option>
                      {connections.filter((connection) => connection.provider === "xero" && connection.status === "healthy").map((connection) => (
                        <option key={connection.id} value={connection.provider_tenant_or_account_id}>{connection.provider_tenant_or_account_id}</option>
                      ))}
                    </select>
                  </label>
                  <div className="mapping-accounts">
                    <p>Selected Plaid accounts</p>
                    {connections.filter((connection) => connection.provider === "plaid" && connection.status === "healthy").map((connection) => {
                      const accountId = connection.provider_tenant_or_account_id;
                      const values = mappingForm.bankAccounts[accountId] || { xeroAccountCode: "", xeroAccountName: "" };
                      return <div className="account-map" key={connection.id}>
                        <code>{accountId}</code>
                        <label>Xero account code<input value={values.xeroAccountCode} onChange={(event) => updateBankMapping(accountId, "xeroAccountCode", event.target.value)} required /></label>
                        <label>Xero account name<input value={values.xeroAccountName} onChange={(event) => updateBankMapping(accountId, "xeroAccountName", event.target.value)} required /></label>
                      </div>;
                    })}
                    {!connections.some((connection) => connection.provider === "plaid" && connection.status === "healthy") && <p className="empty-state">Connect one or more selected Plaid accounts to create this mapping.</p>}
                  </div>
                  <div className="form-columns">
                    <label>Date window (days)<input type="number" min="0" max="60" value={mappingForm.dateWindowDays} onChange={(event) => setMappingForm({ ...mappingForm, dateWindowDays: event.target.value })} required /></label>
                    <label>Fee tolerance<input inputMode="decimal" value={mappingForm.feeTolerance} onChange={(event) => setMappingForm({ ...mappingForm, feeTolerance: event.target.value })} required /></label>
                    <label>Materiality threshold<input inputMode="decimal" value={mappingForm.materialityThreshold} onChange={(event) => setMappingForm({ ...mappingForm, materialityThreshold: event.target.value })} required /></label>
                    <label>Pending policy<select value={mappingForm.pendingPolicy} onChange={(event) => setMappingForm({ ...mappingForm, pendingPolicy: event.target.value as "exclude" | "exception" })}><option value="exception">Keep as exception</option><option value="exclude">Exclude by policy</option></select></label>
                    <label>Maximum aggregate size<input type="number" min="1" max="100" value={mappingForm.maxAggregateSize} onChange={(event) => setMappingForm({ ...mappingForm, maxAggregateSize: event.target.value })} required /></label>
                    <label>Permitted journal codes<input value={mappingForm.journalCodes} onChange={(event) => setMappingForm({ ...mappingForm, journalCodes: event.target.value })} placeholder="1000, 2000" required /></label>
                    <label>Adjustment offset code<input value={mappingForm.journalAdjustmentCode} onChange={(event) => setMappingForm({ ...mappingForm, journalAdjustmentCode: event.target.value })} placeholder="Optional; must be permitted" /></label>
                  </div>
                  <div className="form-columns evidence-fields">
                    <label>Drive folder IDs<input value={mappingForm.driveFolderIds} onChange={(event) => setMappingForm({ ...mappingForm, driveFolderIds: event.target.value })} placeholder="folder-id, folder-id" required /></label>
                    <label>Gmail mailbox<input type="email" value={mappingForm.gmailMailbox} onChange={(event) => setMappingForm({ ...mappingForm, gmailMailbox: event.target.value })} placeholder="close@example.com" required /></label>
                    <label>Gmail labels<input value={mappingForm.gmailLabels} onChange={(event) => setMappingForm({ ...mappingForm, gmailLabels: event.target.value })} placeholder="MONTH_END" required /></label>
                    <label>Allowed recipients<input value={mappingForm.allowedRecipients} onChange={(event) => setMappingForm({ ...mappingForm, allowedRecipients: event.target.value })} placeholder="controller@example.com" required /></label>
                    <label>Retention policy version<input value={mappingForm.retentionPolicyVersion} onChange={(event) => setMappingForm({ ...mappingForm, retentionPolicyVersion: event.target.value })} required /></label>
                  </div>
                  <button type="submit" disabled={busy || !connections.some((connection) => connection.provider === "xero" && connection.status === "healthy") || !connections.some((connection) => connection.provider === "plaid" && connection.status === "healthy")}>{mapping ? `Save new version (current v${mapping.version})` : "Approve close mapping"}</button>
                </form>
              ) : (
                <p className="empty-state">Only a controller can create a new mapping version.</p>
              )}
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

            {run && review && <article className="card review">
              <p className="eyebrow">CONTROLLER REVIEW</p>
              <h2>Frozen-source detail</h2>
              <div className="review-summary">
                <p><span>Snapshot</span><code>{review.snapshot_id || "Not committed yet"}</code></p>
                <p><span>Mapping</span>{review.mapping ? `Version ${review.mapping.version}` : "Not configured"}</p>
                <p><span>Evidence items</span>{review.evidence_items.length}</p>
                <p><span>Journal proposals</span>{review.journal_proposals.length}</p>
              </div>
              <div className="review-columns">
                <section><h3>Source batches</h3>{review.source_batches.length ? <ul className="review-list">{review.source_batches.map((batch) => <li key={`${batch.provider}-${batch.watermark}`}><span><strong>{batch.provider}</strong><small>{batch.environment} · {batch.complete ? "complete" : "incomplete"}</small></span><code>{batch.watermark}</code></li>)}</ul> : <p className="empty-state">No frozen source batch yet.</p>}</section>
                <section><h3>Evidence</h3>{review.evidence_items.length ? <ul className="review-list">{review.evidence_items.slice(0, 8).map((item) => <li key={item.id}><span><strong>{item.kind}</strong><small>{item.provider} · {item.scope_reference}</small></span><code>{item.source_id}</code></li>)}</ul> : <p className="empty-state">No scoped evidence collected yet.</p>}</section>
              </div>
              <section className="reconciliation-panel"><h3>Reconciliation</h3>
                {review.reconciliation_matches.length ? <ul className="review-list">{review.reconciliation_matches.map((match) => <li key={match.id}><span><strong>{match.kind} match · {match.amount} {match.currency}</strong><small>{match.bank_transaction_ids.join(", ")} ↔ {match.ledger_transaction_ids.join(", ")}</small></span><code>{match.evidence_ids.length} sources</code></li>)}</ul> : <p className="empty-state">No persisted reconciliation matches yet.</p>}
              </section>
              <section className="exceptions-panel"><div><h3>Exceptions and recovery</h3><p className="card-copy">Every recovery action is queued for the worker; no email or accounting write happens from this browser.</p></div>
                {review.reconciliation_exceptions.some((item) => item.status === "open") && <label className="recovery-recipient">Allowlisted recovery recipient<input type="email" value={recoveryRecipient} onChange={(event) => setRecoveryRecipient(event.target.value)} placeholder={mapping?.configuration.evidence.allowed_recipients[0] || "controller@example.com"} /></label>}
                {review.reconciliation_exceptions.length ? review.reconciliation_exceptions.map((item) => <article className={`exception ${statusClass(item.status)}`} key={item.id}>
                  <p><strong>{readableStatus(item.control_code)} · {item.amount} {item.currency}</strong><small>{item.status} · explanation {readableStatus(item.explanation_status)}</small></p>
                  <p className="exception-remediation">{item.remediation}</p>
                  {item.explanation && <div className="explanation"><p><b>Grounded explanation</b> {item.explanation.cause}</p><p><b>Recommended next step</b> {item.explanation.recommendation}</p><small>Cites: {item.explanation.evidence_ids.join(", ")} · confidence {item.explanation.confidence_label}</small></div>}
                  {item.status === "open" && selectedOrganization?.role !== "viewer" && <div className="exception-actions"><input value={exceptionNotes[item.id] || ""} onChange={(event) => setExceptionNotes({ ...exceptionNotes, [item.id]: event.target.value })} placeholder="Resolution or policy note" /><button className="secondary" onClick={() => void resolveException(item.id, "resolved")} disabled={busy}>Resolve</button><button className="secondary" onClick={() => void resolveException(item.id, "ignored")} disabled={busy}>Ignore by policy</button><button className="secondary" onClick={() => void requestRecoveryEmail(item.id)} disabled={busy}>Request evidence</button></div>}
                  {item.resolution_comment && <p className="resolution-note">Decision: {item.resolution_comment}</p>}
                </article>) : <p className="empty-state">No persisted exceptions.</p>}
              </section>
              <section className="reports-panel"><h3>Reports and immutable archive</h3>
                {review.report ? <><p className={`report-status ${statusClass(review.report.control_status)}`}>Report controls: {readableStatus(review.report.control_status)}</p><pre>{JSON.stringify(review.report.data, null, 2)}</pre></> : <p className="empty-state">Reports will appear after durable reconciliation.</p>}
                {review.artifacts.length ? <ul className="review-list">{review.artifacts.map((artifact) => <li key={artifact.id}><span><strong>{readableStatus(artifact.type)} · {artifact.status}</strong><small>Object Lock: {artifact.retention_mode} through {new Date(artifact.retain_until).toLocaleDateString()}</small></span><code>{artifact.content_hash.slice(0, 16)}</code></li>)}</ul> : <p className="empty-state">No verified immutable B2 artifact yet.</p>}
              </section>
              <section className="proposals"><h3>Journal proposals</h3>{review.journal_proposals.length ? review.journal_proposals.map((proposal) => <div className="proposal" key={proposal.id}><p><strong>{proposal.narration}</strong><small>{proposal.date} · {readableStatus(proposal.status)}</small></p>{proposal.lines.map((line, index) => <p className="proposal-line" key={`${proposal.id}-${index}`}><code>{line.account_code}</code><span>Debit {line.debit} · Credit {line.credit}</span></p>)}</div>) : <p className="empty-state">No proposed journals. A close may complete with no adjustment.</p>}</section>
              <section className="action-panel"><h3>Worker action recovery</h3>{review.actions.length ? <ul className="review-list">{review.actions.map((action) => <li key={action.id}><span><strong>{action.provider} · {readableStatus(action.operation)}</strong><small>{action.provider_object_id ? `Provider object ${action.provider_object_id}` : "Awaiting provider object"} · {action.marker}</small></span><b className={statusClass(action.status)}>{readableStatus(action.status)}</b></li>)}</ul> : <p className="empty-state">No provider action has been requested.</p>}</section>
            </article>}
          </section>
        </>
      )}
    </main>
  );
}
