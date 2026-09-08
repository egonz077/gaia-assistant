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
aws s3 ls "s3://${SPACES_BUCKET}/backups/" --endpoint-url "${SPACES_ENDPOINT}" \
  | awk '{print $4}' \
  | while read -r key; do
      d=$(echo "$key" | sed -n 's/gaia-\([0-9]\{8\}\)T.*/\1/p')
      [ -n "$d" ] && [ "$d" -lt "$CUTOFF" ] && \
        aws s3 rm "s3://${SPACES_BUCKET}/backups/${key}" --endpoint-url "${SPACES_ENDPOINT}"
    done
echo "backed up ${FILE##*/}"
