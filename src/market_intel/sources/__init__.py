"""Data sources: one interface (``DataSource``), typed records tagged with their source.

Real sources (Upstox, yfinance, RSS) arrive in later Phase 1 tasks; ``FakeSource`` is for tests.
"""

from market_intel.sources.base import KINDS, DataSource, NotSupportedError
from market_intel.sources.records import (
    FAKE_TAG,
    DailyCandle,
    Instrument,
    IntradayCandle,
    NewsRecord,
    Record,
    check_tag,
)

__all__ = [
    "FAKE_TAG",
    "KINDS",
    "DailyCandle",
    "DataSource",
    "Instrument",
    "IntradayCandle",
    "NewsRecord",
    "NotSupportedError",
    "Record",
    "check_tag",
]
