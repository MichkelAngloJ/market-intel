"""The ONE way app code reaches a MongoDB collection: :func:`collection`.

It accepts only the names in ``schemas.COLLECTIONS`` and raises before any database call
otherwise, so a typo can't quietly create a new, unvalidated collection. This is the code
check; the database check is the app role mi_app_rw, whose rights are granted per named
collection. (mongo_setup is the setup tool, not app code: it creates these collections.)
"""

from __future__ import annotations

from typing import Any

from market_intel.mongo.schemas import COLLECTIONS

COLLECTION_NAMES: frozenset[str] = frozenset(spec.name for spec in COLLECTIONS)


class UnknownCollectionError(ValueError):
    """A collection name that is not in schemas.COLLECTIONS."""


def collection(db: Any, name: str) -> Any:
    """``db[name]`` for a known collection name; UnknownCollectionError otherwise."""
    if name not in COLLECTION_NAMES:
        known = ", ".join(sorted(COLLECTION_NAMES))
        raise UnknownCollectionError(f"unknown collection {name!r}; known: {known}")
    return db[name]


def unexpected_collections(names: list[str]) -> list[str]:
    """Collections in the database that are not in the list (MongoDB's system.* excluded)."""
    return sorted(n for n in names if n not in COLLECTION_NAMES and not n.startswith("system."))
