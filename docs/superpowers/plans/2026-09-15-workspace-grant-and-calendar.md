# Workspace Grant and Calendar Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a developer connect their Google Workspace account over WhatsApp, then have Gaia schedule Meet-backed events, invite people only after explicit approval, tie events to leads and commitments both ways, and flag tomorrow's conflicts in the morning digest.

**Architecture:** No Google state is mirrored into Postgres. Postgres holds only the grant (`google_accounts`), the approval record (`pending_invites`), and link ids on existing domain rows; everything about a calendar is read live. The approval gate is durable rather than a prompt rule: `propose_invite` stores the exact address list and `confirm_invite` accepts no addresses at all, so the model cannot confirm a list the human never saw.

**Tech Stack:** Python 3.11+, FastAPI, psycopg 3 (async, `dict_row`), httpx, `cryptography` (new), pytest + pytest-asyncio, Postgres 17 + pgvector.

**Spec:** `docs/superpowers/specs/2026-09-14-workspace-grant-and-calendar-design.md`
**Research (every API claim was measured):** `docs/superpowers/research/2026-09-14-google-workspace.md`

## Global Constraints

- **TDD.** Write the test, watch it fail *for the right reason*, then implement. If a test passes the moment you write it, say so — it is a contract test, not a regression guard.
- **No test-aware production code.** No `if TESTING`, no env var only tests set, no parameter defaulting to off whose only caller is a test. Pass a seam — a *required* parameter — the way `usage.record` takes `pool`.
- **Everything in `gaia/core/db/` takes `(conn, user, ...)`.** `tests/test_db_signatures.py` enforces it.
- **Every read in `gaia/core/db/` either composes `visible()` or is listed in `OWNERSHIP_SCOPED` with a reason.** `tests/test_scope.py` enforces it. None of the reads in this plan compose `visible()`; all of them are ownership-scoped, and each entry must say why.
- **`google_accounts` and `pending_invites` must NOT have a `visibility` column.** `tests/test_migrate.py` asserts the set of tables carrying one equals `DOMAIN_TABLES` exactly. Omitting the column is the mechanism that keeps them out of `DOMAIN_TABLES` and out of `test_isolation.py`.
- **Exactly one OAuth scope this increment:** `https://www.googleapis.com/auth/calendar.events.owned`. Not `calendar.freebusy` — held alongside `events.owned` it restricts nothing, because that token already has full read.
- **Comments carry reasoning, not description.** Explain why a line exists and what went wrong without it.
- **Run `.venv/bin/python -m pytest -q` before every commit.** Tests need the dev database: `docker compose -f docker-compose.yml -f deploy/compose.dev.yml up -d db`.
- **Never commit a secret.** New settings go in `.env` (gitignored) and are named — never valued — in `docs/RUNBOOK.md`.

## File Structure

**Created:**

| Path | Responsibility |
|---|---|
| `migrations/006_google_workspace.sql` | `users.email`, link columns, `google_accounts`, `pending_invites` |
| `gaia/core/crypto.py` | Fernet encrypt/decrypt for refresh tokens at rest |
| `gaia/core/oauth_link.py` | Mint and verify the one-time WhatsApp link token bound to a `user_id` |
| `gaia/core/google.py` | Access-token refresh and a thin authenticated request helper |
| `gaia/core/db/google_accounts.py` | Store, read and revoke a developer's grant |
| `gaia/core/db/pending_invites.py` | The durable approval record |
| `gaia/capabilities/calendar/__init__.py` | `Capability` registration, tool schemas, prompt fragment |
| `gaia/capabilities/calendar/tools.py` | Tool handlers |
| `gaia/capabilities/calendar/client.py` | Calendar REST calls — the only place an event payload is built |
| `gaia/jobs/conflicts.py` | Pure interval arithmetic: overlaps, free minutes |

**Modified:** `gaia/core/config.py`, `gaia/core/db/users.py`, `gaia/core/db/leads.py`, `gaia/core/db/commitments.py`, `gaia/core/admin.py`, `gaia/main.py`, `gaia/jobs/digest.py`, `gaia/capabilities/__init__.py`, `tests/test_scope.py`, `pyproject.toml`, `docs/RUNBOOK.md`.

---

### Task 1: Migration 006

**Files:**
- Create: `migrations/006_google_workspace.sql`
- Test: `tests/test_workspace_schema.py`

**Interfaces:**
- Consumes: nothing.
- Produces: tables `google_accounts`, `pending_invites`; columns `users.email`, `leads.calendar_event_id`, `commitments.calendar_event_id`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_workspace_schema.py
"""Migration 006. The visibility assertion is the important one: these two
tables are infrastructure, not client data, and a visibility column on either
would silently enrol them in DOMAIN_TABLES and the isolation suite."""

import pytest


async def _columns(conn, table: str) -> set[str]:
    cur = await conn.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name = %s",
        (table,),
    )
    return {r["column_name"] for r in await cur.fetchall()}


async def test_google_accounts_shape(conn):
    cols = await _columns(conn, "google_accounts")
    assert {"user_id", "google_email", "refresh_token_enc",
            "scopes", "granted_at", "revoked_at"} <= cols


async def test_pending_invites_shape(conn):
    cols = await _columns(conn, "pending_invites")
    assert {"id", "user_id", "event_id", "emails",
            "created_at", "expires_at", "confirmed_at"} <= cols


@pytest.mark.parametrize("table", ["google_accounts", "pending_invites"])
async def test_new_tables_have_no_visibility_column(conn, table):
    """Deliberate. A token answers 'whose account is this' and a pending invite
    is one person's workflow; neither is org-visible content at any setting."""
    assert "visibility" not in await _columns(conn, table)


async def test_link_columns_exist(conn):
    assert "calendar_event_id" in await _columns(conn, "leads")
    assert "calendar_event_id" in await _columns(conn, "commitments")
    assert "email" in await _columns(conn, "users")
```

- [ ] **Step 2: Run it and watch it fail**

Run: `.venv/bin/python -m pytest tests/test_workspace_schema.py -q`
Expected: FAIL — `google_accounts` does not exist, so `_columns` returns an empty set.

- [ ] **Step 3: Write the migration**

```sql
-- migrations/006_google_workspace.sql

-- The address the OAuth callback must match. A precondition for connecting,
-- never something the callback fills in: if first connect defined the answer,
-- a colleague with a forwarded link would pass the domain check and bind their
-- own account -- and Gaia would then create this developer's events, client
-- names and all, on someone else's calendar.
ALTER TABLE users ADD COLUMN email TEXT UNIQUE;

-- The event for the current next action, not a history. What already happened
-- lives in meetings; a second home for the same truth is how the two drift.
ALTER TABLE leads       ADD COLUMN calendar_event_id TEXT;
ALTER TABLE commitments ADD COLUMN calendar_event_id TEXT;

-- No visibility column: a token is owner-only at every setting, and nobody may
-- read a colleague's. Same reasoning as messages, one table stronger.
-- ON DELETE CASCADE, unlike the RESTRICT used for domain rows: a refresh token
-- outliving its user is a live credential with no owner, which is worse than a
-- blocked delete.
CREATE TABLE google_accounts (
    user_id           UUID PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    google_email      TEXT NOT NULL,
    refresh_token_enc TEXT NOT NULL,
    scopes            TEXT NOT NULL,
    granted_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    revoked_at        TIMESTAMPTZ
);

-- No visibility column: one person's pending workflow, not org content.
-- emails is the list the human actually saw. confirm_invite sends THIS, and
-- takes no addresses of its own, so the model cannot confirm a different list
-- than the one that was read back.
CREATE TABLE pending_invites (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id      UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    event_id     TEXT NOT NULL,
    emails       TEXT[] NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at   TIMESTAMPTZ NOT NULL,
    confirmed_at TIMESTAMPTZ
);
CREATE INDEX idx_pending_invites_open ON pending_invites (user_id, expires_at)
    WHERE confirmed_at IS NULL;
```

- [ ] **Step 4: Run the whole suite**

Run: `.venv/bin/python -m pytest -q`
Expected: PASS. `tests/test_migrate.py` and `tests/test_scope.py` both check that tables carrying a `visibility` column equal `DOMAIN_TABLES` exactly — if either fails, you added a `visibility` column you should not have.

- [ ] **Step 5: Commit**

```bash
git add migrations/006_google_workspace.sql tests/test_workspace_schema.py
git commit -m "feat: schema for the Google grant, approval records and event links"
```

---

### Task 2: Refresh tokens encrypted at rest

**Files:**
- Create: `gaia/core/crypto.py`
- Modify: `gaia/core/config.py`, `pyproject.toml`
- Test: `tests/test_crypto.py`

**Interfaces:**
- Consumes: `settings.google_token_key`.
- Produces: `encrypt(plaintext: str) -> str`, `decrypt(token: str) -> str`. Both raise `RuntimeError` when no key is configured.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_crypto.py
import pytest

from gaia.core import crypto


def test_round_trip(monkeypatch):
    from cryptography.fernet import Fernet
    monkeypatch.setattr(crypto.settings, "google_token_key", Fernet.generate_key().decode())
    assert crypto.decrypt(crypto.encrypt("1//refresh-token")) == "1//refresh-token"


def test_ciphertext_is_not_the_plaintext(monkeypatch):
    """The point of the exercise. A database dump that leaks must not hand
    over live Google credentials -- the key lives in .env, not in the dump."""
    from cryptography.fernet import Fernet
    monkeypatch.setattr(crypto.settings, "google_token_key", Fernet.generate_key().decode())
    assert "1//refresh-token" not in crypto.encrypt("1//refresh-token")


def test_missing_key_fails_loudly_at_use_not_import(monkeypatch):
    """Empty by default so a checkout with no Google feature configured still
    imports and boots -- same reasoning as deepgram_api_key. It must then fail
    visibly rather than storing something reversible."""
    monkeypatch.setattr(crypto.settings, "google_token_key", "")
    with pytest.raises(RuntimeError, match="GOOGLE_TOKEN_KEY"):
        crypto.encrypt("anything")
```

- [ ] **Step 2: Run it and watch it fail**

Run: `.venv/bin/python -m pytest tests/test_crypto.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'gaia.core.crypto'`.

- [ ] **Step 3: Add the dependency and the setting**

In `pyproject.toml`, add to `dependencies`:

```toml
    "cryptography>=43.0",
```

Then install: `.venv/bin/pip install -e .`

In `gaia/core/config.py`, add inside `Settings`:

```python
    # Fernet key encrypting Google refresh tokens at rest. Empty by default so a
    # checkout with no Workspace integration configured still imports and boots.
    #
    # Worth being honest about what this buys: nothing against someone who owns
    # the droplet, since they hold both this and DATABASE_URL. It is aimed at the
    # realistic leak -- a database dump or backup leaving the box -- where the
    # key is not in the dump. Generate one with:
    #   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    google_token_key: str = ""
```

- [ ] **Step 4: Write the implementation**

