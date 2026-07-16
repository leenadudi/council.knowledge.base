#!/usr/bin/env bash
#
# Quick local run for manual testing (esp. the Ask / follow-up thread).
# Preflights env + datastores so failures are obvious BEFORE the app boots,
# then launches the Flask app on http://localhost:5001.
#
# Usage:  ./scripts/run-local.sh
#
set -uo pipefail
cd "$(dirname "$0")/.."

PORT=5001
YELLOW=$'\033[33m'; GREEN=$'\033[32m'; RED=$'\033[31m'; DIM=$'\033[2m'; RST=$'\033[0m'

# ── Load .env (export every assignment) ───────────────────────────────────
if [[ -f .env ]]; then
  set -a; # shellcheck disable=SC1091
  source .env; set +a
  echo "${GREEN}✓${RST} loaded .env"
else
  echo "${YELLOW}!${RST} no .env found — copy .env.example → .env and fill in keys"
fi

# ── Hard requirements: API keys needed to answer a question ────────────────
# This deployment is cloud-backed: Supabase Postgres (SQL + pgvector), Neo4j
# Aura (graph), Voyage (embeddings), Anthropic (LLM). No local datastores.
missing=0
for var in ANTHROPIC_API_KEY VOYAGE_API_KEY DATABASE_URL NEO4J_URI; do
  if [[ -z "${!var:-}" ]]; then
    echo "${RED}✗${RST} $var is not set"
    missing=1
  else
    echo "${GREEN}✓${RST} $var set"
  fi
done

# ── Connectivity probe: reach the Supabase DB (holds SQL + vectors). ───────
# Uses the app's own Settings/driver so the DSN is parsed exactly as the app does.
if python3 - <<'PY' 2>/dev/null; then
import sys
from src.config import get_settings
import psycopg2
try:
    psycopg2.connect(get_settings().database_url, connect_timeout=5).close()
except Exception as e:
    sys.stderr.write(str(e)); sys.exit(1)
PY
  echo "${GREEN}✓${RST} Supabase Postgres reachable"
else
  echo "${YELLOW}!${RST} could not reach the database — check network / DATABASE_URL (queries will fail)"
fi

if [[ $missing -eq 1 ]]; then
  echo "${RED}Missing required config above. Fix .env, then re-run.${RST}"
  echo "${DIM}(The page will still load, but /ask will fail.)${RST}"
fi

# ── Free the port, then launch ────────────────────────────────────────────
if lsof -ti ":$PORT" >/dev/null 2>&1; then
  echo "${DIM}freeing port $PORT…${RST}"
  kill "$(lsof -ti ":$PORT")" 2>/dev/null || true
  sleep 1
fi

echo
echo "${GREEN}▶ starting${RST}  http://localhost:$PORT   ${DIM}(Ask tab → type a question → then a follow-up)${RST}"
echo "${DIM}Ctrl-C to stop.${RST}"
echo
exec python3 app.py
