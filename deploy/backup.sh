#!/usr/bin/env bash
# Nightly logical backup to DigitalOcean Spaces.
# DO droplet backups run weekly; losing six days of meeting notes is not an
# acceptable worst case for a company's client book.
set -euo pipefail

STAMP=$(date -u +%Y%m%dT%H%M%SZ)
FILE="/tmp/gaia-${STAMP}.dump"

pg_dump -Fc -h db -U gaia gaia > "$FILE"
aws s3 cp "$FILE" "s3://${SPACES_BUCKET}/backups/gaia-${STAMP}.dump" \
    --endpoint-url "${SPACES_ENDPOINT}"
rm -f "$FILE"

# 30-day retention.
CUTOFF=$(date -u -d '30 days ago' +%Y%m%d)
aws s3 ls "s3://${SPACES_BUCKET}/backups/" --endpoint-url "${SPACES_ENDPOINT}" \
  | awk '{print $4}' \
  | while read -r key; do
      d=$(echo "$key" | sed -n 's/gaia-\([0-9]\{8\}\)T.*/\1/p')
      [ -n "$d" ] && [ "$d" -lt "$CUTOFF" ] && \
        aws s3 rm "s3://${SPACES_BUCKET}/backups/${key}" --endpoint-url "${SPACES_ENDPOINT}"
    done
echo "backed up ${FILE##*/}"
