import ast
import importlib
import inspect
import pkgutil
import textwrap

import gaia.core.db
from gaia.core.db.migrate import run_migrations
from gaia.core.db.scope import DOMAIN_TABLES, visible


def test_visible_fragment_uses_named_parameter():
    assert visible("m") == "(m.visibility = 'org' OR m.user_id = %(scope_user_id)s)"


async def test_every_visibility_table_is_registered(pool):
    """A new domain table must be added to DOMAIN_TABLES or the build breaks."""
    await run_migrations(pool)
    async with pool.connection() as conn:
        cur = await conn.execute(
            """SELECT table_name FROM information_schema.columns
               WHERE table_schema = 'public' AND column_name = 'visibility'"""
        )
        found = {r[0] for r in await cur.fetchall()}
    assert found == set(DOMAIN_TABLES), (
        f"tables with a visibility column but not in DOMAIN_TABLES: {found - set(DOMAIN_TABLES)}"
    )


# ---------------------------------------------------------------------------
# Every read in the data layer must declare how it is scoped.
#
# Injection is not the risk in this codebase — psycopg parameterises every
# value, and the only things interpolated into SQL are module constants and
# hardcoded literals. The risk is a new read that forgets visible() and
# quietly returns a colleague's client.
#
# So each read either composes visible(), or is listed below with the reason
# it does not. The list is the point: "this read is ownership-scoped" becomes
# a decision someone wrote down, rather than a line nobody noticed was
# missing.
# ---------------------------------------------------------------------------

EXEMPT_MODULES = {
    "gaia.core.db.users",    # the roster itself; keyed by wa_id, not by a user
    "gaia.core.db.pool",     # infrastructure
    "gaia.core.db.migrate",  # infrastructure
    "gaia.core.db.scope",    # defines visible(); cannot compose itself
}

OWNERSHIP_SCOPED = {
    "gaia.core.db.commitments.open_for":
        "the digest: what this person owes, not what they may read. Filtering "
        "by visibility would nag them every morning about a colleague's work.",
    "gaia.core.db.commitments.query":
        "'my commitments'. Ownership for the same reason as open_for, and "
        "because complete() refuses anything the user does not own — a "
        "visibility-scoped list would offer rows that then decline to close.",
    "gaia.core.db.google_accounts.get":
        "one developer's own OAuth grant. Not visibility-scoped because there "
        "is no setting at which a colleague's refresh token is readable -- a "
        "token answers whose account this is, which is ownership, never who "
        "may see it. The table has no visibility column for the same reason.",
    "gaia.core.db.leads.due_for":
        "the digest again: leads this person must act on. The docstring on it "
        "spells out why visible() would be wrong here.",
    "gaia.core.db.leads.by_event_ids":
        "which of this person's own calendar events belong to which lead, for "
        "their digest. Ownership for the same reason as due_for: a colleague's "
        "org-visible lead is readable but is not this person's day.",
    "gaia.core.db.commitments.by_event_ids":
        "the commitments half of the same question, scoped the same way.",
    "gaia.core.db.messages.recent":
        "one person's own conversation log. Another developer's messages are "
        "not org-visible content; they are simply not this user's thread.",
    "gaia.core.db.messages.seen":
        "WhatsApp redelivery dedup, keyed by wa_msg_id. Roster-independent by "
        "design, which is why test_db_signatures exempts it too.",
    "gaia.core.db.pending_invites.claim":
        "one person's own pending approval. Ownership, not visibility: a "
        "colleague may not confirm an invitation on someone else's behalf at "
        "any visibility setting, which is why the table has no visibility "
        "column.",
    "gaia.core.db.pending_invites.open_for":
        "the model's own way back to an approval it proposed a turn ago. "
        "History is prose, so every id dies at the turn boundary; this is the "
        "lookup that replaces guessing, and it answers whose approvals these "
        "are -- ownership, for the same reason as claim.",
    "gaia.core.db.users.get_by_id":
        "resolving the OAuth callback's state to the user who started the "
        "flow. users carries no visibility column at all -- it is the table "
        "visible() is defined in terms of.",
    "gaia.core.db.users.get_email":
        "one person's own Workspace address, compared against the account that "
        "just consented. Ownership by definition.",
}


def _module_string_constants(module) -> dict[str, str]:
    return {
        k: v.upper() for k, v in vars(module).items()
        if isinstance(v, str) and not k.startswith("__")
    }


def _analyse(fn, constants: dict[str, str]) -> tuple[bool, bool]:
    """Returns (calls_visible, reads_rows).

    Parsed rather than grepped, and deliberately so. A substring search for
    "visible(" matches `leads.due_for`, whose docstring explains why it must
    *not* use visible() — a tripwire that reads prose as code would pass the
    one function most worth checking. Same reason the docstring is dropped
    before looking for SELECT: several of these explain their SQL in words.
    """
    tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))

    calls_visible = any(
        isinstance(node, ast.Call) and getattr(node.func, "id", None) == "visible"
        for node in ast.walk(tree)
    )

    body = tree.body[0].body
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
        body = body[1:]

    fragments: list[str] = []
    for statement in body:
        for node in ast.walk(statement):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                fragments.append(node.value.upper())
            elif isinstance(node, ast.Name) and node.id in constants:
                # SQL built from a module constant, e.g. leads._SELECT.
                fragments.append(constants[node.id])

    return calls_visible, any("SELECT" in fragment for fragment in fragments)


def test_every_read_declares_how_it_is_scoped():
    undeclared = []
    for mod in pkgutil.iter_modules(gaia.core.db.__path__):
        name = f"gaia.core.db.{mod.name}"
        if name in EXEMPT_MODULES:
            continue
        module = importlib.import_module(name)
        constants = _module_string_constants(module)
        for fn_name, fn in inspect.getmembers(module, inspect.isfunction):
            if fn_name.startswith("_") or fn.__module__ != name:
                continue
            qualified = f"{name}.{fn_name}"
            calls_visible, reads_rows = _analyse(fn, constants)
            if not reads_rows or calls_visible or qualified in OWNERSHIP_SCOPED:
                continue
            undeclared.append(qualified)

    assert not undeclared, (
        "these reads neither compose visible() nor say why they do not — add "
        "visible(), or add them to OWNERSHIP_SCOPED with the reason: "
        + ", ".join(undeclared)
    )


def test_the_exemption_list_has_no_stale_entries():
    """An entry for a function that no longer exists, or that has since gained
    visible(), is a claim nobody is checking any more."""
    stale = []
    for qualified in OWNERSHIP_SCOPED:
        module_name, fn_name = qualified.rsplit(".", 1)
        module = importlib.import_module(module_name)
        fn = getattr(module, fn_name, None)
        if fn is None:
            stale.append(f"{qualified} (gone)")
            continue
        calls_visible, _ = _analyse(fn, _module_string_constants(module))
        if calls_visible:
            stale.append(f"{qualified} (now composes visible())")

    assert not stale, "stale OWNERSHIP_SCOPED entries: " + ", ".join(stale)


def test_every_exemption_carries_a_reason():
    """The reason is the whole mechanism. An empty string would turn a
    deliberate declaration back into a silent omission."""
    assert all(reason.strip() for reason in OWNERSHIP_SCOPED.values())
