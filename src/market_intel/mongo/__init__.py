"""MongoDB documents: models (models.py), collection validators and indexes (schemas.py)
and the append-only prediction audit writer (audit.py).

Collections are created only by ``uv run python -m market_intel.mongo_setup`` (setup login);
the app login cannot create collections or change indexes and validators.
"""
