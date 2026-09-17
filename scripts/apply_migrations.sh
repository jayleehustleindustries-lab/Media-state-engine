#!/usr/bin/env bash
set -euo pipefail
DB="${DATABASE_URL:-postgresql:///media_state}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
echo "Applying migrations to $DB"
for f in "$ROOT"/supabase/migrations/*.sql; do
  echo "→ $(basename "$f")"
  psql "$DB" -v ON_ERROR_STOP=1 -f "$f"
done
echo "Done."
