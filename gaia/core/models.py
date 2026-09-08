from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True)
class User:
    id: UUID
    name: str
    wa_id: str
    role: str
    timezone: str
    active: bool
