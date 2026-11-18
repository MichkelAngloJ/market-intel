"""Alembic environment: runs migrations with the owner login (POSTGRES_ADMIN_URL).

Run by hand only: ``uv run alembic upgrade head``. Nothing in the app runs migrations.

- The URL comes from the settings (.env), never from alembic.ini, and is never printed.
- Connections go through ``market_intel.db.postgres.connect_url``, the same function the app
  uses, so the same TLS refusal applies (no unencrypted connection to a non-local host).
- ``target_metadata`` is ``market_intel.db.schema.metadata``; ``alembic check`` compares it
  with the database to catch drift.
"""

from __future__ import annotations

import sys
from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool

from market_intel.config import ConfigError, get_settings
from market_intel.db import postgres
from market_intel.db.common import InsecureConnectionError, parse_url
from market_intel.db.schema import metadata

config = context.config
if config.config_file_name is not None and config.attributes.get("configure_logger", True):
    # Keep loggers that already exist (the app's structlog setup, tests).
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = metadata

# Shared by online and offline mode.
COMPARE_OPTS = {"compare_type": True}


def to_psycopg_url(url: str) -> str:
    """SQLAlchemy needs the driver in the scheme: postgresql:// -> postgresql+psycopg://."""
    for prefix in ("postgresql://", "postgres://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix) :]
    return url


def _admin_url_and_profile() -> tuple[str, str, int]:
    try:
        admin = get_settings().require("database_admin")
    except ConfigError as exc:
        sys.exit(f"Migrations need the owner login. {exc}")
    return (
        admin.postgres_admin_url.get_secret_value(),
        admin.app_profile,
        admin.db_connect_timeout_s,
    )


def run_migrations_offline() -> None:
    """``alembic upgrade head --sql``: print the SQL without connecting (no URL needed)."""
    context.configure(
        dialect_name="postgresql",
        target_metadata=target_metadata,
        literal_binds=True,
        **COMPARE_OPTS,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    url, profile, timeout_s = _admin_url_and_profile()
    try:
        postgres.ensure_tls(url, profile)  # fail fast with a clear message (host only)
    except InsecureConnectionError as exc:
        sys.exit(f"Migration refused: {exc}")

    # The engine only knows the driver; the real URL is passed straight to psycopg through
    # connect_url, so SQLAlchemy never holds (or prints) it. autocommit=False: Alembic runs
    # each migration inside a transaction, and Postgres can roll back DDL.
    engine = create_engine(
        to_psycopg_url("postgresql://"),
        creator=lambda: postgres.connect_url(
            url, profile=profile, timeout_s=timeout_s, autocommit=False
        ),
        poolclass=pool.NullPool,
    )
    print(f"Connecting to host: {parse_url(url).hosts}", file=sys.stderr)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata, **COMPARE_OPTS)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
