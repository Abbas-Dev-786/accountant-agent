# DEBUG REPORT — first organization bootstrap denied

- **Symptom:** The first-time organization setup returned `user is not allowed
  to bootstrap this organization` after magic-link sign-in.
- **Root cause:** `POST /api/v1/organizations/bootstrap` only permits the
  authenticated Supabase user's verified email when it case-insensitively equals
  `ACCOUNTINGOS_BOOTSTRAP_CONTROLLER_EMAIL`. Both `backend/.env` and
  `backend/.env.local` still contain the template placeholder rather than a
  real controller mailbox, so no real magic-link user can pass the allow-list.
- **Fix required:** Set `ACCOUNTINGOS_BOOTSTRAP_CONTROLLER_EMAIL` in
  `backend/.env` to the exact email used for magic-link sign-in and restart the
  API with `./backend/start.sh`. Update `.env.local` too if it is used by a
  different local launch method. The value is an operator identity and must not
  be guessed or written by an agent.
- **Evidence:** `backend/app/main.py` compares `user.email.lower()` to the
  environment value before it creates an organization. `supabase_auth.py`
  obtains `user.email` from Supabase Auth's verified `/auth/v1/user` response.
  Configuration was inspected without disclosing the mailbox values; both files
  were detected as placeholders.
- **Regression test:** Not added or run at the user's explicit request not to
  rely on test cases.
- **Status:** DONE_WITH_CONCERNS — diagnosis is complete; the required
  controller-email choice belongs to the operator.
