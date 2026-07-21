# DEBUG REPORT — magic-link API fetch failure

- **Symptom:** After a successful Supabase magic-link sign-in, the browser showed the generic `Failed to fetch` error while loading the authenticated workspace.
- **Root cause:** The first post-login request is `GET http://localhost:8000/api/v1/me`. No API process was listening on that address in this workspace. The local backend environment also retained both the template CORS origin and a Supabase PostgreSQL URL without `sslmode=require`. The backend correctly rejected the latter, but its validation error was mistakenly relabeled as an integer-settings error because `SupabaseConfigError` inherits from `ValueError`.
- **Fix:** `backend/start.sh` now replaces only unset/template CORS and OAuth-return URLs with the local web origin (`http://localhost:3000` by default) and appends `sslmode=require` only for the local process when absent. The configuration parser now reports its actual validation errors. The web API client reports the API URL and launcher command rather than the opaque browser fetch error.
- **Regression test:** Not added or run at the user's explicit request not to rely on test cases.
- **Status:** DONE_WITH_CONCERNS — local startup must be performed outside the sandbox; use `./backend/start.sh` and restart the web app if its public API URL changes.
