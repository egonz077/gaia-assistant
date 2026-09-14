"""Roster management. The bootstrap: until one row exists in users, every
inbound number is ignored, so this cannot be replaced by an in-band flow."""

import argparse
import asyncio
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from gaia.core import stats as stats_mod
from gaia.core import stats_html
from gaia.core.db import contacts as contacts_db
from gaia.core.db import users as users_db
from gaia.core.db.pool import get_pool, tx


def _timezone(value: str) -> str:
    """Validate at the only boundary where a timezone enters the system.

    `users.timezone` is plain TEXT and nothing downstream tolerates a bad one.
    ZoneInfo() raises inside `due_users`' per-user loop, so one typo meant
    *nobody* got a digest, ever, with a single `digest run failed` line every
    fifteen minutes; and it raises inside build_system_prompt, so that agent
    got the apology text for every message she sent, forever. A typo at the
    CLI must not be able to do that, and the digest loop is also guarded
    (jobs/digest.py) so a row that got in some other way cannot either.
    """
    try:
        ZoneInfo(value)
    except (ValueError, ZoneInfoNotFoundError) as exc:
        raise argparse.ArgumentTypeError(
            f"unknown timezone {value!r} — use an IANA name, e.g. America/New_York"
        ) from exc
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m gaia.core.admin")
    sub = parser.add_subparsers(dest="command", required=True)

    add = sub.add_parser("add-user")
    add.add_argument("--name", required=True)
    add.add_argument("--phone", required=True, help="country code, no '+'")
    add.add_argument("--role", default="developer", choices=["developer", "admin"])
    add.add_argument("--tz", default="America/New_York", type=_timezone)

    sub.add_parser("list-users")

    off = sub.add_parser("deactivate")
    off.add_argument("--phone", required=True)

    merge = sub.add_parser(
        "merge-contacts",
        help="fold one contact into another; the --from row is deleted",
    )
    merge.add_argument("--from", dest="source", required=True, help="contact id, deleted")
    merge.add_argument("--into", dest="target", required=True, help="contact id, kept")
    # Not in the spec's usage line, and required anyway: the merge is scoped
    # by what this agent can see. Without it, someone with a shell could fold
    # an agent's private contact into an org row and publish its profile to
    # the whole company — the one failure the visibility system exists for.
    merge.add_argument(
        "--as", dest="acting_phone", required=True,
        help="phone of the developer the merge runs as; they must see both rows",
    )

    stats_cmd = sub.add_parser(
        "stats", help="what the product cost to run, and what it produced"
    )
    stats_cmd.add_argument("--days", type=int, default=30)
    stats_cmd.add_argument(
        "--html", action="store_true",
        help="write a self-contained page to stdout instead of text",
    )

    return parser


def _format_stats(d: dict) -> str:
    """Plain text, because the common case is reading this over ssh."""
    lines = [f"Last {d['days']} days", ""]

    t = d["totals"]
    if not t["calls"]:
        lines += ["no model calls recorded in this window", ""]
    else:
        lines += [
            f"  {t['calls']} model calls, ${t['cost']:.2f}",
            f"  {t['input_tokens']:,} input tokens, {t['output_tokens']:,} output",
        ]
        if d["cache_hit_rate"] is not None:
            lines.append(f"  cache hit rate: {d['cache_hit_rate'] * 100:.0f}%")
        lines.append("")

        lines.append("By job")
        for r in d["by_job"]:
            lines.append(f"  {r['job']:<14} {r['calls']:>5} calls  ${r['cost']:>8.2f}")
        lines.append("")

        lines.append("By developer")
        for r in d["by_user"]:
            lines.append(f"  {r['name']:<22} {r['calls']:>5} calls  ${r['cost']:>8.2f}")
        lines.append("")

        if d["calls_per_turn"]:
            # MAX_ITERATIONS is 8; a turn that reached the cap returned
            # FALLBACK_TEXT rather than an answer.
            spread = ", ".join(f"{k}:{v}" for k, v in sorted(d["calls_per_turn"].items()))
            lines += [f"Calls per turn: {spread}", ""]

        if d["stop_reasons"]:
            reasons = ", ".join(
                f"{k or 'none'}={v}"
                for k, v in sorted(d["stop_reasons"].items(), key=lambda kv: str(kv[0]))
            )
            lines += [f"Stop reasons: {reasons}", ""]

    if d["unknown_models"]:
        lines += [
            "Tokens counted with no cost — no rate card for: "
            + ", ".join(d["unknown_models"]),
            "",
        ]

    m, c, le = d["meetings"], d["commitments"], d["leads"]
    cpm = d["contacts_per_meeting"]
    lines += [
        "Product",
        f"  meetings filed     {m['total']}  ({m['photo']} photo, {m['text']} text)",
        f"  contacts/meeting   {cpm['mean']:.1f} mean, {cpm['max']} max",
        f"  commitments        {c['total']}  ({c['with_due_date']} dated, {c['done']} done)",
        f"  leads opened       {le['total']}  ({le['with_next_action']} with a next action)",
        f"  active developers  {d['users']['active']}  "
        f"({d['users']['digested_today']} digested today)",
    ]
    return "\n".join(lines)


async def _run(args, pool=None) -> None:
    """Run one subcommand against `pool`, or the process-wide pool if omitted.

    An injected pool is the caller's to open/close (tests own the lifecycle
    of their test pool); only a pool we constructed ourselves here — the
    `main()` path — gets opened and closed by this function.
    """
    owns_pool = pool is None
    p = pool or get_pool()
    await p.open(wait=True)
    try:
        async with tx(p) as conn:
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
            elif args.command == "merge-contacts":
                acting = await users_db.get_by_wa_id(conn, args.acting_phone)
                if acting is None:
                    print(f"no active user with phone {args.acting_phone}")
                elif await contacts_db.merge(conn, acting, args.source, args.target):
                    print(f"merged {args.source} into {args.target}")
                else:
                    print(
                        f"nothing merged: {acting.name} cannot see both contacts, "
                        "or they are the same row"
                    )
            elif args.command == "stats":
                data = await stats_mod.collect(conn, days=args.days)
                print(stats_html.render(data) if args.html else _format_stats(data))
    finally:
        if owns_pool:
            await p.close()


def main() -> None:
    asyncio.run(_run(build_parser().parse_args()))


if __name__ == "__main__":
    main()
