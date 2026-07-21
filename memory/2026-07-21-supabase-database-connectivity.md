# DEBUG REPORT — Supabase database connectivity and CORS

- **Symptom:** After magic-link sign-in, `GET /api/v1/me` returned HTTP 500. The
  server trace ended in `psycopg.OperationalError` while resolving the configured
  Supabase direct database hostname. The browser also reported a fetch/CORS
  failure.
- **Root cause:** This is configuration drift at the database boundary. The
  backend was configured with the Supabase direct connection endpoint. Supabase
  documents that this endpoint is IPv6-only unless the project has the IPv4
  add-on; the local runtime could not resolve/reach it. The server previously
  allowed that driver exception to escape as a 500. Separately, local CORS only
  allowed one of `localhost` or `127.0.0.1`, although browsers treat them as
  different origins.
- **Fix:** `backend/start.sh` and `backend/app/main.py` now permit both
  loopback spellings for the chosen local web port. `backend/app/supabase_db.py`
  translates database operational errors into `SupabaseConnectionUnavailable`,
  and `backend/app/main.py` maps that to a CORS-compatible HTTP 503 without
  disclosing driver or connection details. `backend/.env.example` documents the
  required Session pooler setting for IPv4-only local networks.
- **Required operator action:** In Supabase Dashboard -> Connect, copy the exact
  **Session pooler** URI for this project, append `sslmode=require` if absent,
  set it as `SUPABASE_DB_URL` in `backend/.env`, then restart with
  `./backend/start.sh`. The pooler hostname and role are project/region-specific
  and must not be guessed or constructed from the direct URI.
- **Evidence:** The supplied trace reaches `psycopg.connect` and fails during
  hostname resolution. The repository configuration uses the direct
  `db.<project-ref>.supabase.co:5432` form. Supabase's current connection guide
  specifies the Session pooler for persistent application traffic on IPv4-only
  networks. Static checks passed: `bash -n backend/start.sh`, Python bytecode
  compilation of `backend/app`, and `git diff --check`. The sandbox cannot make
  an HTTP connection to the user's host-bound API process, so the live browser
  round trip requires the operator action above.
- **Regression test:** Not added or run at the user's explicit request not to
  rely on test cases.
- **Related:** `memory/2026-07-21-magic-link-api-fetch.md` records the earlier
  API-startup diagnosis.
- **Status:** DONE_WITH_CONCERNS — source changes are verified statically; live
  database connectivity requires replacing the project-specific connection URI.
