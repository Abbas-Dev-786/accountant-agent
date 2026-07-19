-- Server-side OAuth transaction store for the provider authorization flow.
-- Holds the short-lived PKCE verifier + state between the authorize redirect and
-- the callback, so state survives a process restart or a second worker. This is
-- transient auth material, never a long-lived credential; rows are one-time
-- (consumed on callback) and expire. Like every other financial schema it is
-- private: the browser talks to FastAPI, never to this table directly.

create table workflow.oauth_sessions (
    state text primary key,
    provider text not null check (provider in ('xero', 'plaid', 'drive', 'gmail')),
    organization_id text not null,
    code_verifier text not null,
    code_challenge text not null,
    redirect_uri text not null,
    oidc boolean not null default false,
    nonce text,
    expires_at timestamptz not null,
    created_at timestamptz not null default now()
);

create index oauth_sessions_expires_at_idx on workflow.oauth_sessions (expires_at);

alter table workflow.oauth_sessions enable row level security;

revoke all on workflow.oauth_sessions from anon, authenticated;