```python
# gaia/core/crypto.py
"""Encryption for credentials at rest. Only refresh tokens use it today."""

from cryptography.fernet import Fernet

from gaia.core.config import settings


def _cipher() -> Fernet:
    if not settings.google_token_key:
        raise RuntimeError(
            "GOOGLE_TOKEN_KEY is not set. Refusing to store a credential in "
            "plaintext -- generate a key with Fernet.generate_key()."
        )
    return Fernet(settings.google_token_key.encode())


def encrypt(plaintext: str) -> str:
    return _cipher().encrypt(plaintext.encode()).decode()


def decrypt(token: str) -> str:
    return _cipher().decrypt(token.encode()).decode()
```

- [ ] **Step 5: Run and commit**

Run: `.venv/bin/python -m pytest tests/test_crypto.py -q` — Expected: PASS.

```bash
git add gaia/core/crypto.py gaia/core/config.py pyproject.toml tests/test_crypto.py
git commit -m "feat: encrypt credentials at rest, for the refresh tokens coming next"
```

---

### Task 3: The grant table

**Files:**
- Create: `gaia/core/db/google_accounts.py`
- Modify: `tests/test_scope.py` (add `OWNERSHIP_SCOPED` entries)
- Test: `tests/test_google_accounts.py`

**Interfaces:**
- Consumes: `crypto.encrypt` / `crypto.decrypt` (Task 2).
- Produces:
  - `async def upsert(conn, user, *, google_email: str, refresh_token: str, scopes: str) -> None`
  - `async def get(conn, user) -> dict | None` — keys `google_email`, `refresh_token`, `scopes`, `revoked_at`; refresh token already decrypted
  - `async def revoke(conn, user) -> None`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_google_accounts.py
from cryptography.fernet import Fernet
import pytest

from gaia.core import crypto
from gaia.core.db import google_accounts as ga


@pytest.fixture(autouse=True)
def key(monkeypatch):
    monkeypatch.setattr(crypto.settings, "google_token_key", Fernet.generate_key().decode())


async def test_round_trip(conn, ana):
    await ga.upsert(conn, ana, google_email="ana@gaiagroupdevelopment.com",
                    refresh_token="1//tok", scopes="calendar.events.owned")
    got = await ga.get(conn, ana)
    assert got["google_email"] == "ana@gaiagroupdevelopment.com"
    assert got["refresh_token"] == "1//tok"
    assert got["revoked_at"] is None


async def test_stored_column_is_encrypted(conn, ana):
    await ga.upsert(conn, ana, google_email="a@x.com", refresh_token="1//tok", scopes="s")
    cur = await conn.execute("SELECT refresh_token_enc FROM google_accounts")
    assert "1//tok" not in (await cur.fetchone())["refresh_token_enc"]


async def test_one_user_cannot_see_anothers_grant(conn, ana, sofia):
    """Ownership, not visibility. There is no setting at which a colleague's
    token is readable, which is why this table has no visibility column."""
    await ga.upsert(conn, ana, google_email="a@x.com", refresh_token="1//tok", scopes="s")
    assert await ga.get(conn, sofia) is None


async def test_reconnect_replaces_the_grant(conn, ana):
    """A second consent must not leave the revoked token behind, and must clear
    revoked_at -- otherwise reconnecting appears to work and then fails."""
    await ga.upsert(conn, ana, google_email="a@x.com", refresh_token="1//old", scopes="s")
    await ga.revoke(conn, ana)
    await ga.upsert(conn, ana, google_email="a@x.com", refresh_token="1//new", scopes="s")
    got = await ga.get(conn, ana)
    assert got["refresh_token"] == "1//new" and got["revoked_at"] is None


async def test_revoke_marks_rather_than_deletes(conn, ana):
    """Kept so the digest can say 'your calendar disconnected' once, rather
    than silently losing the fact that it ever existed."""
    await ga.upsert(conn, ana, google_email="a@x.com", refresh_token="1//tok", scopes="s")
    await ga.revoke(conn, ana)
    assert (await ga.get(conn, ana))["revoked_at"] is not None
```

- [ ] **Step 2: Run it and watch it fail**

Run: `.venv/bin/python -m pytest tests/test_google_accounts.py -q`
Expected: FAIL — `No module named 'gaia.core.db.google_accounts'`.

- [ ] **Step 3: Write the module**

```python
# gaia/core/db/google_accounts.py
"""One developer's Google grant.

Ownership-scoped throughout, and deliberately so: a token answers *whose
account is this*, which is responsibility, never *who may see this*. There is
no visibility setting at which a colleague's refresh token is readable, which
is why the table has no visibility column at all.
"""

from gaia.core import crypto
from gaia.core.models import User


async def upsert(conn, user: User, *, google_email: str, refresh_token: str, scopes: str) -> None:
    """Re-consenting replaces everything, including revoked_at. A reconnect
    that left the old revoked_at in place would look like it worked and then
    behave as though it had not."""
    await conn.execute(
        """INSERT INTO google_accounts (user_id, google_email, refresh_token_enc, scopes)
           VALUES (%s,%s,%s,%s)
           ON CONFLICT (user_id) DO UPDATE
             SET google_email      = EXCLUDED.google_email,
                 refresh_token_enc = EXCLUDED.refresh_token_enc,
                 scopes            = EXCLUDED.scopes,
                 granted_at        = now(),
                 revoked_at        = NULL""",
        (user.id, google_email, crypto.encrypt(refresh_token), scopes),
    )


async def get(conn, user: User) -> dict | None:
    cur = await conn.execute(
        """SELECT google_email, refresh_token_enc, scopes, revoked_at
           FROM google_accounts WHERE user_id = %s""",
        (user.id,),
    )
    row = await cur.fetchone()
    if row is None:
        return None
    return {
        "google_email": row["google_email"],
        "refresh_token": crypto.decrypt(row["refresh_token_enc"]),
        "scopes": row["scopes"],
        "revoked_at": row["revoked_at"],
    }


async def revoke(conn, user: User) -> None:
    """Marked, not deleted. The digest says so once; deleting the row would
    lose the fact that a grant ever existed, and with it the ability to tell
    'never connected' from 'connection lapsed'."""
    await conn.execute(
        "UPDATE google_accounts SET revoked_at = now() WHERE user_id = %s", (user.id,)
    )
```

- [ ] **Step 4: Declare the scoping, or the build breaks**

In `tests/test_scope.py`, add to `OWNERSHIP_SCOPED`:

```python
    "gaia.core.db.google_accounts.get":
        "one developer's own OAuth grant. Not visibility-scoped because there "
        "is no setting at which a colleague's refresh token is readable -- a "
        "token answers whose account this is, which is ownership, never who "
        "may see it. The table has no visibility column for the same reason.",
```

- [ ] **Step 5: Run the full suite and commit**

Run: `.venv/bin/python -m pytest -q` — Expected: PASS, including `test_scope.py` and `test_db_signatures.py`.

```bash
git add gaia/core/db/google_accounts.py tests/test_google_accounts.py tests/test_scope.py
git commit -m "feat: store a developer's Google grant, encrypted and owner-only"
```

---

### Task 4: An address for each developer

**Files:**
- Modify: `gaia/core/db/users.py`, `gaia/core/admin.py`
- Test: `tests/test_user_email.py`

**Interfaces:**
- Consumes: `users.email` (Task 1).
- Produces: `async def set_email(conn, *, wa_id: str, email: str) -> bool`; `create_user(..., email: str | None = None)`; CLI `add-user --email`, `set-email --phone --email`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_user_email.py
"""users.email is a precondition for connecting a calendar, so it needs a way
in. admin.py had none: add-user took name, phone, role and tz. Every user that
already exists predates the column, which is why set-email is not optional."""

from gaia.core.admin import build_parser, _run
from gaia.core.db import users as users_db


async def test_create_user_accepts_an_email(conn):
    user = await users_db.create_user(
        conn, name="Ana", wa_id="13055559001", email="ana@gaiagroupdevelopment.com"
    )
    assert await users_db.get_email(conn, user) == "ana@gaiagroupdevelopment.com"


async def test_set_email_backfills_an_existing_user(conn, ana):
    assert await users_db.set_email(conn, wa_id=ana.wa_id, email="ana@gaiagroupdevelopment.com")
    assert await users_db.get_email(conn, ana) == "ana@gaiagroupdevelopment.com"


async def test_set_email_reports_an_unknown_number(conn):
    assert await users_db.set_email(conn, wa_id="19999999999", email="x@y.com") is False


async def test_cli_parses_set_email():
    args = build_parser().parse_args(
        ["set-email", "--phone", "13055550001", "--email", "a@gaiagroupdevelopment.com"]
    )
    assert args.command == "set-email" and args.email == "a@gaiagroupdevelopment.com"


async def test_cli_set_email_updates_the_row(migrated, ana, capsys):
    args = build_parser().parse_args(
        ["set-email", "--phone", ana.wa_id, "--email", "ana@gaiagroupdevelopment.com"]
    )
    await _run(args, pool=migrated)
    assert "ana@gaiagroupdevelopment.com" in capsys.readouterr().out
```

- [ ] **Step 2: Run it and watch it fail**

Run: `.venv/bin/python -m pytest tests/test_user_email.py -q`
Expected: FAIL — `create_user() got an unexpected keyword argument 'email'`.

- [ ] **Step 3: Extend `users.py`**

In `gaia/core/db/users.py`, add `email` to the insert and add two functions. `_COLUMNS` and `_row_to_user` stay untouched — `User` is what the agent loop carries around, and an address it never uses does not belong in it.

```python
async def create_user(
    conn,
    *,
    name: str,
    wa_id: str,
    role: str = "developer",
    timezone: str = "America/New_York",
    email: str | None = None,
) -> User:
    cur = await conn.execute(
        f"""INSERT INTO users (name, wa_id, role, timezone, email)
            VALUES (%s, %s, %s, %s, %s) RETURNING {_COLUMNS}""",
        (name, wa_id, role, timezone, email),
    )
    return _row_to_user(await cur.fetchone())


async def get_email(conn, user: User) -> str | None:
    """The address the OAuth callback must match. Ownership-scoped: this is
    one person's own identity, not org-visible content."""
    cur = await conn.execute("SELECT email FROM users WHERE id = %s", (user.id,))
    row = await cur.fetchone()
    return row["email"] if row else None


async def set_email(conn, *, wa_id: str, email: str) -> bool:
    """Admin-only backfill. Keyed by wa_id rather than by User because the
    caller is the CLI, which knows a phone number and nothing else."""
    cur = await conn.execute(
        "UPDATE users SET email = %s WHERE wa_id = %s RETURNING id", (email, wa_id)
    )
    return await cur.fetchone() is not None
```

- [ ] **Step 4: Extend the CLI**

In `gaia/core/admin.py`, inside `build_parser()`, add `--email` to the existing `add-user` parser and register a new subcommand:

```python
    add.add_argument("--email", help="Workspace address; required before connecting a calendar")

    set_email = sub.add_parser("set-email", help="Set or correct a user's Workspace address")
    set_email.add_argument("--phone", required=True, help="country code, no '+'")
    set_email.add_argument("--email", required=True)
```

In `_run()`, pass it through on creation and handle the new command:

