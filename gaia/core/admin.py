"""Roster management. The bootstrap: until one row exists in users, every
inbound number is ignored, so this cannot be replaced by an in-band flow."""

import argparse
import asyncio

from gaia.core.db import users as users_db
from gaia.core.db.pool import get_pool, tx


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m gaia.core.admin")
    sub = parser.add_subparsers(dest="command", required=True)

    add = sub.add_parser("add-user")
    add.add_argument("--name", required=True)
    add.add_argument("--phone", required=True, help="country code, no '+'")
    add.add_argument("--role", default="agent", choices=["agent", "admin"])
    add.add_argument("--tz", default="America/New_York")

    sub.add_parser("list-users")

    off = sub.add_parser("deactivate")
    off.add_argument("--phone", required=True)

    return parser


async def _run(args) -> None:
    pool = get_pool()
    await pool.open(wait=True)
    try:
        async with tx(pool) as conn:
            if args.command == "add-user":
                user = await users_db.create_user(
                    conn, name=args.name, wa_id=args.phone, role=args.role, timezone=args.tz
                )
                print(f"added {user.name} ({user.wa_id}) as {user.role}")
            elif args.command == "list-users":
                for u in await users_db.list_users(conn):
                    state = "active" if u.active else "inactive"
                    print(f"{u.wa_id:<15} {u.name:<20} {u.role:<8} {state}")
            elif args.command == "deactivate":
                ok = await users_db.deactivate(conn, args.phone)
                print("deactivated" if ok else f"no user with phone {args.phone}")
    finally:
        await pool.close()


def main() -> None:
    asyncio.run(_run(build_parser().parse_args()))


if __name__ == "__main__":
    main()
