"""The only place read-filtering lives.

visibility governs reads; user_id governs responsibility. Anything that tells
a person what to do filters on ownership, not on this fragment.
"""

DOMAIN_TABLES: tuple[str, ...] = (
    "contacts",
    "leads",
    "meetings",
    "commitments",
    "memory_chunks",
)


def visible(alias: str) -> str:
    """SQL predicate restricting a domain table to what `scope_user_id` may read."""
    return f"({alias}.visibility = 'org' OR {alias}.user_id = %(scope_user_id)s)"
