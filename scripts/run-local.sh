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

# ── Hard requirements: keys needed to actually answer a question ───────────
missing=0
for var in ANTHROPIC_API_KEY OPENAI_API_KEY; do
  if [[ -z "${!var:-}" ]]; then
    echo "${RED}✗${RST} $var is not set (required to answer questions)"
    missing=1
  else
    echo "${GREEN}✓${RST} $var set"
  fi
done

# ── Soft checks: datastores. Warn but don't block — the UI still loads. ────
check_tcp() { # host port label
  if command -v nc >/dev/null 2>&1 && nc -z -G2 "$1" "$2" >/dev/null 2>&1; then
    echo "${GREEN}✓${RST} $3 reachable ($1:$2)"
  else
    echo "${YELLOW}!${RST} $3 NOT reachable ($1:$2) — queries will error until it's up"
  fi
}
# Pull host:port from the DSN/URLs when present, else fall back to defaults.
check_tcp localhost 5432 "PostgreSQL"
check_tcp localhost 6333 "Qdrant (vector)"
check_tcp localhost 7687 "Neo4j (graph)"

if [[ $missing -eq 1 ]]; then
  echo "${RED}Cannot answer questions without the keys above. Set them in .env, then re-run.${RST}"
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
