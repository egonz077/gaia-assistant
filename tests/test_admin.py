from psycopg.rows import dict_row

from gaia.core.admin import _run, build_parser
from gaia.core.db import users as users_db


def test_parser_accepts_add_user():
    args = build_parser().parse_args(
        ["add-user", "--name", "Ana", "--phone", "13055550001", "--role", "admin"]
    )
    assert args.command == "add-user"
    assert args.name == "Ana"
    assert args.phone == "13055550001"
    assert args.role == "admin"


def test_parser_defaults_role_to_agent():
    args = build_parser().parse_args(["add-user", "--name", "Ana", "--phone", "1305"])
    assert args.role == "developer"


def test_parser_accepts_deactivate():
    args = build_parser().parse_args(["deactivate", "--phone", "13055550001"])
    assert args.command == "deactivate"


# --- end-to-end: this CLI is the bootstrap (until one row exists in users,
# every inbound WhatsApp number is ignored), so "the parser parses" is not
# evidence that add-user actually works. Exercise _run() against the real
# test pool and assert on database state, not just on printed text. `_run`
# takes an injectable `pool` for exactly this: it will not close a pool it
# didn't open itself, so the shared `migrated` pool survives past the call
# for fixture teardown and further assertions.


async def test_run_add_user_creates_a_resolvable_user(migrated, capsys):
    args = build_parser().parse_args(
        ["add-user", "--name", "Ana", "--phone", "13055550001", "--role", "admin", "--tz", "America/Chicago"]
    )
    await _run(args, pool=migrated)

    out = capsys.readouterr().out
    assert "Ana" in out
    assert "13055550001" in out
    assert "admin" in out

    async with migrated.connection() as conn:
        conn.row_factory = dict_row
        found = await users_db.get_by_wa_id(conn, "13055550001")
    assert found is not None
    assert found.name == "Ana"
    assert found.role == "admin"
    assert found.timezone == "America/Chicago"


async def test_run_list_users_prints_existing_users(migrated, capsys):
    await _run(build_parser().parse_args(["add-user", "--name", "Ana", "--phone", "13055550001"]), pool=migrated)
    capsys.readouterr()  # discard add-user's own output

    await _run(build_parser().parse_args(["list-users"]), pool=migrated)
    out = capsys.readouterr().out
    assert "13055550001" in out
    assert "Ana" in out
    assert "active" in out


async def test_run_deactivate_revokes_access_and_reports_missing_phone(migrated, capsys):
    await _run(build_parser().parse_args(["add-user", "--name", "Ana", "--phone", "13055550001"]), pool=migrated)
    capsys.readouterr()

    await _run(build_parser().parse_args(["deactivate", "--phone", "13055550001"]), pool=migrated)
    out = capsys.readouterr().out
    assert "deactivated" in out

    async with migrated.connection() as conn:
        conn.row_factory = dict_row
        assert await users_db.get_by_wa_id(conn, "13055550001") is None

    await _run(build_parser().parse_args(["deactivate", "--phone", "19998887777"]), pool=migrated)
    out = capsys.readouterr().out
    assert "no user with phone 19998887777" in out


# --- I8: an unvalidated timezone is a company-wide outage, not a typo. It
# raises inside due_users' per-user loop, so one bad row means no user gets a
# digest ever; and inside build_system_prompt, so that agent gets the apology
# text for every message she sends, forever.


def test_parser_rejects_an_unknown_timezone(capsys):
    import pytest

    with pytest.raises(SystemExit):
        build_parser().parse_args(
            ["add-user", "--name", "Ana", "--phone", "1305", "--tz", "America/NewYork"]
        )
    assert "unknown timezone" in capsys.readouterr().err


def test_parser_accepts_a_real_timezone():
    args = build_parser().parse_args(
        ["add-user", "--name", "Ana", "--phone", "1305", "--tz", "America/Chicago"]
    )
    assert args.tz == "America/Chicago"


