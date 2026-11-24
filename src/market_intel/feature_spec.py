"""The feature and label column lists (S2). Shared by the schema (``features_daily``,
``labels_daily``), the builder and the leakage test, so the three can't drift apart.

Every feature for trading day t uses only data available at prediction_time(t) (see
:mod:`market_intel.features`). Labels live in a separate table and are never an input.
"""

from __future__ import annotations

from dataclasses import dataclass

FEATURE_VERSION = "v1"


@dataclass(frozen=True)
class FeatureSpec:
    name: str
    group: str
    description: str
    for_training: bool = True  # False: stored, but S3 must not train on it yet


FEATURES_V1: tuple[FeatureSpec, ...] = (
    FeatureSpec("ret_1d", "returns", "log return over 1 day"),
    FeatureSpec("ret_5d", "returns", "log return over 5 days"),
    FeatureSpec("ret_20d", "returns", "log return over 20 days"),
    FeatureSpec("ret_60d", "returns", "log return over 60 days"),
    FeatureSpec("ret_120d", "returns", "log return over 120 days"),
    FeatureSpec("vol_20d", "volatility", "std of daily log returns over 20 days"),
    FeatureSpec("vol_60d", "volatility", "std of daily log returns over 60 days"),
    FeatureSpec("atr14_pct", "volatility", "14-day average true range / close"),
    FeatureSpec("close_sma20", "trend", "close / SMA20 - 1"),
    FeatureSpec("close_sma50", "trend", "close / SMA50 - 1"),
    FeatureSpec("close_sma200", "trend", "close / SMA200 - 1"),
    FeatureSpec("sma20_sma50", "trend", "SMA20 / SMA50 - 1"),
    FeatureSpec("rsi14", "momentum", "RSI over 14 days (Wilder smoothing), 0..100"),
    FeatureSpec("volume_z20", "volume", "z-score of log(1 + volume) vs the last 20 days"),
    FeatureSpec("log_traded_value_20d", "volume", "log(1 + 20-day mean of close x volume)"),
    FeatureSpec("high_52w", "52 weeks", "close / 252-day max high - 1"),
    FeatureSpec("low_52w", "52 weeks", "close / 252-day min low - 1"),
    FeatureSpec("gap_open", "gap", "open / previous close - 1"),
    FeatureSpec("rs_nifty_20d", "relative strength", "ret_20d minus Nifty 50 ret_20d"),
    FeatureSpec(
        "rs_sector_20d", "relative strength", "ret_20d minus the sector's equal-weight ret_20d"
    ),
    FeatureSpec("mkt_ret_5d", "market", "Nifty 50 log return over 5 days"),
    FeatureSpec("mkt_ret_20d", "market", "Nifty 50 log return over 20 days"),
    FeatureSpec("mkt_close_sma200", "market", "Nifty 50 close / SMA200 - 1"),
    FeatureSpec("vix_level", "market", "India VIX close"),
    FeatureSpec("vix_chg_5d", "market", "India VIX / VIX 5 days ago - 1"),
    FeatureSpec("day_of_week", "calendar", "0 = Monday .. 4 = Friday"),
    FeatureSpec("month", "calendar", "1 .. 12"),
    FeatureSpec("history_days", "flags", "regular-session candles up to and including t"),
    FeatureSpec(
        "price_event_5d", "flags", "1 if a suspected split/bonus in the last 5 days, else 0"
    ),
    FeatureSpec(
        "news_exchange_1d", "news", "exchange items since the previous prediction time", False
    ),
    FeatureSpec(
        "news_exchange_5d", "news", "exchange items over the last 5 prediction windows", False
    ),
    FeatureSpec(
        "news_company_5d", "news", "company news over the last 5 prediction windows", False
    ),
)

FEATURE_NAMES: tuple[str, ...] = tuple(f.name for f in FEATURES_V1)
TRAINING_FEATURES: tuple[str, ...] = tuple(f.name for f in FEATURES_V1 if f.for_training)
NOT_FOR_TRAINING: tuple[str, ...] = tuple(f.name for f in FEATURES_V1 if not f.for_training)

HORIZONS: tuple[int, ...] = (21, 63, 126)
LABEL_COLUMNS: tuple[str, ...] = ("horizon_days", "fwd_return", "up", "excluded_reason")
EXCLUDED_REASONS: tuple[str, ...] = ("beyond_data", "special_session", "price_event", "gap")
# Words a feature name must never contain (the leakage test checks this too).
FORBIDDEN_FEATURE_WORDS: tuple[str, ...] = ("fwd", "label", "future", "target", "horizon")
