"""Create or update the MongoDB collections: ``uv run python -m market_intel.mongo_setup``.

Uses the SETUP login (MONGODB_ADMIN_URI: readWrite + dbAdmin), through the same connection
function as the app (same TLS refusal). Runs only when started by hand.

Idempotent, and it never drops anything:
- creates missing collections, with their $jsonSchema validator;
- updates the validator of an existing collection (collMod) when it differs;
- creates missing indexes.
If an index exists with the same name but a different definition (or the same keys under
another name), it reports the conflict and stops before changing anything.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from pymongo.database import Database
from pymongo.errors import PyMongoError

from market_intel.config import ConfigError, get_settings
from market_intel.db.common import InsecureConnectionError, parse_url, redactor
from market_intel.db.mongo import client_url
from market_intel.logging_setup import bound_context, configure_logging, get_logger, new_run_id
from market_intel.mongo.schemas import (
    COLLECTIONS,
    VALIDATION_ACTION,
    VALIDATION_LEVEL,
    CollectionSpec,
    IndexSpec,
)

log = get_logger("market_intel.mongo_setup")


@dataclass
class Action:
    kind: str  # "created" or "updated"
    description: str
    apply: Callable[[], Any]


@dataclass
class Plan:
    actions: list[Action] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)


def _keys(index_info: dict[str, Any]) -> list[tuple[str, int]]:
    return [(name, int(direction)) for name, direction in index_info["key"]]


def _validator_options(spec: CollectionSpec) -> dict[str, Any]:
    return {
        "validator": spec.validator,
        "validationLevel": VALIDATION_LEVEL,
        "validationAction": VALIDATION_ACTION,
    }


def _plan_indexes(
    db: Database, spec: CollectionSpec, current: dict[str, dict[str, Any]], plan: Plan
) -> None:
    for idx in spec.indexes:
        label = f"{spec.name}: index {idx.name}"
        wanted = [(name, int(direction)) for name, direction in idx.keys]
        info = current.get(idx.name)
        if info is None:
            twins = [name for name, other in current.items() if _keys(other) == wanted]
            if twins:
                plan.conflicts.append(f"{label}: the same keys are already indexed as {twins[0]}")
            else:
                plan.actions.append(Action("created", label, _index_creator(db, spec, idx)))
        elif _keys(info) != wanted or bool(info.get("unique", False)) != idx.unique:
            plan.conflicts.append(
                f"{label}: exists with a different definition "
                f"(keys {_keys(info)}, unique={bool(info.get('unique', False))}; "
                f"wanted keys {wanted}, unique={idx.unique})"
            )
        else:
            plan.unchanged.append(label)


def _index_creator(db: Database, spec: CollectionSpec, idx: IndexSpec) -> Callable[[], Any]:
    options = {"name": idx.name, **({"unique": True} if idx.unique else {})}
    return lambda: db[spec.name].create_index(list(idx.keys), **options)


def make_plan(db: Database) -> Plan:
    """Compare the database with schemas.COLLECTIONS. Reads only; changes nothing."""
    plan = Plan()
    existing = {info["name"]: info for info in db.list_collections()}
    for spec in COLLECTIONS:
        info = existing.get(spec.name)
        if info is None:
            options = _validator_options(spec) if spec.validator else {}
            what = "collection with validator" if spec.validator else "collection"
            plan.actions.append(
                Action(
                    "created",
                    f"{spec.name}: {what}",
                    lambda spec=spec, options=options: db.create_collection(spec.name, **options),
                )
            )
            current: dict[str, dict[str, Any]] = {}
        else:
            plan.unchanged.append(f"{spec.name}: collection")
            if spec.validator is not None:
                opts = info.get("options", {})
                have = (
                    opts.get("validator"),
                    opts.get("validationLevel", "strict"),
                    opts.get("validationAction", "error"),
                )
                if have != (spec.validator, VALIDATION_LEVEL, VALIDATION_ACTION):
                    options = _validator_options(spec)
                    plan.actions.append(
                        Action(
                            "updated",
                            f"{spec.name}: validator (collMod)",
                            lambda spec=spec, options=options: db.command(
                                "collMod", spec.name, **options
                            ),
                        )
                    )
                else:
                    plan.unchanged.append(f"{spec.name}: validator")
            current = db[spec.name].index_information()
        _plan_indexes(db, spec, current, plan)
    return plan


def run(db: Database, host: str) -> int:
    """Plan, stop on conflicts, otherwise apply. Prints a summary. Returns the exit code."""
    print(f"MongoDB setup on host: {host}, database: {db.name}")
    plan = make_plan(db)
    if plan.conflicts:
        for conflict in plan.conflicts:
            print(f"  CONFLICT {conflict}")
            log.error("mongo_setup_conflict", detail=conflict)
        print("stopped: conflicts found, nothing was changed (fix by hand, never by dropping)")
        return 1
    for action in plan.actions:
        action.apply()
        print(f"  {action.kind:<8} {action.description}")
        log.info("mongo_setup_change", change=action.kind, detail=action.description)
    created = sum(a.kind == "created" for a in plan.actions)
    updated = sum(a.kind == "updated" for a in plan.actions)
    if not plan.actions:
        print(f"nothing to change ({len(plan.unchanged)} items already up to date)")
    else:
        print(f"summary: created {created}, updated {updated}, unchanged {len(plan.unchanged)}")
    return 0


def main() -> int:
    """Entry point for ``python -m market_intel.mongo_setup``."""
    try:
        settings = get_settings()
        admin = settings.require("mongo_admin")
    except ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 1
    configure_logging(settings)
    url = admin.mongodb_admin_uri.get_secret_value()
    redact = redactor(url)
    with bound_context(run_id=new_run_id(), component="mongo_setup"):
        try:
            client = client_url(
                url, profile=admin.app_profile, timeout_s=admin.db_connect_timeout_s
            )
        except InsecureConnectionError as exc:
            print(f"Refused: {exc}", file=sys.stderr)
            return 1
        try:
            with client:
                return run(client[admin.mongodb_db], parse_url(url).hosts)
        except PyMongoError as exc:
            message = f"{type(exc).__name__}: {redact(str(exc)).strip()}"
            log.error("mongo_setup_failed", error=message)
            print(f"MongoDB error: {message}", file=sys.stderr)
            return 1


if __name__ == "__main__":
    sys.exit(main())
