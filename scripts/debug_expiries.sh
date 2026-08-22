#!/usr/bin/env bash
# Trace expiring/closed options across bronze/silver/gold.
# Usage: scripts/debug_expiries.sh [closed_date_1] [closed_date_2]
#   e.g. scripts/debug_expiries.sh 2026-08-21 2026-08-22   (defaults to those two)
set -euo pipefail

D1="${1:-2026-08-21}"
D2="${2:-2026-08-22}"
# YYYY-MM-DD -> YYMMDD (OKX inst-id expiry code)
EXP1="${D1:2:2}${D1:5:2}${D1:8:2}"
EXP2="${D2:2:2}${D2:5:2}${D2:8:2}"

CONTAINER="${CEX_PG_CONTAINER:-cex_pg}"
DB_USER="${POSTGRES_USER:-cex}"
DB_NAME="${POSTGRES_DB:-cex_option_reporting}"
HERE="$(cd "$(dirname "$0")" && pwd)"

echo "Tracing closed dates $D1 / $D2  (inst-id expiry $EXP1 / $EXP2) in $CONTAINER/$DB_NAME"
docker exec -i "$CONTAINER" psql -U "$DB_USER" -d "$DB_NAME" \
  -v d1="$D1" -v d2="$D2" -v exp1="$EXP1" -v exp2="$EXP2" \
  -f - < "$HERE/debug_expiries.sql"
