"""Integration tests talk to the REAL databases from .env, so they switch off two guards from
tests/conftest.py (same fixture names override them here). They only run with
RUN_INTEGRATION=1 (CI compose-smoke); normal runs skip them."""

import os
from collections.abc import Iterator

import pytest
import structlog

from market_intel.config import get_settings
from market_intel.logging_setup import reset_logging

RUN_INTEGRATION = os.environ.get("RUN_INTEGRATION") == "1"


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    skip = pytest.mark.skip(reason="integration test: set RUN_INTEGRATION=1 to run")
    for item in items:
        if "integration" in str(item.path.parent.name):
            item.add_marker(pytest.mark.integration)
            if not RUN_INTEGRATION:
                item.add_marker(skip)


@pytest.fixture(autouse=True)
def no_real_databases() -> Iterator[None]:
    """Overrides the unit-test guard: these tests connect for real."""
    yield


@pytest.fixture(autouse=True)
def isolated_env() -> Iterator[None]:
    """Overrides the unit-test guard: use the real .env, with fresh settings and logging."""
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
    reset_logging()
    structlog.contextvars.clear_contextvars()
