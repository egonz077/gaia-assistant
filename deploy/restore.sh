#!/usr/bin/env bash
# Restore a dump into a scratch database and report row counts.
# Run this BEFORE going live. An untested backup is not a backup.
set -euo pipefail
DUMP=${1:?usage: restore.sh <dump-file>}

createdb -h db -U gaia gaia_restore_test 2>/dev/null || true
pg_restore -h db -U gaia -d gaia_restore_test --clean --if-exists "$DUMP"

psql -h db -U gaia -d gaia_restore_test -c "
  SELECT 'users' t, count(*) FROM users
  UNION ALL SELECT 'contacts', count(*) FROM contacts
  UNION ALL SELECT 'meetings', count(*) FROM meetings
  UNION ALL SELECT 'leads', count(*) FROM leads
  UNION ALL SELECT 'memory_chunks', count(*) FROM memory_chunks;"

echo "Restore OK. Drop with: dropdb -h db -U gaia gaia_restore_test"