```python
            if args.command == "add-user":
                user = await users_db.create_user(
                    conn, name=args.name, wa_id=args.phone,
                    role=args.role, timezone=args.tz, email=args.email,
                )
                print(f"created {user.name} ({user.wa_id})")
            elif args.command == "set-email":
                if await users_db.set_email(conn, wa_id=args.phone, email=args.email):
                    print(f"{args.phone} -> {args.email}")
                else:
                    print(f"no user with phone {args.phone}")
```

Match the surrounding `print` style if it differs; the assertions above only require the address to appear in stdout.

- [ ] **Step 5: Run and commit**

Run: `.venv/bin/python -m pytest -q` — Expected: PASS.

```bash
git add gaia/core/db/users.py gaia/core/admin.py tests/test_user_email.py
git commit -m "feat: give users an address, and the CLI a way to set one

The OAuth callback matches against it, so every existing user needs a backfill
before the calendar feature can be announced."
```

---

### Task 5: The one-time link token

**Files:**
- Create: `gaia/core/oauth_link.py`
- Test: `tests/test_oauth_link.py`

**Interfaces:**
- Consumes: `settings.google_token_key`.
- Produces: `mint(user_id: UUID, *, ttl_seconds: int = 600) -> str`; `verify(token: str) -> UUID | None`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_oauth_link.py
"""The token Gaia puts in a WhatsApp link. It is a bearer credential for its
whole lifetime, so it is signed, bound to exactly one user, and short."""

import time
from uuid import uuid4

from cryptography.fernet import Fernet
import pytest

from gaia.core import oauth_link


@pytest.fixture(autouse=True)
def key(monkeypatch):
    monkeypatch.setattr(oauth_link.settings, "google_token_key", Fernet.generate_key().decode())


def test_round_trip():
    uid = uuid4()
    assert oauth_link.verify(oauth_link.mint(uid)) == uid


def test_expired_token_is_refused():
    assert oauth_link.verify(oauth_link.mint(uuid4(), ttl_seconds=-1)) is None


def test_tampered_payload_is_refused():
    """Swapping the user id must not survive the signature -- otherwise anyone
    holding one link could bind an account to any user they liked."""
    token = oauth_link.mint(uuid4())
    body, sig = token.split(".", 1)
    forged = oauth_link.mint(uuid4()).split(".", 1)[0]
    assert oauth_link.verify(f"{forged}.{sig}") is None


def test_garbage_is_refused():
    for junk in ["", "nodot", "a.b", "...."]:
        assert oauth_link.verify(junk) is None


def test_signature_comparison_is_constant_time():
    """hmac.compare_digest, not ==. A timing oracle on this signature is a
    forged link, and a forged link is someone else's calendar."""
    import inspect
    assert "compare_digest" in inspect.getsource(oauth_link.verify)
```

- [ ] **Step 2: Run it and watch it fail**

Run: `.venv/bin/python -m pytest tests/test_oauth_link.py -q`
Expected: FAIL — `No module named 'gaia.core.oauth_link'`.

- [ ] **Step 3: Write the module**

```python
# gaia/core/oauth_link.py
"""The one-time link Gaia sends over WhatsApp to start a Google consent flow.

Consent happens in a browser; Gaia knows people only by wa_id. Nothing links
the two, and this token is that link: signed, bound to one user_id, and valid
for ten minutes.

Domain-separated from crypto.py's use of the same secret by the HMAC prefix
below -- the key encrypts refresh tokens there and authenticates link payloads
here, and those must never be interchangeable.
"""

import base64
import hmac
import json
import time
from hashlib import sha256
from uuid import UUID

from gaia.core.config import settings

_PREFIX = b"gaia-oauth-link-v1"


def _sign(body: bytes) -> str:
    if not settings.google_token_key:
        raise RuntimeError("GOOGLE_TOKEN_KEY is not set; cannot sign a consent link.")
    mac = hmac.new(_PREFIX + settings.google_token_key.encode(), body, sha256)
    return base64.urlsafe_b64encode(mac.digest()).decode().rstrip("=")


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def mint(user_id: UUID, *, ttl_seconds: int = 600) -> str:
    body = json.dumps({"u": str(user_id), "e": int(time.time()) + ttl_seconds}).encode()
    return f"{_b64(body)}.{_sign(body)}"


def verify(token: str) -> UUID | None:
    """None for anything wrong -- expired, tampered, malformed. The caller
    cannot usefully distinguish them and neither can the person holding the
    link, so they all get the same answer."""
    try:
        encoded, sig = token.split(".", 1)
        body = _unb64(encoded)
        if not hmac.compare_digest(sig, _sign(body)):
            return None
        claims = json.loads(body)
        if int(claims["e"]) < time.time():
            return None
        return UUID(claims["u"])
    except Exception:
        return None
```

- [ ] **Step 4: Run and commit**

Run: `.venv/bin/python -m pytest tests/test_oauth_link.py -q` — Expected: PASS.

```bash
git add gaia/core/oauth_link.py tests/test_oauth_link.py
git commit -m "feat: signed one-time link binding a browser consent to a wa_id"
```

---

### Task 6: `/oauth/start` and `/oauth/callback`

**Files:**
- Modify: `gaia/main.py`, `gaia/core/config.py`
- Test: `tests/test_oauth_routes.py`

**Interfaces:**
- Consumes: `oauth_link.mint/verify` (5), `google_accounts.upsert` (3), `users_db.get_email` (4).
- Produces: `GET /oauth/start?t=<token>` → 307 to Google; `GET /oauth/callback?code=&state=` → text/plain result.

- [ ] **Step 1: Add the settings**

In `gaia/core/config.py`, inside `Settings`:

```python
    # The OAuth client. Empty by default so a checkout without the Workspace
    # integration still boots; the routes refuse rather than half-work.
    google_client_id: str = ""
    google_client_secret: str = ""
    # Every consenting account must be on this domain. It is also exactly what
    # keeps the app's Internal configuration -- and with it the exemption from
    # verification and the CASA assessment -- true. See research doc section 2.
    google_domain: str = "gaiagroupdevelopment.com"
    # Public origin, for building the OAuth redirect. Already in .env as DOMAIN.
    domain: str = ""
```

- [ ] **Step 2: Write the failing test**

```python
# tests/test_oauth_routes.py
from cryptography.fernet import Fernet
import pytest
from fastapi.testclient import TestClient

from gaia import main
from gaia.core import crypto, oauth_link
from gaia.core.db import google_accounts as ga
from gaia.core.db import users as users_db


@pytest.fixture(autouse=True)
def configured(monkeypatch):
    key = Fernet.generate_key().decode()
    for mod in (crypto, oauth_link, main):
        monkeypatch.setattr(mod.settings, "google_token_key", key, raising=False)
    monkeypatch.setattr(main.settings, "google_client_id", "cid")
    monkeypatch.setattr(main.settings, "google_client_secret", "csec")
    monkeypatch.setattr(main.settings, "domain", "gaia.example.com")


@pytest.fixture
def client(monkeypatch, migrated):
    monkeypatch.setattr(main, "get_pool", lambda: migrated)
    with TestClient(main.app) as c:
        yield c


def test_start_redirects_to_google(client, ana):
    r = client.get(f"/oauth/start?t={oauth_link.mint(ana.id)}", follow_redirects=False)
    assert r.status_code == 307
    assert "accounts.google.com" in r.headers["location"]
    assert "calendar.events.owned" in r.headers["location"]


def test_start_consumes_nothing(client, ana):
    """WhatsApp fetches URLs to build link previews. If /oauth/start consumed
    the one-time token, Meta's fetcher would burn it before the developer ever
    tapped the link, and every connect would fail with nothing in the logs."""
    token = oauth_link.mint(ana.id)
    assert client.get(f"/oauth/start?t={token}", follow_redirects=False).status_code == 307
    assert client.get(f"/oauth/start?t={token}", follow_redirects=False).status_code == 307


def test_start_refuses_a_bad_token(client):
    assert client.get("/oauth/start?t=rubbish", follow_redirects=False).status_code == 403


def test_callback_refuses_an_unknown_state(client):
    assert client.get("/oauth/callback?code=x&state=nonsense").status_code == 403


async def test_callback_stores_the_grant(client, migrated, monkeypatch):
    async with migrated.connection() as c:
        from psycopg.rows import dict_row
        c.row_factory = dict_row
        user = await users_db.create_user(
            c, name="Ana", wa_id="13055558801", email="ana@gaiagroupdevelopment.com")
        await c.commit()

    monkeypatch.setattr(main, "_exchange_code", _fake_exchange("ana@gaiagroupdevelopment.com"))
    state = client.get(f"/oauth/start?t={oauth_link.mint(user.id)}",
                       follow_redirects=False).headers["location"]
    state = state.split("state=")[1].split("&")[0]

    assert client.get(f"/oauth/callback?code=x&state={state}").status_code == 200
    async with migrated.connection() as c:
        from psycopg.rows import dict_row
        c.row_factory = dict_row
        assert (await ga.get(c, user))["refresh_token"] == "1//refresh"


async def test_callback_refuses_a_different_address(client, migrated, monkeypatch):
    """The link is a bearer credential for ten minutes. A colleague who gets it
    forwarded passes the domain check -- and if this equality test were absent,
    Gaia would then create this developer's events, client names and all, on
    that colleague's calendar."""
    async with migrated.connection() as c:
        from psycopg.rows import dict_row
        c.row_factory = dict_row
        user = await users_db.create_user(
            c, name="Ana", wa_id="13055558802", email="ana@gaiagroupdevelopment.com")
        await c.commit()

    monkeypatch.setattr(main, "_exchange_code", _fake_exchange("someone@gaiagroupdevelopment.com"))
    loc = client.get(f"/oauth/start?t={oauth_link.mint(user.id)}",
                     follow_redirects=False).headers["location"]
    state = loc.split("state=")[1].split("&")[0]
    assert client.get(f"/oauth/callback?code=x&state={state}").status_code == 403


async def test_callback_refuses_a_user_with_no_address(client, migrated, monkeypatch):
    async with migrated.connection() as c:
        from psycopg.rows import dict_row
        c.row_factory = dict_row
        user = await users_db.create_user(c, name="NoMail", wa_id="13055558803")
        await c.commit()

    monkeypatch.setattr(main, "_exchange_code", _fake_exchange("nomail@gaiagroupdevelopment.com"))
    loc = client.get(f"/oauth/start?t={oauth_link.mint(user.id)}",
                     follow_redirects=False).headers["location"]
    state = loc.split("state=")[1].split("&")[0]
    assert client.get(f"/oauth/callback?code=x&state={state}").status_code == 403


def _fake_exchange(email: str):
    async def _exchange(code: str):
        return {"refresh_token": "1//refresh", "email": email,
                "scopes": "https://www.googleapis.com/auth/calendar.events.owned"}
    return _exchange
```

- [ ] **Step 3: Run it and watch it fail**

Run: `.venv/bin/python -m pytest tests/test_oauth_routes.py -q`
Expected: FAIL — 404 on `/oauth/start`, since the route does not exist.

- [ ] **Step 4: Add the routes**

In `gaia/main.py`, add the imports and the two routes:

```python
import urllib.parse

from gaia.core import oauth_link
from gaia.core.db import google_accounts as ga_db

CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar.events.owned"

