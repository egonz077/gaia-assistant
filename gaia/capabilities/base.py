import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from uuid import UUID

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
    description: str
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

    def all(self) -> list[Capability]:
        return list(self._capabilities)

    def for_user(self, user: User) -> list[Capability]:
        return [c for c in self._capabilities if c.visible_to(user)]

    def tool_defs(self, user: User) -> list[dict]:
        return [t.to_api() for c in self.for_user(user) for t in c.tools]

    def prompt_fragments(self, user: User) -> str:
        return "".join(c.prompt_fragment for c in self.for_user(user))

    async def dispatch(self, conn, user: User, name: str, args: dict) -> str:
        """Re-checks visibility. Filtering the tool list is presentation; this
        is enforcement - a hallucinated tool name must not execute."""
        for capability in self._capabilities:
            for tool in capability.tools:
                if tool.name != name:
                    continue
                if not capability.visible_to(user):
                    log.warning("user %s attempted hidden tool %s", user.id, name)
                    return f"tool {name} is not available"
                try:
                    return json.dumps(await tool.handler(conn, user, args), default=str)
                except Exception as exc:  # surfaced to the model, not the user
                    log.exception("tool %s failed", name)
                    return f"tool error: {exc}"
        return f"unknown tool {name}"


registry = Registry()
