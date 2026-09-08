# Deploying gaia-butler

gaia-butler holds a real-estate agency's client book — names, budgets, and
what sellers said in confidence. Treat backups and access control here as
seriously as that implies.

## Droplet

Ubuntu 24.04 LTS, 2GB / 1 vCPU / 50GB, NYC region.

```bash
# 2GB swap — Postgres, Python and Caddy on 2GB have no headroom otherwise
fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
echo '/swapfile none swap sw 0 0' >> /etc/fstab

curl -fsSL https://get.docker.com | sh
```

**DO cloud firewall:** inbound 80 and 443 from anywhere, 22 from your IP only.
Postgres publishes no ports and is reachable only on the compose network.

## DNS

An A record for `$DOMAIN` pointing at the droplet. Caddy provisions TLS on first
boot; it needs port 80 reachable to do so.

**Caddy's TLS/ACME step is unverified in this build.** Everything else in this
runbook has been exercised hands-on against the local stack — the app resolves
`db:5432`, migrations run at startup and log `applied migrations: 001_init.sql`,
`/health` answers `{"status":"ok"}` from inside the app container, and Caddy
correctly reaches `app:8000` and 308-redirects HTTP to HTTPS. But ACME
certificate issuance needs a real domain and public DNS, which only exist at
the first real deploy. Locally the webhook could only be exercised against
`app:8000` directly, never through Caddy over HTTPS. **The first real deploy
must confirm Caddy actually obtains a certificate and that Meta's webhook
verification succeeds through `https://$DOMAIN/webhook`** before you consider
the deploy done.

## Meta

Do 1, 2 and 4 now — none of them need the app running, and both the token
and the template have approval lead times outside your control. **Step 3
needs a live app and comes after "First run" below** — do not attempt it
yet.

1. A phone number not already on personal WhatsApp.
2. A permanent access token via Business Settings → System Users.
3. *(after "First run")* Webhook `https://$DOMAIN/webhook`, verify token
   matching `WA_VERIFY_TOKEN`, subscribed to `messages`. Meta calls this URL
   with a challenge to confirm it before accepting the subscription, so
   Caddy and the app must already be up and answering — see the "Return to
   Meta" note at the end of First run.
4. **A `daily_digest` utility template, one body parameter.** This is a
   prerequisite, not a nice-to-have: WhatsApp only allows free-form
   (non-template) sends within 24 hours of the user's last inbound message —
   outside that window, a free-form send is rejected outright. A
   newly-onboarded agent who has never texted the number has no open window
   at all, so their very first morning digest has no path to delivery except
   through this approved template. Submit it early — approval time is not
   under your control, and an agent added the day before go-live with no
   template in place gets no digest.

## First run

```bash
git clone <repo> && cd gaia-assistant
cp .env.example .env      # fill in
chmod 600 .env             # it holds API keys, WhatsApp secrets, and Spaces
                            # credentials — restrict it to the deploying user
docker compose up -d --build

docker compose exec app python -m gaia.core.admin \
    add-user --name "<name>" --phone <number> --role admin
```

**Return to Meta, step 3.** The app and Caddy are up now, so go back and
finish the webhook configuration — point it at `https://$DOMAIN/webhook`
and confirm Meta's verification handshake succeeds before moving on.

## Before going live: prove the restore

An untested backup is not a backup. Do not wait for an incident to discover
whether `restore.sh` actually works — run it now, against a real dump, and
confirm the numbers.

```bash
docker compose exec backup /deploy/backup.sh
docker compose exec backup /deploy/restore.sh /tmp/gaia-<stamp>.dump
```

Row counts must match production. This is the step people skip and regret.

## Adding an agent

```bash
docker compose exec app python -m gaia.core.admin add-user --name "Ana" --phone 13055550001
docker compose exec app python -m gaia.core.admin list-users
docker compose exec app python -m gaia.core.admin deactivate --phone 13055550001
```

`deactivate` is **immediate revocation** — the response to a lost or stolen
phone. In this system a phone number is the credential: whoever holds the SIM
(or has cloned the number) can act as that agent the moment a message arrives.
There is no separate password to change and no session to expire, so
`deactivate` is the entire incident response. Run it the moment a phone goes
missing, not after confirming anything else.

## Backups

Nightly `pg_dump -Fc` to DigitalOcean Spaces, 30-day retention, run by the
`backup` compose service (see `docker-compose.yml` and `deploy/backup.sh`).
DigitalOcean's own droplet backups run weekly — losing up to six days of
meeting notes and client conversations is not an acceptable worst case for
this data, so the nightly job exists independently of that.

**Expect a dump at 03:00 America/New_York, every night**, not "24 hours
after the container last started." The service computes seconds until the
next wall-clock 03:00 and sleeps to it, recomputing fresh each time through
the loop — a restart at 3pm does not shift the schedule to 3pm, and a slow
backup one night does not push the next one later. If a night's backup
fails, `backup failed` is logged and the loop continues to the next 03:00
rather than dying; check `docker compose logs backup` if you suspect a
night was missed.

Requires in `.env`: `SPACES_BUCKET`, `SPACES_ENDPOINT`, `AWS_ACCESS_KEY_ID`,
`AWS_SECRET_ACCESS_KEY` (a Spaces access key pair from the DO control panel —
the `aws` CLI reads those two variable names natively, no extra config
needed).

## Upgrades

`pgvector/pgvector:pg17` is pinned deliberately. A major-version bump makes the
existing data directory unreadable and Postgres refuses to start; moving to pg18
means a dump, a fresh volume, and a restore — exactly the drill above, done on
purpose instead of under duress.
