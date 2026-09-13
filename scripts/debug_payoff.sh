#!/usr/bin/env bash
# Diagnose report ② (Payoff): expiry coverage + settlement-price availability.
# Usage: scripts/debug_payoff.sh [underlying]   (default BTC-USD)
set -euo pipefail
ULY="${1:-BTC-USD}"
CONTAINER="${CEX_PG_CONTAINER:-cex_pg}"
DB_USER="${POSTGRES_USER:-cex}"
DB_NAME="${POSTGRES_DB:-cex_option_reporting}"
HERE="$(cd "$(dirname "$0")" && pwd)"

# On the VPS Postgres may be local (not docker). Use psql directly if available, else docker exec.
if command -v psql >/dev/null 2>&1 && [ -n "${DATABASE_URL:-}" ]; then
  psql "$DATABASE_URL" -v uly="$ULY" -f "$HERE/debug_payoff.sql"
else
  docker exec -i "$CONTAINER" psql -U "$DB_USER" -d "$DB_NAME" -v uly="$ULY" -f - < "$HERE/debug_payoff.sql"
fi