# state -> user_id, for the few seconds a consent takes. In-process because a
# restart mid-consent is a retry, not a data-loss event: the developer taps the
# link again. A table would outlive the thing it describes.
_PENDING_STATES: dict[str, str] = {}


async def _exchange_code(code: str) -> dict:
    """Swap an authorization code for a refresh token and the account's address.

    Separated so tests can replace it: the alternative is an env var only tests
    set, which this codebase does not do.
    """
    import httpx

    redirect = f"https://{settings.domain}/oauth/callback"
    async with httpx.AsyncClient(timeout=15) as http:
        tok = (await http.post("https://oauth2.googleapis.com/token", data={
            "code": code,
            "client_id": settings.google_client_id,
            "client_secret": settings.google_client_secret,
            "redirect_uri": redirect,
            "grant_type": "authorization_code",
        })).json()
        who = (await http.get(
            "https://www.googleapis.com/oauth2/v2/userinfo",
            headers={"Authorization": f"Bearer {tok['access_token']}"},
        )).json()
    return {"refresh_token": tok.get("refresh_token", ""),
            "email": who.get("email", ""),
            "scopes": tok.get("scope", "")}


@app.get("/oauth/start")
async def oauth_start(t: str = "") -> Response:
    """Validates and redirects. Consumes NOTHING.

    WhatsApp builds link previews by fetching URLs. If this consumed the
    one-time token, Meta's fetcher would burn it before the developer ever
    tapped the link and every connect attempt would fail, with nothing in the
    logs to explain it. A preview fetcher gets a 307 to Google and achieves
    nothing, because consent needs a human.
    """
    user_id = oauth_link.verify(t)
    if user_id is None:
        raise HTTPException(status_code=403, detail="This link has expired. Ask Gaia for a new one.")

    import secrets
    state = secrets.token_urlsafe(24)
    _PENDING_STATES[state] = str(user_id)
    query = urllib.parse.urlencode({
        "client_id": settings.google_client_id,
        "redirect_uri": f"https://{settings.domain}/oauth/callback",
        "response_type": "code",
        "scope": CALENDAR_SCOPE,
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
        # Additive, so the email increment's scopes join this grant rather than
        # replacing it and silently dropping calendar access.
        "include_granted_scopes": "true",
    })
    return Response(status_code=307,
                    headers={"location": f"https://accounts.google.com/o/oauth2/v2/auth?{query}"})


@app.get("/oauth/callback")
async def oauth_callback(code: str = "", state: str = "") -> Response:
    user_id = _PENDING_STATES.pop(state, None)
    if user_id is None:
        raise HTTPException(status_code=403, detail="That consent did not come from this server.")

    result = await _exchange_code(code)
    async with tx() as conn:
        user = await users_db.get_by_id(conn, user_id)
        if user is None:
            raise HTTPException(status_code=403)
        expected = await users_db.get_email(conn, user)
        # Both checks, in this order. The domain is what keeps the app's
        # Internal configuration true; the equality is what stops a forwarded
        # link binding a colleague's account to this developer's identity.
        if not expected or not result["email"].endswith("@" + settings.google_domain):
            raise HTTPException(status_code=403, detail="That account cannot be connected.")
        if result["email"].lower() != expected.lower():
            raise HTTPException(status_code=403, detail="That is not the account we expected.")
        await ga_db.upsert(conn, user, google_email=result["email"],
                           refresh_token=result["refresh_token"], scopes=result["scopes"])
    return Response(content="Calendar connected. You can close this tab.", media_type="text/plain")
```

- [ ] **Step 5: Add the missing lookup**

`users_db` has `get_by_wa_id` but nothing by id. In `gaia/core/db/users.py`:

```python
async def get_by_id(conn, user_id) -> User | None:
    cur = await conn.execute(
        f"SELECT {_COLUMNS} FROM users WHERE id = %s AND active", (user_id,)
    )
    row = await cur.fetchone()
    return _row_to_user(row) if row else None
```

Add it to `OWNERSHIP_SCOPED` in `tests/test_scope.py`:

```python
    "gaia.core.db.users.get_by_id":
        "resolving the OAuth callback's state to the user who started the "
        "flow. users carries no visibility column at all -- it is the table "
        "visible() is defined in terms of.",
    "gaia.core.db.users.get_email":
        "one person's own Workspace address, compared against the account that "
        "just consented. Ownership by definition.",
```

- [ ] **Step 6: Run and commit**

Run: `.venv/bin/python -m pytest -q` — Expected: PASS.

```bash
git add gaia/main.py gaia/core/config.py gaia/core/db/users.py tests/test_oauth_routes.py tests/test_scope.py
git commit -m "feat: consent surface binding a browser OAuth flow to a wa_id

/oauth/start consumes nothing, because WhatsApp fetches URLs to build link
previews and would otherwise burn the one-time token before anyone tapped it."
```

---

### Task 7: Talking to Google

**Files:**
- Create: `gaia/core/google.py`
- Test: `tests/test_google_client.py`

**Interfaces:**
- Consumes: `google_accounts.get/revoke` (3).
- Produces: `class RevokedGrant(Exception)`; `async def access_token(conn, user, *, http) -> str`; `async def request(conn, user, method: str, url: str, *, http, json=None) -> dict`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_google_client.py
from cryptography.fernet import Fernet
import httpx
import pytest

from gaia.core import crypto, google
from gaia.core.db import google_accounts as ga


@pytest.fixture(autouse=True)
def key(monkeypatch):
    monkeypatch.setattr(crypto.settings, "google_token_key", Fernet.generate_key().decode())


def _http(handler):
    """An httpx client wired to a handler instead of the network. The client is
    a required parameter throughout -- a seam, not a switch."""
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_refreshes_and_returns_an_access_token(conn, ana):
    await ga.upsert(conn, ana, google_email="a@x.com", refresh_token="1//r", scopes="s")

    def handler(request):
        assert "oauth2.googleapis.com" in str(request.url)
        return httpx.Response(200, json={"access_token": "ya29.live"})

    async with _http(handler) as http:
        assert await google.access_token(conn, ana, http=http) == "ya29.live"


async def test_invalid_grant_revokes_and_raises(conn, ana):
    """A refresh that comes back invalid_grant means the developer revoked us
    in their Google account. Retrying forever is how an integration becomes
    invisible noise in the logs."""
    await ga.upsert(conn, ana, google_email="a@x.com", refresh_token="1//r", scopes="s")

    def handler(request):
        return httpx.Response(400, json={"error": "invalid_grant"})

    async with _http(handler) as http:
        with pytest.raises(google.RevokedGrant):
            await google.access_token(conn, ana, http=http)
    assert (await ga.get(conn, ana))["revoked_at"] is not None


async def test_no_account_raises_revoked(conn, ana):
    async with _http(lambda r: httpx.Response(200, json={})) as http:
        with pytest.raises(google.RevokedGrant):
            await google.access_token(conn, ana, http=http)


async def test_request_attaches_the_bearer_token(conn, ana):
    await ga.upsert(conn, ana, google_email="a@x.com", refresh_token="1//r", scopes="s")

    def handler(request):
        if "oauth2" in str(request.url):
            return httpx.Response(200, json={"access_token": "ya29.live"})
        assert request.headers["authorization"] == "Bearer ya29.live"
        return httpx.Response(200, json={"ok": True})

    async with _http(handler) as http:
        assert await google.request(
            conn, ana, "GET", "https://www.googleapis.com/calendar/v3/x", http=http
        ) == {"ok": True}
```

- [ ] **Step 2: Run it and watch it fail**

Run: `.venv/bin/python -m pytest tests/test_google_client.py -q`
Expected: FAIL — `No module named 'gaia.core.google'`.

- [ ] **Step 3: Write the module**

```python
# gaia/core/google.py
"""Authenticated calls to Google, on behalf of one developer.

The httpx client is a required parameter, not something this module creates.
That is the seam tests use -- the alternative is an env var only tests set,
which this codebase does not do (see usage.record, which takes `pool` for
exactly this reason).
"""

import logging

from gaia.core.config import settings
from gaia.core.db import google_accounts as ga_db
from gaia.core.models import User

log = logging.getLogger("gaia.google")


class RevokedGrant(Exception):
    """No usable grant: never connected, or revoked in the developer's Google
    account. Callers turn this into an offer of a fresh link, never a retry."""


async def access_token(conn, user: User, *, http) -> str:
    account = await ga_db.get(conn, user)
    if account is None or account["revoked_at"] is not None:
        raise RevokedGrant(f"no live Google grant for {user.id}")

    resp = await http.post("https://oauth2.googleapis.com/token", data={
        "client_id": settings.google_client_id,
        "client_secret": settings.google_client_secret,
        "refresh_token": account["refresh_token"],
        "grant_type": "refresh_token",
    })
    body = resp.json()
    if resp.status_code != 200 or "access_token" not in body:
        # invalid_grant is the documented signal for a revoked or expired
        # refresh token. Marking it here means the digest can say so once,
        # instead of failing quietly every morning.
        if body.get("error") == "invalid_grant":
            await ga_db.revoke(conn, user)
            raise RevokedGrant(f"grant revoked for {user.id}")
        raise RuntimeError(f"token refresh failed: {resp.status_code} {body}")
    return body["access_token"]


async def request(conn, user: User, method: str, url: str, *, http, json=None) -> dict:
    token = await access_token(conn, user, http=http)
    resp = await http.request(
        method, url, headers={"Authorization": f"Bearer {token}"}, json=json
    )
    if resp.status_code >= 400:
        raise RuntimeError(f"google {method} {url} -> {resp.status_code} {resp.text[:300]}")
    return resp.json() if resp.content else {}
```

- [ ] **Step 4: Run and commit**

Run: `.venv/bin/python -m pytest tests/test_google_client.py -q` — Expected: PASS.

```bash
git add gaia/core/google.py tests/test_google_client.py
git commit -m "feat: authenticated Google calls, with revocation as a first-class outcome"
```

---

### Task 8: Calendar calls, and the tripwire

**Files:**
- Create: `gaia/capabilities/calendar/client.py`, `gaia/capabilities/calendar/__init__.py`
- Test: `tests/test_calendar_client.py`

**Interfaces:**
- Consumes: `google.request` (7).
- Produces:
  - `async def create_event(conn, user, *, summary, start, end, with_meet, http, extended=None) -> dict`
  - `async def list_events(conn, user, *, time_min, time_max, http) -> list[dict]`
  - `async def add_attendees(conn, user, *, event_id, emails, http) -> dict`
  - `async def delete_event(conn, user, *, event_id, http) -> None`
  - `def busy_intervals(events) -> list[tuple[datetime, datetime]]`

- [ ] **Step 1: Write the tripwire first — it is the finding that cost a probe to learn**

