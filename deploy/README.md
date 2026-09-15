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
Postgres publishes no ports and is reachable only on the compose network —
`docker-compose.yml` has no `ports:` block on `db`, and there is deliberately no
auto-loaded `docker-compose.override.yml` that could quietly add one here. The
only thing that publishes 5432 is `deploy/compose.dev.yml`, which compose does
not load unless you name it with `-f`; it exists for the test suite on a
developer's machine and must never be passed on the droplet.

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
4. **A `daily_digest` utility template, one body parameter, language `en`.**
   Register it as `en`, not `en_US` — `gaia/core/whatsapp.py` sends
   `{"code": "en"}` and Meta matches the language tag exactly, so a template
   approved under `en_US` is simply not found at send time. This is a
   prerequisite, not a nice-to-have: WhatsApp only allows free-form
   (non-template) sends within 24 hours of the user's last inbound message —
   outside that window, a free-form send is rejected outright. A
   newly-onboarded agent who has never texted the number has no open window
   at all, so their very first morning digest has no path to delivery except
   through this approved template. Submit it early — approval time is not
   under your control, and an agent added the day before go-live with no
   template in place gets no digest.

## Google

Needed only for the calendar feature, and, like Meta, better done before the
deploy than during it. Everything here happens in the Google Cloud console,
under the **Gaia Workspace organisation** — a project created under a personal
account cannot be made Internal, and that single setting is what the whole
arrangement rests on.

1. **Create the project** inside the organisation. Check the org name at the
   top of the console; a project sitting under "No organisation" is the one
   mistake here that cannot be corrected later without starting over.
