#!/usr/bin/env bash
# Nightly logical backup to DigitalOcean Spaces.
# DO droplet backups run weekly; losing six days of meeting notes is not an
# acceptable worst case for a company's client book.
set -euo pipefail

RETENTION_DAYS=30

STAMP=$(date -u +%Y%m%dT%H%M%SZ)
FILE="/tmp/gaia-${STAMP}.dump"

pg_dump -Fc -h db -U gaia gaia > "$FILE"
aws s3 cp "$FILE" "s3://${SPACES_BUCKET}/backups/gaia-${STAMP}.dump" \
    --endpoint-url "${SPACES_ENDPOINT}"
rm -f "$FILE"

# ${RETENTION_DAYS}-day retention. `date -d '30 days ago'` is GNU syntax and
# this service runs postgres:17-alpine (busybox date), which rejects it —
# with `set -e` that would abort the script right here, *after* the dump
# and upload above already succeeded: retention silently never prunes, and
# every run logs a false "backup failed". Epoch arithmetic works on both
# busybox and GNU date.
CUTOFF=$(date -u -d @$(( $(date -u +%s) - RETENTION_DAYS * 86400 )) +%Y%m%d)

# The age guard must be an `if`, not a bare `&&`-list. Used as a statement, an
# `&&`-list whose guard is false evaluates to 1 — and under `set -e` that kills
# the shell running it. That shell was the last stage of a pipeline (a subshell),
# so `pipefail` turned it into a non-zero exit for the whole script, *after* the
# dump and upload above had already succeeded. Every night but the very first
# has at least one object newer than the cutoff, so this fired every night,
# forever: retention never pruned, and `backup failed` was logged nightly until
# the operator learned to ignore it — which is worse than no alarm at all,
# because a genuine failure then looks exactly like the normal case.
#
# The listing is captured first rather than piped into the loop, so the loop
# body runs in *this* shell and the `pruned` counter survives it. A silent
# no-op and a working prune must not look the same in the logs.
LISTING=$(aws s3 ls "s3://${SPACES_BUCKET}/backups/" --endpoint-url "${SPACES_ENDPOINT}" \
          | awk '{print $4}')
pruned=0
while read -r key; do
    [ -n "$key" ] || continue
    d=$(echo "$key" | sed -n 's/gaia-\([0-9]\{8\}\)T.*/\1/p')
    # Anything that is not one of our dated dumps — or is newer than the
    # cutoff — is a normal skip, not an error.
    if [ -n "$d" ] && [ "$d" -lt "$CUTOFF" ]; then
        aws s3 rm "s3://${SPACES_BUCKET}/backups/${key}" --endpoint-url "${SPACES_ENDPOINT}"
        pruned=$((pruned + 1))
    fi
done <<< "$LISTING"

echo "backed up ${FILE##*/}; pruned ${pruned} backup(s) dated before ${CUTOFF}"
