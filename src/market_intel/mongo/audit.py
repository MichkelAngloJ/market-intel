"""Append-only prediction audit records (collection ``prediction_audit``).

These two functions are the ONLY code that writes audit records:

- :func:`record_prediction` inserts a new audit once (outcome empty, status "pending").
- :func:`record_outcome` adds the outcome once (status "pending" -> "filled").

There is deliberately no function to change or delete an audit. This rule is enforced in
code, not by MongoDB rights: the app login (role mi_app_rw) can technically update and remove
documents in every collection. A stricter per-collection role can be added later.
"""

from __future__ import annotations

from pymongo.database import Database

from market_intel.mongo.models import Outcome, PredictionAudit
from market_intel.mongo.schemas import PREDICTION_AUDIT

COLLECTION = PREDICTION_AUDIT.name


class AuditError(Exception):
    """An audit write that would break the append-only rule."""


def record_prediction(db: Database, audit: PredictionAudit) -> str:
    """Insert a new audit record. Returns its prediction_id.

    The PredictionAudit model has already applied the leakage guard. The record must not
    carry an outcome yet; a duplicate prediction_id is rejected by the unique index.
    """
    if audit.outcome is not None or audit.outcome_status != "pending":
        raise AuditError("a new audit record must have outcome_status 'pending' and no outcome")
    db[COLLECTION].insert_one(audit.to_document())
    return audit.prediction_id


def record_outcome(db: Database, prediction_id: str, outcome: Outcome) -> None:
    """Add the outcome to a pending audit, exactly once.

    The filter matches only a *pending* record, so a second call (or an unknown id) changes
    nothing and raises AuditError.
    """
    result = db[COLLECTION].update_one(
        {"prediction_id": prediction_id, "outcome_status": "pending"},
        {"$set": {"outcome": outcome.model_dump(), "outcome_status": "filled"}},
    )
    if result.matched_count != 1:
        raise AuditError(
            f"no pending audit with prediction_id {prediction_id}: already filled or unknown"
        )
