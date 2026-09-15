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


async def test_cli_set_email_updates_the_row(migrated, conn, ana, capsys):
    # `ana` was inserted on the `conn` fixture's connection, which stays open
    # (uncommitted) for the fixture's whole life. `_run(pool=migrated)` opens
    # a second connection via tx(); under READ COMMITTED that connection
    # cannot see ana's row until this one commits.
    await conn.commit()
    args = build_parser().parse_args(
        ["set-email", "--phone", ana.wa_id, "--email", "ana@gaiagroupdevelopment.com"]
    )
    await _run(args, pool=migrated)
    assert "ana@gaiagroupdevelopment.com" in capsys.readouterr().out
