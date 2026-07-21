#!/usr/bin/env bash
# Start the local AccountingOS API with backend/.env loaded server-side.
set -euo pipefail

backend_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$backend_dir"

if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

# The checked-in environment template intentionally contains production URL
# placeholders. For a local launcher, replace only those placeholders with the
# local web origin so an authenticated browser may call this API. A real
# configured deployment is never overridden.
local_web_origin="${ACCOUNTINGOS_LOCAL_WEB_ORIGIN:-http://localhost:3000}"
local_cors_origins="$local_web_origin"

# Browsers treat localhost and 127.0.0.1 as distinct origins.  Permit both
# loopback spellings for the configured local web port so switching between
# them cannot cause a development-only CORS failure.
if [[ "$local_web_origin" =~ ^http://(localhost|127\.0\.0\.1):([0-9]+)$ ]]; then
  local_web_port="${BASH_REMATCH[2]}"
  local_cors_origins="http://localhost:${local_web_port},http://127.0.0.1:${local_web_port}"
fi
if [[ -z "${ACCOUNTINGOS_CORS_ORIGINS:-}" || "${ACCOUNTINGOS_CORS_ORIGINS}" == *"replace-with-"* ]]; then
  export ACCOUNTINGOS_CORS_ORIGINS="$local_cors_origins"
fi
if [[ -z "${ACCOUNTINGOS_WEB_APP_URL:-}" || "${ACCOUNTINGOS_WEB_APP_URL}" == *"replace-with-"* ]]; then
  export ACCOUNTINGOS_WEB_APP_URL="$local_web_origin"
fi

# Supabase Postgres must use TLS. Older local connection strings occasionally
# omit the libpq sslmode query parameter; add the strict mode for this process
# rather than permitting an insecure fallback.
if [[ -n "${SUPABASE_DB_URL:-}" && "${SUPABASE_DB_URL}" != *"sslmode="* ]]; then
  separator="?"
  [[ "${SUPABASE_DB_URL}" == *"?"* ]] && separator="&"
  export SUPABASE_DB_URL="${SUPABASE_DB_URL}${separator}sslmode=require"
fi

python_bin="${ACCOUNTINGOS_PYTHON_BIN:-$backend_dir/.venv/bin/python}"
if [[ ! -x "$python_bin" ]]; then
  echo "Python virtual environment not found: $python_bin" >&2
  echo "Create it with: cd backend && python3 -m venv .venv && .venv/bin/pip install -e ." >&2
  exit 1
fi

host="${ACCOUNTINGOS_API_HOST:-127.0.0.1}"
port="${ACCOUNTINGOS_API_PORT:-8000}"

if [[ "${ACCOUNTINGOS_API_RELOAD:-1}" == "1" ]]; then
  exec "$python_bin" -m uvicorn app.main:app --host "$host" --port "$port" --reload "$@"
fi

exec "$python_bin" -m uvicorn app.main:app --host "$host" --port "$port" "$@"