```python
# tests/test_calendar_client.py
from datetime import datetime
from zoneinfo import ZoneInfo

from cryptography.fernet import Fernet
import httpx
import pytest

from gaia.capabilities.calendar import client as cal
from gaia.core import crypto
from gaia.core.db import google_accounts as ga

TZ = ZoneInfo("America/New_York")


@pytest.fixture(autouse=True)
def key(monkeypatch):
    monkeypatch.setattr(crypto.settings, "google_token_key", Fernet.generate_key().decode())


@pytest.fixture
async def connected(conn, ana):
    await ga.upsert(conn, ana, google_email="a@x.com", refresh_token="1//r", scopes="s")
    return ana


def _capture(sent, response=None):
    def handler(request):
        if "oauth2" in str(request.url):
            return httpx.Response(200, json={"access_token": "ya29"})
        sent.append({"url": str(request.url), "method": request.method,
                     "body": request.content.decode() or "{}"})
        return httpx.Response(200, json=response or {"id": "evt-1"})
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_create_event_never_sends_attendees(conn, connected):
    """TRIPWIRE. An event created with an attendee and sendUpdates omitted was
    measured landing on an external attendee's calendar, Meet link and all,
    before any email existed -- sendUpdates suppresses the notification, not
    the intrusion. So attendees may only ever appear via add_attendees, which
    runs downstream of a human approving the list.

    EXEMPT: add_attendees. That call is the one place attendees are supposed
    to appear, and generalising this test to 'no Calendar call may send
    attendees' would block the feature it exists to protect.
    """
    import json as _json
    sent = []
    async with _capture(sent) as http:
        await cal.create_event(
            conn, connected, summary="Site walk",
            start=datetime(2026, 9, 16, 10, tzinfo=TZ),
            end=datetime(2026, 9, 16, 11, tzinfo=TZ),
            with_meet=True, http=http,
        )
    assert "attendees" not in _json.loads(sent[0]["body"])


async def test_create_event_asks_for_a_meet_link(conn, connected):
    import json as _json
    sent = []
    async with _capture(sent) as http:
        await cal.create_event(
            conn, connected, summary="Site walk",
            start=datetime(2026, 9, 16, 10, tzinfo=TZ),
            end=datetime(2026, 9, 16, 11, tzinfo=TZ),
            with_meet=True, http=http,
        )
    body = _json.loads(sent[0]["body"])
    assert body["conferenceData"]["createRequest"]["conferenceSolutionKey"]["type"] == "hangoutsMeet"
    # Without conferenceDataVersion=1 the whole block is ignored -- silently,
    # with no error and no Meet link.
    assert "conferenceDataVersion=1" in sent[0]["url"]


async def test_add_attendees_announces_them(conn, connected):
    import json as _json
    sent = []
    async with _capture(sent) as http:
        await cal.add_attendees(conn, connected, event_id="evt-1",
                                emails=["x@y.com"], http=http)
    assert sent[0]["method"] == "PATCH"
    assert "sendUpdates=all" in sent[0]["url"]
    assert _json.loads(sent[0]["body"])["attendees"] == [{"email": "x@y.com"}]


async def test_list_events_expands_recurrences(conn, connected):
    """Without singleEvents=true a weekly standup is one row that answers
    nothing about whether Tuesday is free."""
    sent = []
    async with _capture(sent, response={"items": []}) as http:
        await cal.list_events(conn, connected,
                              time_min=datetime(2026, 9, 16, tzinfo=TZ),
                              time_max=datetime(2026, 9, 17, tzinfo=TZ), http=http)
    assert "singleEvents=true" in sent[0]["url"]


def test_busy_intervals_drops_everything_but_times():
    """check_availability answers 'is 10am free'. Meeting titles are not part
    of that answer and there is no reason to spend the model's context on them."""
    events = [{"summary": "Seller call re: Aurea",
               "attendees": [{"email": "client@example.com"}],
               "start": {"dateTime": "2026-09-16T10:00:00-04:00"},
               "end": {"dateTime": "2026-09-16T11:00:00-04:00"}}]
    out = cal.busy_intervals(events)
    assert len(out) == 1
    assert "Aurea" not in repr(out)


def test_busy_intervals_skips_all_day_events():
    """An all-day event carries `date`, not `dateTime`, and blocking the whole
    day on someone's birthday would make every day look full."""
    assert cal.busy_intervals([{"start": {"date": "2026-09-16"},
                                "end": {"date": "2026-09-17"}}]) == []
```

- [ ] **Step 2: Run it and watch it fail**

Run: `.venv/bin/python -m pytest tests/test_calendar_client.py -q`
Expected: FAIL — `No module named 'gaia.capabilities.calendar'`.

- [ ] **Step 3: Write the client**

```python
# gaia/capabilities/calendar/client.py
"""Calendar REST calls. The only place an event payload is built.

Kept separate from tools.py so the tripwire test has one surface to assert on:
if every outbound create goes through create_event(), then proving create_event
never sends attendees proves it for the whole feature.
"""

import urllib.parse
from datetime import datetime

from gaia.core import google
from gaia.core.models import User

BASE = "https://www.googleapis.com/calendar/v3/calendars/primary/events"


async def create_event(conn, user: User, *, summary: str, start: datetime,
                       end: datetime, with_meet: bool, http, extended: dict | None = None) -> dict:
    """Creates the event BARE -- no attendees, ever.

    An event created with an attendee and sendUpdates omitted was measured
    already sitting on that attendee's calendar, Meet link and all, before any
    email was sent. sendUpdates suppresses the notification, not the intrusion.
    So a freshly created event is purely this developer's own calendar entry,
    invisible to anyone else, and attendees arrive only via add_attendees().
    """
    body: dict = {
        "summary": summary,
        "start": {"dateTime": start.isoformat()},
        "end": {"dateTime": end.isoformat()},
    }
    if extended:
        body["extendedProperties"] = {"private": extended}
    params = {}
    if with_meet:
        body["conferenceData"] = {"createRequest": {
            # Derived from the slot, so a retry after a timeout does not mint a
            # second conference for the same meeting.
            "requestId": f"gaia-{user.id}-{int(start.timestamp())}",
            "conferenceSolutionKey": {"type": "hangoutsMeet"},
        }}
        # Without this the conferenceData block is ignored silently.
        params["conferenceDataVersion"] = "1"
    url = f"{BASE}?{urllib.parse.urlencode(params)}" if params else BASE
    return await google.request(conn, user, "POST", url, http=http, json=body)


async def add_attendees(conn, user: User, *, event_id: str, emails: list[str], http) -> dict:
    """The one call in this feature that reaches a third party. It runs only
    from confirm_invite, downstream of a human who saw the address list."""
    params = urllib.parse.urlencode({"conferenceDataVersion": "1", "sendUpdates": "all"})
    return await google.request(
        conn, user, "PATCH", f"{BASE}/{event_id}?{params}",
        http=http, json={"attendees": [{"email": e} for e in emails]},
    )


async def list_events(conn, user: User, *, time_min: datetime, time_max: datetime, http) -> list[dict]:
    params = urllib.parse.urlencode({
        "timeMin": time_min.isoformat(),
        "timeMax": time_max.isoformat(),
        # A weekly standup is otherwise one row that says nothing about Tuesday.
        "singleEvents": "true",
        "orderBy": "startTime",
    })
    return (await google.request(conn, user, "GET", f"{BASE}?{params}", http=http)).get("items", [])


async def delete_event(conn, user: User, *, event_id: str, http) -> None:
    await google.request(
        conn, user, "DELETE", f"{BASE}/{event_id}?sendUpdates=all", http=http
    )


def busy_intervals(events: list[dict]) -> list[tuple[datetime, datetime]]:
    """Times only. Summaries and attendees are dropped here rather than at the
    tool boundary, so nothing downstream has to remember to do it."""
    out = []
    for ev in events:
        start, end = ev.get("start", {}), ev.get("end", {})
        # All-day events carry `date`, not `dateTime`. Treating one as a busy
        # block would make every birthday look like a full day.
        if "dateTime" not in start or "dateTime" not in end:
            continue
        if ev.get("transparency") == "transparent":
            continue  # marked "free" by the developer; not a conflict
        out.append((datetime.fromisoformat(start["dateTime"]),
                    datetime.fromisoformat(end["dateTime"])))
    return sorted(out)
```

Create an empty `gaia/capabilities/calendar/__init__.py` for now; Task 10 fills it in.

- [ ] **Step 4: Run and commit**

Run: `.venv/bin/python -m pytest tests/test_calendar_client.py -q` — Expected: PASS.

```bash
git add gaia/capabilities/calendar/ tests/test_calendar_client.py
git commit -m "feat: calendar calls, with a tripwire keeping attendees off create

An event created with an attendee and sendUpdates omitted was measured landing
on that attendee's calendar before any email existed. Attendees may only ever
arrive by patch, downstream of a human approving the list."
```

---

### Task 9: The approval record

**Files:**
- Create: `gaia/core/db/pending_invites.py`
- Modify: `tests/test_scope.py`
- Test: `tests/test_pending_invites.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `async def create(conn, user, *, event_id, emails, ttl_minutes=60) -> UUID`; `async def claim(conn, user, pending_id) -> dict | None` — returns `{"event_id", "emails"}` and marks confirmed, or `None`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_pending_invites.py
from uuid import uuid4

from gaia.core.db import pending_invites as pi


async def test_claim_returns_what_was_stored(conn, ana):
    """The whole design. confirm_invite takes only a pending_id, so the model
    cannot confirm a different list than the one the human was read back."""
    pid = await pi.create(conn, ana, event_id="evt-1", emails=["a@x.com", "b@x.com"])
    assert (await pi.claim(conn, ana, pid))["emails"] == ["a@x.com", "b@x.com"]


async def test_claim_is_single_use(conn, ana):
    pid = await pi.create(conn, ana, event_id="evt-1", emails=["a@x.com"])
    assert await pi.claim(conn, ana, pid) is not None
    assert await pi.claim(conn, ana, pid) is None


async def test_expired_invite_cannot_be_claimed(conn, ana):
    """An approval from Tuesday must not fire on Friday."""
    pid = await pi.create(conn, ana, event_id="evt-1", emails=["a@x.com"], ttl_minutes=-1)
    assert await pi.claim(conn, ana, pid) is None


async def test_one_user_cannot_claim_anothers(conn, ana, sofia):
    pid = await pi.create(conn, ana, event_id="evt-1", emails=["a@x.com"])
    assert await pi.claim(conn, sofia, pid) is None


async def test_unknown_id_is_none(conn, ana):
    assert await pi.claim(conn, ana, uuid4()) is None
```

- [ ] **Step 2: Run it and watch it fail**

Run: `.venv/bin/python -m pytest tests/test_pending_invites.py -q`
Expected: FAIL — `No module named 'gaia.core.db.pending_invites'`.

- [ ] **Step 3: Write the module**

