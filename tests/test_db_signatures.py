import importlib
import inspect
import pkgutil

import gaia.core.db

# users.py manages the roster itself, so its functions take wa_id, not a User.
# pool.py / migrate.py are infrastructure, not domain-scoped queries.
# scope.py builds the `visible()` fragment other modules use; it has no user
# to receive.
# messages.seen(conn, wa_msg_id) is a single named exemption within messages:
# WhatsApp redelivery dedup is roster-independent by design (see report), so
# it cannot take a user as its second parameter. Rather than exempt the whole
# module (which would blind this tripwire to log()/recent()), we exempt only
# that one qualified function name below.
EXEMPT = {"gaia.core.db.users", "gaia.core.db.pool", "gaia.core.db.migrate", "gaia.core.db.scope"}
EXEMPT_FUNCTIONS = {"gaia.core.db.messages.seen"}


def test_every_db_function_takes_user_first():
    """A tripwire for carelessness, not a proof of correct scoping — the
    parametrized isolation suite is what establishes that. This only checks
    that a function's second parameter is *named* `user`; it does not check
    that the function actually scopes its query by it."""
    offenders = []
    for mod in pkgutil.iter_modules(gaia.core.db.__path__):
        name = f"gaia.core.db.{mod.name}"
        if name in EXEMPT:
            continue
        module = importlib.import_module(name)
        for fn_name, fn in inspect.getmembers(module, inspect.isfunction):
            if fn_name.startswith("_") or fn.__module__ != name:
                continue
            if f"{name}.{fn_name}" in EXEMPT_FUNCTIONS:
                continue
            params = list(inspect.signature(fn).parameters)
            if params[:2] != ["conn", "user"]:
                offenders.append(f"{name}.{fn_name}{tuple(params)}")
    assert not offenders, "must take (conn, user, ...): " + ", ".join(offenders)
