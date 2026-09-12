"""Fixtures for the live tier: real Anthropic, real Voyage, real Postgres.

Never a real WhatsApp send. That is the one boundary that stays faked, and
`no_real_whatsapp` below makes it structural rather than a convention — these
tests exist to exercise the parts of the system that only real services can
exercise, not to message a real person.

Credentials: tests/conftest.py pins ANTHROPIC_API_KEY and VOYAGE_API_KEY to
placeholders before any gaia import, deliberately, so that a developer's real
.env cannot steer the hermetic suite. That pinning must stay, so this package
reads .env itself instead of unpinning anything. A missing or placeholder key
skips rather than fails: a clean checkout can never spend money by accident.
"""

import os
import re
from pathlib import Path

import pytest
import pytest_asyncio

REPO_ROOT = Path(__file__).resolve().parents[2]

# Values that look like credentials but are not. The first two are what
# tests/conftest.py exports; the rest are what .env.example ships with.
PLACEHOLDERS = {"sk-ant-test", "pa-test", "test-token", "test-secret", "test-verify"}


def _dotenv() -> dict[str, str]:
    path = REPO_ROOT / ".env"
    if not path.exists():
        return {}
    out: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        # Trailing comments are split on whitespace-then-# rather than on a
        # bare "#", which would truncate a credential that contains one.
        value = re.split(r"\s+#", value, maxsplit=1)[0].strip().strip("'\"")
        out[key.strip()] = value
    return out


def _real_key(name: str) -> str | None:
    """A usable credential for `name`, or None.

    A genuinely exported variable wins; .env is the fallback. Anything
    matching a known placeholder — or ending in "...", which is how
    .env.example writes an elided key — counts as absent.
    """
    for value in (os.environ.get(name, ""), _dotenv().get(name, "")):
        value = value.strip()
        if value and value not in PLACEHOLDERS and not value.endswith("..."):
            return value
    return None


@pytest.fixture(autouse=True)
def fake_embed(monkeypatch):
    """Decide once, for the whole package, how embedding works here.

    Keeps the parent fixture's name on purpose: tests/conftest.py declares
    `fake_embed` autouse for the entire tree, so overriding it by name is what
    stops it patching these tests. Shadowing it and doing nothing is not
    enough, though — gaia.core.embeddings builds its module-level `_client` at
    import time from settings.voyage_api_key, which is the pinned placeholder
    by then. Restoring the real `embed` function while leaving that client in
    place produces "Provided API key is invalid" from Voyage, which is how the
    agent tests failed the first time this ran.

    So with a real key: swap the client too, and everything here embeds for
    real. Without one: reinstate the parent's fake, so tests that want only a
    real *model* still run instead of dying on a placeholder credential. The
    tests that assert on similarity depend on `voyage` below, which skips.
    """
    key = _real_key("VOYAGE_API_KEY")
    if key:
        import voyageai

        import gaia.core.embeddings as embeddings

        monkeypatch.setattr(embeddings, "_client", voyageai.Client(api_key=key))
        return

    from gaia.core.embeddings import EMBED_DIM

    async def _embed(texts, input_type="document"):
        return [[float(len(t) % 7)] + [0.0] * (EMBED_DIM - 1) for t in texts]

    monkeypatch.setattr("gaia.core.embeddings.embed", _embed)
    monkeypatch.setattr("gaia.core.db.memory.embed", _embed)


@pytest.fixture(autouse=True)
def no_real_whatsapp(monkeypatch):
    """Make a real Graph API send impossible, not merely discouraged.

    Every test here is handed FakeWhatsApp explicitly. This is the backstop
    for the day someone writes a live test that reaches for the real client
    out of habit: constructing one raises instead of texting an agent at Gaia.
    """

    def _forbidden(*args, **kwargs):
        raise AssertionError(
            "live tests must never construct WhatsAppClient — inject FakeWhatsApp"
        )

    monkeypatch.setattr("gaia.core.whatsapp.WhatsAppClient.__init__", _forbidden)


@pytest.fixture(scope="session")
def anthropic_key() -> str:
    key = _real_key("ANTHROPIC_API_KEY")
    if not key:
        pytest.skip("no real ANTHROPIC_API_KEY in the environment or .env")
    return key


@pytest.fixture(scope="session")
def voyage_key() -> str:
    key = _real_key("VOYAGE_API_KEY")
    if not key:
        pytest.skip("no real VOYAGE_API_KEY in the environment or .env")
    return key


@pytest_asyncio.fixture
async def claude(anthropic_key):
    """A real Anthropic client.

    Constructed with an explicit key rather than zero-arg like butler.py does:
    the process environment holds the pinned placeholder, so the SDK's own
    credential resolution would find that instead.
    """
    from anthropic import AsyncAnthropic

    client = AsyncAnthropic(api_key=anthropic_key)
    yield client
    await client.close()


@pytest.fixture
def voyage(voyage_key):
    """Skip gate for tests that assert on real similarity.

    The client swap itself happens in fake_embed, for the whole package, so
    this only has to establish that a real key exists — requesting voyage_key
    skips the test when it does not.
    """
    return voyage_key


@pytest_asyncio.fixture
async def ana(migrated):
    """A committed user row, unlike the hermetic suite's `ana`.

    The agent loop and every capability tool open their own transactions on
    other pooled connections (Registry.dispatch does this deliberately), so a
    users row left uncommitted in the test's own session is invisible to them
    and their inserts fail on the foreign key instead. tests/test_webhook.py's
    `wa_user` fixture exists for exactly this reason.
    """
    from gaia.core.db import users as users_db
    from gaia.core.db.pool import tx

    async with tx(migrated) as conn:
        return await users_db.create_user(conn, name="Ana", wa_id="13055550001")


@pytest_asyncio.fixture
async def sofia(migrated):
    from gaia.core.db import users as users_db
    from gaia.core.db.pool import tx

    async with tx(migrated) as conn:
        return await users_db.create_user(conn, name="Sofia", wa_id="13055550002")
