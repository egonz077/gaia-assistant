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