# --- I9: merge-contacts is the documented remedy for the non-unique name
# index and for get_or_create's race. Spec §7 listed it; build_parser did not.


async def test_run_merge_contacts_folds_one_contact_into_another(migrated, capsys):
    from psycopg.rows import dict_row

    from gaia.core.db import contacts as contacts_db

    await _run(
        build_parser().parse_args(["add-user", "--name", "Ana", "--phone", "13055550001"]),
        pool=migrated,
    )
    capsys.readouterr()

    async with migrated.connection() as conn:
        conn.row_factory = dict_row
        async with conn.transaction():
            ana = await users_db.get_by_wa_id(conn, "13055550001")
            keep = await contacts_db.create_contact(conn, ana, name="Maria Delgado")
            dupe = await contacts_db.create_contact(conn, ana, name="maria delgado")
            await contacts_db.merge_profile(conn, ana, dupe, "budget 600k")

    await _run(
        build_parser().parse_args([
            "merge-contacts", "--from", str(dupe), "--into", str(keep),
            "--as", "13055550001",
        ]),
        pool=migrated,
    )
    assert "merged" in capsys.readouterr().out

    async with migrated.connection() as conn:
        conn.row_factory = dict_row
        async with conn.transaction():
            found = await contacts_db.lookup(conn, ana, "Maria Delgado")
            cur = await conn.execute("SELECT count(*) AS n FROM contacts")
            remaining = (await cur.fetchone())["n"]
    assert remaining == 1
    assert "budget 600k" in found["profile"]


async def test_run_merge_contacts_reports_an_unknown_acting_user(migrated, capsys):
    from uuid import uuid4

    await _run(
        build_parser().parse_args([
            "merge-contacts", "--from", str(uuid4()), "--into", str(uuid4()),
            "--as", "19998887777",
        ]),
        pool=migrated,
    )
    assert "no active user with phone 19998887777" in capsys.readouterr().out


async def test_stats_prints_totals_and_the_cache_hit_rate(migrated, capsys):
    from gaia.core.db.pool import tx

    async with tx(migrated) as conn:
        ana = await users_db.create_user(conn, name="Ana", wa_id="13055550001")
        await conn.execute(
            """INSERT INTO model_calls (job, user_id, model, input_tokens, output_tokens,
                                      cache_creation_input_tokens, cache_read_input_tokens)
               VALUES ('turn', %s, 'claude-opus-5', 100, 10, 300, 600)""",
            (ana.id,),
        )

    await _run(build_parser().parse_args(["stats"]), pool=migrated)

    out = capsys.readouterr().out
    assert "turn" in out
    assert "60%" in out, "the cache hit rate must appear as a percentage"
    assert "Ana" in out


async def test_stats_on_an_empty_database_says_so(migrated, capsys):
    """A fresh deploy must produce a readable report, not a crash."""
    await _run(build_parser().parse_args(["stats"]), pool=migrated)

    out = capsys.readouterr().out
    assert "no model calls" in out.lower()


async def test_stats_honours_the_days_window(migrated, capsys):
    from gaia.core.db.pool import tx

    async with tx(migrated) as conn:
        ana = await users_db.create_user(conn, name="Ana", wa_id="13055550001")
        await conn.execute(
            """INSERT INTO model_calls (job, user_id, model, input_tokens, output_tokens,
                                      created_at)
               VALUES ('turn', %s, 'claude-opus-5', 100, 10, now() - interval '10 days')""",
            (ana.id,),
        )

    await _run(build_parser().parse_args(["stats", "--days", "7"]), pool=migrated)

    assert "no model calls" in capsys.readouterr().out.lower()


async def test_stats_html_writes_a_self_contained_page(migrated, capsys):
    await _run(build_parser().parse_args(["stats", "--html"]), pool=migrated)

    out = capsys.readouterr().out
    assert out.strip().startswith("<!doctype html>")
    assert "<script" not in out.lower()
    assert "https://" not in out, "no CDN — it has to open from a file:// URL"