```python
# gaia/core/db/pending_invites.py
"""The durable half of the approval gate.

The weak version of this gate is a prompt rule: the model reads the addresses
back, the human agrees, the model calls add_attendees(event_id, emails). That
trusts the model to pass the same list it read back.

Here, propose_invite stores the exact list and confirm_invite takes only an id.
The model cannot smuggle a different recipient into the confirmation because
confirmation accepts no recipients.
"""

from uuid import UUID

from gaia.core.models import User


async def create(conn, user: User, *, event_id: str, emails: list[str],
                 ttl_minutes: int = 60) -> UUID:
    cur = await conn.execute(
        """INSERT INTO pending_invites (user_id, event_id, emails, expires_at)
           VALUES (%s, %s, %s, now() + make_interval(mins => %s)) RETURNING id""",
        (user.id, event_id, emails, ttl_minutes),
    )
    return (await cur.fetchone())["id"]


async def claim(conn, user: User, pending_id: UUID) -> dict | None:
    """Ownership-scoped, single-use and expiring, in one statement.

    Claiming in the UPDATE rather than reading then writing means two
    confirmations racing cannot both succeed -- one updates the row, the other
    matches nothing and gets None.
    """
    cur = await conn.execute(
        """UPDATE pending_invites SET confirmed_at = now()
           WHERE id = %s AND user_id = %s
             AND confirmed_at IS NULL AND expires_at > now()
           RETURNING event_id, emails""",
        (pending_id, user.id),
    )
    row = await cur.fetchone()
    return {"event_id": row["event_id"], "emails": row["emails"]} if row else None
```

- [ ] **Step 4: Declare the scoping**

In `tests/test_scope.py`, add to `OWNERSHIP_SCOPED`:

```python
    "gaia.core.db.pending_invites.claim":
        "one person's own pending approval. Ownership, not visibility: a "
        "colleague may not confirm an invitation on someone else's behalf at "
        "any visibility setting, which is why the table has no visibility "
        "column.",
```

- [ ] **Step 5: Run and commit**

Run: `.venv/bin/python -m pytest -q` — Expected: PASS.

```bash
git add gaia/core/db/pending_invites.py tests/test_pending_invites.py tests/test_scope.py
git commit -m "feat: durable approval records, so confirmation takes no addresses"
```

---

### Task 10: The calendar capability

**Files:**
- Create: `gaia/capabilities/calendar/tools.py`
- Modify: `gaia/capabilities/calendar/__init__.py`, `gaia/capabilities/__init__.py`
- Test: `tests/test_calendar_tools.py`

**Interfaces:**
- Consumes: everything from Tasks 3, 7, 8, 9.
- Produces: tools `check_availability`, `create_event`, `propose_invite`, `confirm_invite`, `cancel_event`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_calendar_tools.py
"""NOTE ON FIXTURES: capability tools get their own transaction via
registry.dispatch, so a user created on this connection and not committed is
invisible to them and the insert dies on a foreign key -- the same trap
documented for usage.record. These tests commit the user first.

tx() also sets row_factory on the pooled connection and that rides back into
the pool, so the connection here sets it explicitly.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

from cryptography.fernet import Fernet
import httpx
import pytest
from psycopg.rows import dict_row

from gaia.capabilities.calendar import tools
from gaia.core import crypto
from gaia.core.db import google_accounts as ga
from gaia.core.db import pending_invites as pi
from gaia.core.db import users as users_db


@pytest.fixture(autouse=True)
def key(monkeypatch):
    monkeypatch.setattr(crypto.settings, "google_token_key", Fernet.generate_key().decode())


@pytest.fixture
async def committed(migrated):
    async with migrated.connection() as c:
        c.row_factory = dict_row
        user = await users_db.create_user(c, name="Ana", wa_id="13055557001",
                                          email="ana@gaiagroupdevelopment.com")
        await ga.upsert(c, user, google_email="ana@gaiagroupdevelopment.com",
                        refresh_token="1//r", scopes="s")
        await c.commit()
    async with migrated.connection() as c:
        c.row_factory = dict_row
        yield c, user


def _http(sent, response):
    def handler(request):
        if "oauth2" in str(request.url):
            return httpx.Response(200, json={"access_token": "ya29"})
        sent.append({"url": str(request.url), "method": request.method,
                     "body": request.content.decode() or "{}"})
        return httpx.Response(200, json=response)
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_create_event_uses_the_users_timezone(committed):
    """The model supplies wall-clock time and nothing else. today_line() exists
    because the model called one date Friday in one turn and Thu in the next;
    it must not be trusted to attach a UTC offset across a DST boundary."""
    import json as _json
    conn, user = committed
    sent = []
    async with _http(sent, {"id": "evt-1", "hangoutLink": "https://meet.google.com/abc"}) as http:
        out = await tools.create_event(conn, user, {
            "summary": "Site walk", "date": "2026-09-16",
            "start_time": "10:00", "duration_minutes": 60, "with_meet": True,
        }, http=http)
    assert out["meet_link"] == "https://meet.google.com/abc"
    body = _json.loads(sent[0]["body"])
    # America/New_York on that date is UTC-4.
    assert body["start"]["dateTime"].endswith("-04:00")


async def test_check_availability_returns_times_only(committed):
    conn, user = committed
    events = {"items": [{"summary": "Seller call re: Aurea",
                         "start": {"dateTime": "2026-09-16T10:00:00-04:00"},
                         "end": {"dateTime": "2026-09-16T11:00:00-04:00"}}]}
    async with _http([], events) as http:
        out = await tools.check_availability(conn, user, {"date": "2026-09-16"}, http=http)
    assert "Aurea" not in str(out)
    assert out["busy"] == [{"from": "10:00", "to": "11:00"}]


async def test_propose_invite_reaches_nobody(committed):
    conn, user = committed
    sent = []
    async with _http(sent, {}) as http:
        out = await tools.propose_invite(
            conn, user, {"event_id": "evt-1", "emails": ["a@x.com"]}, http=http)
    assert sent == []          # no Google call at all
    assert out["pending_id"]
    assert out["emails"] == ["a@x.com"]


async def test_confirm_invite_sends_what_was_stored(committed):
    import json as _json
    conn, user = committed
    pid = await pi.create(conn, user, event_id="evt-1", emails=["stored@x.com"])
    sent = []
    async with _http(sent, {"id": "evt-1"}) as http:
        await tools.confirm_invite(conn, user, {"pending_id": str(pid)}, http=http)
    assert _json.loads(sent[0]["body"])["attendees"] == [{"email": "stored@x.com"}]


async def test_confirm_invite_schema_accepts_no_addresses():
    """The gate, as a schema assertion. If an emails field ever appears here,
    the model can confirm a list the human never saw."""
    from gaia.capabilities.calendar import CAPABILITY
    tool = next(t for t in CAPABILITY.tools if t.name == "confirm_invite")
    assert set(tool.input_schema["properties"]) == {"pending_id"}


async def test_tools_offer_a_link_when_not_connected(migrated):
    conn_user = None
    async with migrated.connection() as c:
        c.row_factory = dict_row
        conn_user = await users_db.create_user(c, name="New", wa_id="13055557002")
        await c.commit()
    async with migrated.connection() as c:
        c.row_factory = dict_row
        async with _http([], {}) as http:
            out = await tools.check_availability(c, conn_user, {"date": "2026-09-16"}, http=http)
    assert out["needs_connection"] is True
```

- [ ] **Step 2: Run it and watch it fail**

Run: `.venv/bin/python -m pytest tests/test_calendar_tools.py -q`
Expected: FAIL — `cannot import name 'tools' from 'gaia.capabilities.calendar'`.

- [ ] **Step 3: Write the handlers**

```python
# gaia/capabilities/calendar/tools.py
"""Calendar tool handlers.

