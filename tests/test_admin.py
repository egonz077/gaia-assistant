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
    assert args.role == "agent"


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