2. **Enable the Google Calendar API** for it.
3. **Configure the OAuth consent screen as `Internal`.** Internal is what
   exempts the app from [Google's verification
   review](https://developers.google.com/identity/protocols/oauth2/requirements)
   — no unverified-app interstitial, no 100-user cap, and none of the annual
   CASA security assessment that a restricted scope would otherwise drag in.
   **It holds only while every user is on the Workspace domain.** One
   contractor on a personal Gmail forces External, and External in Testing
   expires refresh tokens seven days after consent, which means every
   developer reconnecting weekly, forever.
4. **Add the scopes.** Three, and no more:

   ```
   openid
   email
   https://www.googleapis.com/auth/calendar.events.owned
   ```

   `calendar.events.owned` is "see, create, change, and delete events on
   Google calendars you own" — own-calendar-only, so no domain-wide
   delegation and no key that can impersonate anyone in the organisation.
   `openid` and `email` are non-sensitive and are how the callback learns
   which account consented: they make the token response carry an `id_token`,
   whose signed `hd` claim is what the domain check actually tests. Without
   them the flow has no way to identify the consenting account at all.
5. **Create an OAuth client** of type *Web application*. Register exactly one
   authorized redirect URI:

   ```
   https://<DOMAIN>/oauth/callback
   ```

   Google matches this string exactly — scheme, host and path, no trailing
   slash. It must equal `https://$DOMAIN/oauth/callback` with the same
   `DOMAIN` that is in `.env`, because that is what `settings.domain` builds
   the redirect from. A mismatch fails at Google's own screen with
   `redirect_uri_mismatch`, before the callback is ever reached.

   **Web application, not Desktop app.** The desktop type is what a loopback
   probe uses and it is the one muscle memory reaches for. It offers no
   redirect URI field at all — Google permits only loopback redirects for it —
   so every consent fails with that same `redirect_uri_mismatch`, and the
   client's own page gives no hint why, because there is nothing on it to
   edit. On the phone it reads "Access blocked: This app's request is
   invalid", with whatever account Google happened to have signed in shown
   underneath, which sends you chasing the wrong account instead of the wrong
   client. The first live deploy lost an hour to exactly this. If the client's
   header reads "Client ID for Desktop", delete it and create a Web
   application one; the id and secret change, so `.env` on the droplet does
   too.
6. **Put the client id and secret in `.env`** as `GOOGLE_CLIENT_ID` and
   `GOOGLE_CLIENT_SECRET`, generate `GOOGLE_TOKEN_KEY`, and set
   `GOOGLE_DOMAIN` to the Workspace domain. See `.env.example`.

**After the deploy, before announcing the feature**, every existing developer
needs an address on their user row — `docs/RUNBOOK.md` → "Deploy the Google
Workspace integration" has the order and the reason. The callback refuses any
account that does not match it, so announcing first produces a developer who
cannot connect and does not know why.

## First run

```bash
git clone <repo> && cd gaia-assistant
cp .env.example .env      # fill in
# DB_PASSWORD ships as CHANGE_ME_... and has no default in docker-compose.yml:
# compose refuses to start until you replace it. Generate one:
openssl rand -base64 32
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

`--tz` is validated against the IANA database before the row is written. It
used to be any string, and a typo was not a typo: `ZoneInfo()` raises inside
the digest's per-user loop, so one bad row meant nobody in the company got a
digest again, and that agent got an error reply to every message she sent.

## What it costs to run

    docker compose exec -T app python -m gaia.core.admin stats
    docker compose exec -T app python -m gaia.core.admin stats --days 7

For a page you can open on a laptop or a phone, pull it down over the ssh
session you already have — no scp step, no new port, nothing listening:

    ssh gaia 'cd /opt/gaia-assistant && docker compose exec -T app \
        python -m gaia.core.admin stats --html' > report.html

The report carries aggregates only — no contact names and no meeting text — so
it is safe to screenshot and send to someone. `llm_calls` is the one table in
the schema with nowhere to put a client in it.

Cost is computed when the report is read, from the rate card in
`gaia/core/stats.py`. Edit that dict when Anthropic's prices change and every
historical row reprices correctly; a model with no entry there reports its
tokens with no cost and is named in the output, rather than being priced from
the wrong card.

The number worth watching first is the **cache hit rate**. `core/llm.py`'s
`_system_blocks` puts the cache breakpoint after the stable prefix on the
argument that the contact roster reorders on nearly every `save_meeting`, and
until now that was a belief rather than a measurement.

## Merging duplicate contacts

Two rows for the same person happen: the index on `lower(name)` is
deliberately non-unique — different people share a name often enough that a
unique constraint would force bad data — and two agents filing meetings with
the same new client at the same moment can each create one.

```bash
# find the duplicates and their ids
docker compose exec db psql -U gaia -d gaia -c \
  "SELECT id, name, phone, left(profile, 60) FROM contacts ORDER BY lower(name), created_at"

docker compose exec app python -m gaia.core.admin merge-contacts \
    --from <id to delete> --into <id to keep> --as <agent's phone>
```

`--as` is required and is not decoration: the merge is scoped to what that
agent can see, so nobody with a shell can fold an agent's *private* contact
into an org row and publish its profile to the whole company. Merging is
lossy in one respect only — `profile` is an append-only string, so the two
histories are concatenated rather than interleaved. Everything else moves:
leads, commitments, memory chunks, meeting links, and any phone or email the
survivor was missing.

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

A successful run logs one line naming the dump it uploaded and how many old
objects it pruned, e.g. `backed up gaia-20260908T030001Z.dump; pruned 1
backup(s) dated before 20260809`. `pruned 0` on a bucket older than 30 days
means retention is not working, which is worth noticing — a silent no-op and a
working prune must not look the same.

Requires in `.env`: `SPACES_BUCKET`, `SPACES_ENDPOINT`, `AWS_ACCESS_KEY_ID`,
`AWS_SECRET_ACCESS_KEY` (a Spaces access key pair from the DO control panel —
the `aws` CLI reads those two variable names natively, no extra config
needed).

## Upgrades

`pgvector/pgvector:pg17` is pinned deliberately. A major-version bump makes the
existing data directory unreadable and Postgres refuses to start; moving to pg18
means a dump, a fresh volume, and a restore — exactly the drill above, done on
purpose instead of under duress.