Every handler takes `http` as a keyword argument with a default, so
registry.dispatch can call it with (conn, user, args) while tests pass their
own transport. The default constructs a client; it is not a test switch.
"""

import logging
from datetime import date as date_cls
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import httpx

from gaia.capabilities.calendar import client as cal
from gaia.core import google, oauth_link
from gaia.core.config import settings
from gaia.core.db import pending_invites as pi_db
from gaia.core.models import User

log = logging.getLogger("gaia.calendar")

CONNECT_HINT = "Ask the user to connect their Google account, then send them this link."


def _client(http):
    return http if http is not None else httpx.AsyncClient(timeout=20)


def _needs_connection(conn, user: User) -> dict:
    """Not a hidden capability. Hiding the tools would make Gaia claim it
    cannot do calendars at all, which is false and unhelpful."""
    return {
        "needs_connection": True,
        "link": f"https://{settings.domain}/oauth/start?t={oauth_link.mint(user.id)}",
        "note": CONNECT_HINT,
    }


def _local(user: User, day: str, hhmm: str) -> datetime:
    """Wall clock plus the user's timezone, from the database.

    The model supplies '2026-09-16' and '10:00' and never an offset. A model
    that got Friday and Thursday confused inside one conversation must not be
    the thing deciding whether a date is inside daylight saving.
    """
    d = date_cls.fromisoformat(day)
    h, m = (int(x) for x in hhmm.split(":"))
    return datetime.combine(d, time(h, m), tzinfo=ZoneInfo(user.timezone))


async def check_availability(conn, user: User, args: dict, *, http=None) -> dict:
    day = args["date"]
    try:
        async with _client(http) as client:
            events = await cal.list_events(
                conn, user, http=client,
                time_min=_local(user, day, "00:00"),
                time_max=_local(user, day, "00:00") + timedelta(days=1),
            )
    except google.RevokedGrant:
        return _needs_connection(conn, user)
    return {"date": day, "busy": [
        {"from": s.strftime("%H:%M"), "to": e.strftime("%H:%M")}
        for s, e in cal.busy_intervals(events)
    ]}


async def create_event(conn, user: User, args: dict, *, http=None) -> dict:
    start = _local(user, args["date"], args["start_time"])
    end = start + timedelta(minutes=int(args.get("duration_minutes", 60)))
    extended = {}
    if args.get("lead_id"):
        extended["gaia_lead_id"] = args["lead_id"]
    if args.get("commitment_id"):
        extended["gaia_commitment_id"] = args["commitment_id"]
    try:
        async with _client(http) as client:
            ev = await cal.create_event(
                conn, user, summary=args["summary"], start=start, end=end,
                with_meet=bool(args.get("with_meet")), http=client,
                extended=extended or None,
            )
    except google.RevokedGrant:
        return _needs_connection(conn, user)
    return {"event_id": ev.get("id"),
            "meet_link": ev.get("hangoutLink"),
            "starts": start.strftime("%A %Y-%m-%d %H:%M"),
            "note": "Nobody has been invited. Use propose_invite to ask first."}


async def propose_invite(conn, user: User, args: dict, *, http=None) -> dict:
    """Reaches nobody. Records the exact list so confirm_invite can send it."""
    emails = list(args["emails"])
    pending_id = await pi_db.create(conn, user, event_id=args["event_id"], emails=emails)
    return {"pending_id": str(pending_id), "emails": emails,
            "note": "Read these addresses back and wait for a yes before confirm_invite."}


async def confirm_invite(conn, user: User, args: dict, *, http=None) -> dict:
    """Takes no addresses. That is the gate: the model cannot confirm a list
    the human was never shown, because it has nowhere to put one."""
    claimed = await pi_db.claim(conn, user, args["pending_id"])
    if claimed is None:
        return {"sent": False, "note": "That approval has expired or was already used."}
    try:
        async with _client(http) as client:
            await cal.add_attendees(conn, user, event_id=claimed["event_id"],
                                    emails=claimed["emails"], http=client)
    except google.RevokedGrant:
        return _needs_connection(conn, user)
    return {"sent": True, "invited": claimed["emails"]}


async def cancel_event(conn, user: User, args: dict, *, http=None) -> dict:
    try:
        async with _client(http) as client:
            await cal.delete_event(conn, user, event_id=args["event_id"], http=client)
    except google.RevokedGrant:
        return _needs_connection(conn, user)
    return {"cancelled": True}
```

- [ ] **Step 4: Register the capability**

```python
# gaia/capabilities/calendar/__init__.py
from gaia.capabilities.base import Capability, Tool
from gaia.capabilities.calendar.tools import (
    cancel_event,
    check_availability,
    confirm_invite,
    create_event,
    propose_invite,
)

CAPABILITY = Capability(
    name="calendar",
    prompt_fragment=(
        "\nYou can check the user's calendar and put things on it. Creating an event invites "
        "nobody — it lands only on their own calendar. To invite people you must call "
        "propose_invite, read the exact addresses back to the user, wait for them to agree, "
        "and only then call confirm_invite.\n"
        "\nNever call confirm_invite without having shown the addresses and heard a yes. An "
        "event you created can be deleted; an invitation that reached a client cannot be "
        "unsent. When you read addresses back, name the people — 'Dalila Serrao and two "
        "others at Arquitectonica' — because seven raw addresses are not checkable at a "
        "glance.\n"
        "\nGive dates as a date and a wall-clock time in the user's own day. Never compute a "
        "UTC offset yourself.\n"
        "\nIf a tool says needs_connection, send the user the link it gives you and explain "
        "that Gaia needs access to their Google Calendar once.\n"
    ),
    tools=(
        Tool("check_availability",
             "What the user is already booked for on a given day. Times only, no details.",
             {"type": "object",
              "properties": {"date": {"type": "string", "description": "YYYY-MM-DD"}},
              "required": ["date"]},
             check_availability),
        Tool("create_event",
             "Put an event on the user's own calendar, optionally with a Google Meet link. "
             "Invites nobody — use propose_invite afterwards to add people.",
             {"type": "object",
              "properties": {
                  "summary": {"type": "string"},
                  "date": {"type": "string", "description": "YYYY-MM-DD"},
                  "start_time": {"type": "string", "description": "HH:MM, the user's local time"},
                  "duration_minutes": {"type": "integer"},
                  "with_meet": {"type": "boolean"},
                  "lead_id": {"type": "string", "description": "Link this event to a lead."},
                  "commitment_id": {"type": "string"},
              },
              "required": ["summary", "date", "start_time"]},
             create_event),
        Tool("propose_invite",
             "Prepare an invitation for an existing event. Sends nothing. Returns the exact "
             "address list to read back to the user before confirming.",
             {"type": "object",
              "properties": {"event_id": {"type": "string"},
                             "emails": {"type": "array", "items": {"type": "string"}}},
              "required": ["event_id", "emails"]},
             propose_invite),
        Tool("confirm_invite",
             "Send the invitation the user just approved. Takes only the pending_id from "
             "propose_invite — the addresses are the ones already shown to the user.",
             {"type": "object",
              "properties": {"pending_id": {"type": "string"}},
              "required": ["pending_id"]},
             confirm_invite),
        Tool("cancel_event",
             "Delete an event from the user's calendar, notifying anyone invited.",
             {"type": "object",
              "properties": {"event_id": {"type": "string"}},
              "required": ["event_id"]},
             cancel_event),
    ),
)
```

Then register it. `gaia/capabilities/__init__.py` becomes:

```python
from gaia.capabilities.base import registry
from gaia.capabilities.calendar import CAPABILITY as CALENDAR
from gaia.capabilities.leads import CAPABILITY as LEADS
from gaia.capabilities.meetings import CAPABILITY as MEETINGS

registry.register(MEETINGS)
registry.register(LEADS)
registry.register(CALENDAR)
```

Registration order is prompt order — `prompt_fragments` joins them in the order registered — so calendar goes last, after the meeting and lead instructions it refers to.

- [ ] **Step 5: Run and commit**

Run: `.venv/bin/python -m pytest -q` — Expected: PASS.

```bash
git add gaia/capabilities/ tests/test_calendar_tools.py
git commit -m "feat: calendar tools, with approval as a schema property

confirm_invite accepts no addresses, so the model cannot confirm a list the
user was never shown."
```

---

### Task 11: Correlation both ways

**Files:**
- Modify: `gaia/core/db/leads.py`, `gaia/core/db/commitments.py`, `gaia/capabilities/calendar/tools.py`, `tests/test_scope.py`
- Test: `tests/test_calendar_correlation.py`

**Interfaces:**
- Consumes: `calendar_event_id` columns (1), `create_event` tool (10).
- Produces: `leads.set_calendar_event(conn, user, lead_id, event_id)`, `leads.by_event_ids(conn, user, event_ids)`, and the same pair on `commitments`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_calendar_correlation.py
from gaia.core.db import commitments as commitments_db
from gaia.core.db import leads as leads_db
from tests.factories import make_row


async def test_set_and_look_up_a_lead_by_event(conn, ana):
    lead_id = await make_row(conn, "leads", ana)
    assert await leads_db.set_calendar_event(conn, ana, lead_id, "evt-1")
    assert (await leads_db.by_event_ids(conn, ana, ["evt-1"]))["evt-1"]["lead_id"] == lead_id


async def test_lookup_is_ownership_scoped(conn, ana, sofia):
    """Sofia's digest must not describe Ana's event, even though an org-visible
    lead is readable. This answers 'what does this person's day hold'."""
    lead_id = await make_row(conn, "leads", ana, visibility="org")
    await leads_db.set_calendar_event(conn, ana, lead_id, "evt-1")
    assert await leads_db.by_event_ids(conn, sofia, ["evt-1"]) == {}


async def test_missing_event_is_not_an_error(conn, ana):
    """People delete events. A calendar_event_id pointing at nothing is normal."""
    assert await leads_db.by_event_ids(conn, ana, ["gone"]) == {}


async def test_commitments_correlate_too(conn, ana):
    cid = await make_row(conn, "commitments", ana)
    assert await commitments_db.set_calendar_event(conn, ana, cid, "evt-2")
    assert (await commitments_db.by_event_ids(conn, ana, ["evt-2"]))["evt-2"]["commitment_id"] == cid


async def test_setting_an_event_on_someone_elses_lead_fails(conn, ana, sofia):
    lead_id = await make_row(conn, "leads", ana)
    assert await leads_db.set_calendar_event(conn, sofia, lead_id, "evt-1") is False
```

- [ ] **Step 2: Run it and watch it fail**

Run: `.venv/bin/python -m pytest tests/test_calendar_correlation.py -q`
Expected: FAIL — `module 'gaia.core.db.leads' has no attribute 'set_calendar_event'`.

- [ ] **Step 3: Add the functions**

In `gaia/core/db/leads.py`:

```python
async def set_calendar_event(conn, user: User, lead_id, event_id: str) -> bool:
    """The event for this lead's current next action -- not a history. What
    already happened lives in meetings; a second home for the same truth is
    how the two drift apart."""
    cur = await conn.execute(
        """UPDATE leads SET calendar_event_id = %s, updated_at = now()
           WHERE id = %s AND user_id = %s RETURNING id""",
        (event_id, lead_id, user.id),
    )
    return await cur.fetchone() is not None


async def by_event_ids(conn, user: User, event_ids: list[str]) -> dict:
    """Which of today's events belong to which lead.

    Ownership-scoped, same reason as due_for: this answers what THIS person's
    day holds. A colleague's org-visible lead attached to their own event is
    readable but is not this person's day.

    An id that matches nothing is normal, not an error -- people delete events,
    and a calendar_event_id pointing at a 404 is the expected end state.
    """
    if not event_ids:
        return {}
    cur = await conn.execute(
        f"""{_SELECT}
            WHERE l.user_id = %(uid)s AND l.calendar_event_id = ANY(%(ids)s)""",
        {"uid": user.id, "ids": list(event_ids)},
    )
    return {r["calendar_event_id"]: {"lead_id": r["id"], "contact": r["name"],
                                     "description": r["description"]}
            for r in await cur.fetchall()}
```

`_SELECT` ends with `FROM leads l JOIN contacts ct ...`, so the column cannot be appended after it. Add it to the projection itself — this is the exact replacement:

```python
_SELECT = """SELECT l.id, ct.name, l.description, l.status, l.next_action_at,
                    l.next_action_note, l.nudge_count, l.calendar_event_id
             FROM leads l JOIN contacts ct ON ct.id = l.contact_id"""
```

Every existing caller selects by key, so the extra column is inert for them.

Now the mirror pair in `gaia/core/db/commitments.py`. Its `_SELECT` needs the same treatment:

```python
_SELECT = """SELECT c.id, c.description, c.due_at, c.nudge_count,
                    ct.name AS contact, c.calendar_event_id
             FROM commitments c
             LEFT JOIN contacts ct ON ct.id = c.contact_id"""


async def set_calendar_event(conn, user: User, commitment_id, event_id: str) -> bool:
    cur = await conn.execute(
        """UPDATE commitments SET calendar_event_id = %s
           WHERE id = %s AND user_id = %s RETURNING id""",
        (event_id, commitment_id, user.id),
    )
    return await cur.fetchone() is not None


async def by_event_ids(conn, user: User, event_ids: list[str]) -> dict:
    """The commitments half of 'what does this person's day hold'. Ownership
    for the same reason as open_for; an id matching nothing is normal, because
    people delete events."""
    if not event_ids:
        return {}
    cur = await conn.execute(
        f"""{_SELECT}
            WHERE c.user_id = %(uid)s AND c.calendar_event_id = ANY(%(ids)s)""",
        {"uid": user.id, "ids": list(event_ids)},
    )
    return {r["calendar_event_id"]: {"commitment_id": r["id"],
                                     "description": r["description"]}
            for r in await cur.fetchall()}
```

- [ ] **Step 4: Link on creation**

In `gaia/capabilities/calendar/tools.py`, at the end of `create_event`, after the Google call succeeds:

```python
    # Both directions, written together. extendedProperties (set in
    # client.create_event) lets events.list find Gaia's events server-side;
    # the column is the durable half, because the calendar is not a database
    # and people delete events.
    if args.get("lead_id"):
        await leads_db.set_calendar_event(conn, user, args["lead_id"], ev["id"])
    if args.get("commitment_id"):
        await commitments_db.set_calendar_event(conn, user, args["commitment_id"], ev["id"])
```

Import `leads as leads_db` and `commitments as commitments_db` from `gaia.core.db`.

- [ ] **Step 5: Declare the scoping, run, commit**

In `tests/test_scope.py`, add to `OWNERSHIP_SCOPED`:

```python
    "gaia.core.db.leads.by_event_ids":
        "which of this person's own calendar events belong to which lead, for "
        "their digest. Ownership for the same reason as due_for: a colleague's "
        "org-visible lead is readable but is not this person's day.",
    "gaia.core.db.commitments.by_event_ids":
        "the commitments half of the same question, scoped the same way.",
