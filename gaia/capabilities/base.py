import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from gaia.core.db.pool import tx
from gaia.core.models import User

log = logging.getLogger("gaia.registry")

Handler = Callable[[Any, User, dict], Awaitable[dict]]


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    input_schema: dict
    handler: Handler

    def to_api(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }


@dataclass(frozen=True)
class Capability:
    name: str
    tools: tuple[Tool, ...]
    prompt_fragment: str = ""
    allowed_roles: frozenset[str] | None = None
    allowed_user_ids: frozenset[UUID] | None = None

    def visible_to(self, user: User) -> bool:
        """Public by default. A capability restricts itself; nothing else does."""
        if self.allowed_roles is not None and user.role in self.allowed_roles:
            return True
        if self.allowed_user_ids is not None and user.id in self.allowed_user_ids:
            return True
        return self.allowed_roles is None and self.allowed_user_ids is None


class Registry:
    def __init__(self) -> None:
        self._capabilities: list[Capability] = []

    def register(self, capability: Capability) -> None:
        self._capabilities.append(capability)

    def for_user(self, user: User) -> list[Capability]:
        return [c for c in self._capabilities if c.visible_to(user)]

    def tool_defs(self, user: User) -> list[dict]:
        return [t.to_api() for c in self.for_user(user) for t in c.tools]

    def prompt_fragments(self, user: User) -> str:
        return "".join(c.prompt_fragment for c in self.for_user(user))

    async def dispatch(self, pool, user: User, name: str, args: dict) -> str:
        """Re-checks visibility. Filtering the tool list is presentation; this
        is enforcement - a hallucinated tool name must not execute.

        Takes the connection *pool*, not a connection, and gives each handler
        its own transaction. Catching a handler's exception is only recovery
        if the connection is still usable afterwards, and for a database error
        it is not: psycopg leaves the connection INERROR, so every later
        statement on it raises InFailedSqlTransaction. With one transaction
        spanning the whole turn that meant a single bad tool call — a
        hallucinated uuid, an unparseable date — silently aborted the log of
        the assistant's reply too, and the rollback took with it the meeting
        the previous tool call had just saved. The user photographed her
        notes, was told something went wrong, and the notes were gone.

        Per-call transactions also mean a tool's successful writes are already
        committed if the turn fails later, and that no pooled connection is
        held across the model's own network round-trips.
        """
        for capability in self._capabilities:
            for tool in capability.tools:
                if tool.name != name:
                    continue
                if not capability.visible_to(user):
                    log.warning("user %s attempted hidden tool %s", user.id, name)
                    return f"tool {name} is not available"
                try:
                    async with tx(pool) as conn:
                        result = await tool.handler(conn, user, args)
                    return json.dumps(result, default=str)
                except Exception:
                    # Full detail (which may include SQL fragments, column
                    # names, etc.) stays server-side. The model only gets a
                    # generic signal it can act on, distinct from "unknown
                    # tool" and "not available" so it can respond sensibly.
                    log.exception("tool %s failed", name)
                    return (
                        f"tool {name} failed. Tell the user you could not "
                        "complete this and suggest trying again."
                    )
        return f"unknown tool {name}"


registry = Registry()