```

Run: `.venv/bin/python -m pytest -q` — Expected: PASS.

```bash
git add gaia/core/db/leads.py gaia/core/db/commitments.py gaia/capabilities/calendar/tools.py tests/test_calendar_correlation.py tests/test_scope.py
git commit -m "feat: tie calendar events to leads and commitments, both directions"
```

---

### Task 12: Conflicts in the digest

**Files:**
- Create: `gaia/jobs/conflicts.py`
- Modify: `gaia/jobs/digest.py`
- Test: `tests/test_conflicts.py`

**Interfaces:**
- Consumes: `cal.busy_intervals` (8), `google.RevokedGrant` (7), `by_event_ids` (11).
- Produces: `def overlaps(intervals) -> list[tuple]`; `def free_minutes(intervals, *, day, tz, start_hour=9, end_hour=18) -> int`; `async def for_user(conn, user, *, http, now) -> dict | None`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_conflicts.py
"""Interval arithmetic, computed in Python and never asked of the model.

today_line() exists because the model called 2026-09-11 'Friday' in one turn
and 'Thu' in the next. Reasoning over a day of overlapping events is strictly
harder than naming a weekday.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

from gaia.jobs import conflicts

TZ = ZoneInfo("America/New_York")


def _at(h, m=0):
    return datetime(2026, 9, 16, h, m, tzinfo=TZ)


def test_detects_an_overlap():
    assert conflicts.overlaps([(_at(10), _at(11)), (_at(10, 30), _at(11, 30))])


def test_touching_events_do_not_overlap():
    """A 10-11 and an 11-12 are back to back, not double-booked. Reporting
    those as conflicts would make every full day look broken."""
    assert conflicts.overlaps([(_at(10), _at(11)), (_at(11), _at(12))]) == []


def test_free_minutes_subtracts_busy_blocks():
    # 9-18 is 540 minutes; one two-hour meeting leaves 420.
    assert conflicts.free_minutes([(_at(10), _at(12))], day="2026-09-16", tz=TZ) == 420


def test_free_minutes_ignores_time_outside_the_working_day():
    """A 7am flight is not working time, and counting it would report a day as
    fuller than it is."""
    assert conflicts.free_minutes([(_at(6), _at(8))], day="2026-09-16", tz=TZ) == 540


def test_overlapping_meetings_are_not_double_counted():
    assert conflicts.free_minutes(
        [(_at(10), _at(12)), (_at(11), _at(13))], day="2026-09-16", tz=TZ) == 360


def test_no_events_is_a_whole_free_day():
    assert conflicts.free_minutes([], day="2026-09-16", tz=TZ) == 540
```

And the "once" behaviour, which is about the digest rather than the arithmetic — add to `tests/test_digest_calendar.py`:

```python
# tests/test_digest_calendar.py
from datetime import date, timedelta

from cryptography.fernet import Fernet
import httpx
import pytest

from gaia.core import crypto
from gaia.core.db import google_accounts as ga
from gaia.jobs import digest


@pytest.fixture(autouse=True)
def key(monkeypatch):
    monkeypatch.setattr(crypto.settings, "google_token_key", Fernet.generate_key().decode())


def _revoking_http():
    def handler(request):
        return httpx.Response(400, json={"error": "invalid_grant"})
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_revocation_is_announced_on_the_first_morning_after(conn, ana):
    await ga.upsert(conn, ana, google_email="a@x.com", refresh_token="1//r", scopes="s")
    async with _revoking_http() as http:
        out = await digest.calendar_section(
            conn, ana, http=http, today="2026-09-16",
            last_digest_on=date(2026, 9, 15),
        )
    assert out == {"revoked": True}


async def test_revocation_is_not_repeated_the_next_morning(conn, ana):
    """Someone may have revoked us deliberately. Telling them every morning
    is its own failure."""
    await ga.upsert(conn, ana, google_email="a@x.com", refresh_token="1//r", scopes="s")
    await ga.revoke(conn, ana)
    async with _revoking_http() as http:
        out = await digest.calendar_section(
            conn, ana, http=http, today="2026-09-18",
            last_digest_on=date.today() + timedelta(days=1),
        )
    assert out is None


async def test_a_user_who_never_connected_is_never_nagged(conn, ana):
    async with _revoking_http() as http:
        out = await digest.calendar_section(
            conn, ana, http=http, today="2026-09-16", last_digest_on=date(2026, 9, 15))
    assert out is None
```

- [ ] **Step 2: Run it and watch it fail**

Run: `.venv/bin/python -m pytest tests/test_conflicts.py -q`
Expected: FAIL — `No module named 'gaia.jobs.conflicts'`.

- [ ] **Step 3: Write the arithmetic**

```python
# gaia/jobs/conflicts.py
"""What today already contains, computed rather than inferred."""

from datetime import date as date_cls
from datetime import datetime, time, timedelta

# Hardcoded, deliberately. A users.working_hours column is the obvious next
# step and nothing needs it yet; inventing the column now would mean inventing
# a default for every existing row too.
WORK_START_HOUR = 9
WORK_END_HOUR = 18


def overlaps(intervals: list[tuple[datetime, datetime]]) -> list[tuple]:
    """Pairs that genuinely collide. Back-to-back is not a collision: a 10-11
    followed by an 11-12 is a normal day, and reporting it would make every
    busy day look broken."""
    out = []
    ordered = sorted(intervals)
    for i, (start, end) in enumerate(ordered):
        for other_start, other_end in ordered[i + 1:]:
            if other_start >= end:
                break
            out.append(((start, end), (other_start, other_end)))
    return out


def free_minutes(intervals, *, day: str, tz, start_hour: int = WORK_START_HOUR,
                 end_hour: int = WORK_END_HOUR) -> int:
    """Unbooked minutes inside the working day.

    Merged before subtracting, so two meetings that overlap each other are not
    counted twice -- otherwise a double-booked morning reports negative time
    and the digest says something absurd.
    """
    d = date_cls.fromisoformat(day)
    window_start = datetime.combine(d, time(start_hour), tzinfo=tz)
    window_end = datetime.combine(d, time(end_hour), tzinfo=tz)

    clipped = []
    for start, end in sorted(intervals):
        start, end = max(start, window_start), min(end, window_end)
        if start < end:
            clipped.append((start, end))

    merged: list[list[datetime]] = []
    for start, end in clipped:
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])

    busy = sum((e - s for s, e in merged), timedelta())
    return int(((window_end - window_start) - busy).total_seconds() // 60)
```

- [ ] **Step 4: Wire it into the digest**

In `gaia/jobs/digest.py`, add a function that the payload builder calls. It must never raise into the digest:

```python
async def calendar_section(conn, user: User, *, http, today: str, last_digest_on) -> dict | None:
    """Today's shape, or None.

    The calendar must never break the digest. This product's daily heartbeat
    going silent because Google had a bad morning would be a worse bug than the
    one this feature fixes, so every failure here degrades to None.
    """
    from zoneinfo import ZoneInfo

    from gaia.capabilities.calendar import client as cal
    from gaia.core import google
    from gaia.core.db import commitments as commitments_db
    from gaia.core.db import leads as leads_db
    from gaia.jobs import conflicts

    tz = ZoneInfo(user.timezone)
    try:
        start = datetime.combine(date_cls.fromisoformat(today), time(0), tzinfo=tz)
        events = await cal.list_events(conn, user, time_min=start,
                                       time_max=start + timedelta(days=1), http=http)
    except google.RevokedGrant:
        # Said out loud ONCE. A daily nag about an integration someone may have
        # revoked deliberately is its own failure -- and last_digest_on already
        # records when we last spoke, so knowing whether this is the first
        # morning since the revocation needs no new column.
        #
        # A user who never connected at all has no revoked_at and is never
        # nagged: they are not missing anything, they simply do not use it.
        from gaia.core.db import google_accounts as ga_db

        account = await ga_db.get(conn, user)
        revoked_on = account["revoked_at"].date() if account and account["revoked_at"] else None
        if revoked_on is None:
            return None
        if last_digest_on is not None and revoked_on < last_digest_on:
            return None
        return {"revoked": True}
    except Exception:
        log.exception("calendar section failed for %s", user.id)
        return None

    intervals = cal.busy_intervals(events)
    ids = [e["id"] for e in events if e.get("id")]
    linked = {**await leads_db.by_event_ids(conn, user, ids),
              **await commitments_db.by_event_ids(conn, user, ids)}
    return {
        "overlaps": [
            {"a": a[0].strftime("%H:%M"), "b": b[0].strftime("%H:%M")}
            for a, b in conflicts.overlaps(intervals)
        ],
        # Free minutes and a count, never a duration estimate. Gaia does not
        # know how long "send comps to Marcel" takes, and inventing forty
        # minutes would make it confidently wrong.
        "free_minutes": conflicts.free_minutes(intervals, day=today, tz=tz),
        "about": list(linked.values()),
    }
```

Add `date as date_cls`, `time` and `timedelta` to the existing datetime imports. `run_once` already reads `last_digest_on` for each user (`digest.py:62`) — pass that same value through to `calendar_section` rather than querying again. Include the returned dict in the JSON payload handed to the composer, and extend `SYSTEM` with one paragraph:

```
The payload may carry a calendar section. If two events overlap, say so plainly. \
If free_minutes is small and several follow-ups are due, say both numbers and let \
them judge - never estimate how long any task will take. If it says revoked, tell \
them their Google Calendar disconnected and offer to send a fresh link.
```

- [ ] **Step 5: Run and commit**

Run: `.venv/bin/python -m pytest -q` — Expected: PASS.

```bash
git add gaia/jobs/conflicts.py gaia/jobs/digest.py tests/test_conflicts.py tests/test_digest_calendar.py
git commit -m "feat: today's conflicts in the morning digest

Computed in Python and handed to the composer as data, for the same reason
today_line exists. Reports free minutes and a count rather than guessing how
long anything takes."
```

---

### Task 13: Document it, then deploy in the right order

**Files:**
- Modify: `docs/RUNBOOK.md`, `README.md`

- [ ] **Step 1: Name the new credentials in the RUNBOOK**

Names and purposes only, never values:

- `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` — the Internal OAuth client. Internal is load-bearing: it is what exempts the app from verification and the CASA security assessment, and it holds only while every user is on the Workspace domain.
- `GOOGLE_TOKEN_KEY` — Fernet key encrypting refresh tokens at rest. Generate with `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`. **Rotating it orphans every stored token**; everyone reconnects.
- `GOOGLE_DOMAIN` — the only domain whose accounts may connect.

- [ ] **Step 2: Write the deploy order down, because it is not optional**

```
1. Deploy. Migration 006 applies itself at startup.
2. For every existing developer:
   .venv/bin/python -m gaia.core.admin set-email --phone <wa_id> --email <addr>
3. Only then tell anyone the feature exists.
```

Every user predates `users.email`, and the OAuth callback refuses an account whose address does not match. Announce first and the very first person to try gets a 403 with no way to fix it themselves.

- [ ] **Step 3: Verify against production**

```bash
ssh gaia 'cd /opt/gaia-assistant && docker compose ps && curl -sf localhost:8000/health'
```

Containers running with 0 restarts, `/health` ok, logs clean. **Ask before writing to the droplet.**

- [ ] **Step 4: Commit**

```bash
git add docs/RUNBOOK.md README.md
git commit -m "docs: the Workspace credentials, and why set-email comes before the announcement"
```
