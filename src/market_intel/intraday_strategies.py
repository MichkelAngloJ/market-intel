"""Intraday rule strategies — the baselines the ML meta-model has to beat (sprint S5i;
decision 2026-11-25).

Each strategy reads the features v1 of a decision row and answers one question: long, short or
nothing. They are deliberately simple and well known, because their job is to be a fair
yardstick: a model that cannot beat an opening-range breakout after costs is not worth running.

Every rule is vectorised over a features frame (one row per instrument, timestamp and decision
minute) and returns a Series of +1 (long), -1 (short) or 0 (nothing). Two filters apply to all
of them:

- the **no-trade filter** removes rows the data says not to touch at all: a price event on the
  previous day, a thin session so far, and a missing opening range;
- the **market-mood filter** removes rows whose side fights the market: India VIX above a
  ceiling (panic), or the Nifty moving hard against the side since the open.

Thresholds are parameters of :class:`RuleSettings`, never hidden constants, so the backtest can
show how much each one matters. Nothing here sizes or places anything: the backtest turns a
signal into a sized plan through :mod:`market_intel.sizing`.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import numpy as np
import pandas as pd

LONG, SHORT, FLAT = 1, -1, 0


@dataclass(frozen=True)
class RuleSettings:
    """Every threshold the rules use. Defaults are round numbers, not fitted values."""

    vwap_far_pct: float = 0.3  # "far" from VWAP, for mean reversion
    vwap_near_pct: float = 0.05  # "beyond" VWAP, for the trend rule
    rs_open_pct: float = 0.3  # relative strength since the open, in percent
    gap_bucket: int = 1  # |gap bucket| at or above this counts as a gap
    volume_burst: float = 3.0  # bar volume vs the time-of-day normal
    momentum_pct: float = 0.05  # the 5-minute move that confirms a burst
    vix_ceiling: float = 30.0  # above this, stay out
    market_against_pct: float = 0.75  # Nifty move since the open that vetoes the other side
    require_opening_range: bool = True


DEFAULT_RULES = RuleSettings()


def _pct(series: pd.Series) -> pd.Series:
    """A log return or ratio feature as a percentage."""
    return series.astype(float) * 100


def no_trade_mask(features: pd.DataFrame, settings: RuleSettings = DEFAULT_RULES) -> pd.Series:
    """True where no strategy may trade at all."""
    out = pd.Series(False, index=features.index)
    for col in ("price_event_prev_day", "thin_so_far"):
        if col in features:
            out |= features[col].fillna(0).astype(float) > 0
    if settings.require_opening_range and "or15_pos" in features:
        out |= features["or15_pos"].isna()  # before the opening range completes
    return out


def mood_veto(
    signal: pd.Series, features: pd.DataFrame, settings: RuleSettings = DEFAULT_RULES
) -> pd.Series:
    """Zero the signals the market mood is against."""
    out = signal.copy()
    if "vix_level" in features:
        panic = features["vix_level"].astype(float) > settings.vix_ceiling
        out = out.where(~panic.fillna(False), FLAT)
    if "mkt_ret_open" in features:
        market = _pct(features["mkt_ret_open"]).fillna(0.0)
        against_long = market <= -settings.market_against_pct
        against_short = market >= settings.market_against_pct
        out = out.where(~((out == LONG) & against_long), FLAT)
        out = out.where(~((out == SHORT) & against_short), FLAT)
    return out


def _crossing(features: pd.DataFrame, column: str) -> pd.Series:
    """A state flag's first True of the day, per instrument (the decision rows are 5 minutes
    apart, so this is 'it just became true')."""
    flag = features[column].fillna(0).astype(float) > 0
    keys = (
        [features["instrument_id"], features["trade_date"]]
        if "instrument_id" in features
        else [features["trade_date"]]
    )
    previous = flag.groupby(keys).shift(1).fillna(False).astype(bool)
    return flag & ~previous


# --- the rules ----------------------------------------------------------------------------


def orb_15(features: pd.DataFrame, settings: RuleSettings = DEFAULT_RULES) -> pd.Series:
    """Opening-range breakout: the price leaves the first 15 minutes' range."""
    up, down = _crossing(features, "or15_up"), _crossing(features, "or15_down")
    return pd.Series(np.select([up, down], [LONG, SHORT], FLAT), index=features.index)


def orb_30(features: pd.DataFrame, settings: RuleSettings = DEFAULT_RULES) -> pd.Series:
    """The same with the first 30 minutes' range."""
    up, down = _crossing(features, "or30_up"), _crossing(features, "or30_down")
    return pd.Series(np.select([up, down], [LONG, SHORT], FLAT), index=features.index)


def vwap_trend(features: pd.DataFrame, settings: RuleSettings = DEFAULT_RULES) -> pd.Series:
    """Trade with the session VWAP: above it and rising is long, below and falling is short."""
    dist, slope = _pct(features["vwap_dist"]), _pct(features["vwap_slope_15"])
    long = (dist > settings.vwap_near_pct) & (slope > 0)
    short = (dist < -settings.vwap_near_pct) & (slope < 0)
    return pd.Series(
        np.select([long.fillna(False), short.fillna(False)], [LONG, SHORT], FLAT),
        index=features.index,
    )


def vwap_reversion(features: pd.DataFrame, settings: RuleSettings = DEFAULT_RULES) -> pd.Series:
    """Fade a stretch away from VWAP: far below is long, far above is short."""
    dist = _pct(features["vwap_dist"])
    long = dist < -settings.vwap_far_pct
    short = dist > settings.vwap_far_pct
    return pd.Series(
        np.select([long.fillna(False), short.fillna(False)], [LONG, SHORT], FLAT),
        index=features.index,
    )


def gap_and_go(features: pd.DataFrame, settings: RuleSettings = DEFAULT_RULES) -> pd.Series:
    """A gap that keeps going: gap up and still up since the open is long."""
    bucket = features["gap_bucket"].fillna(0).astype(float)
    since_open = _pct(features["ret_open"]).fillna(0.0)
    long = (bucket >= settings.gap_bucket) & (since_open > 0)
    short = (bucket <= -settings.gap_bucket) & (since_open < 0)
    return pd.Series(np.select([long, short], [LONG, SHORT], FLAT), index=features.index)


def gap_fill(features: pd.DataFrame, settings: RuleSettings = DEFAULT_RULES) -> pd.Series:
    """The opposite bet: a gap that is already fading closes the rest of the way."""
    bucket = features["gap_bucket"].fillna(0).astype(float)
    since_open = _pct(features["ret_open"]).fillna(0.0)
    short = (bucket >= settings.gap_bucket) & (since_open < 0)
    long = (bucket <= -settings.gap_bucket) & (since_open > 0)
    return pd.Series(np.select([long, short], [LONG, SHORT], FLAT), index=features.index)


def volume_burst(features: pd.DataFrame, settings: RuleSettings = DEFAULT_RULES) -> pd.Series:
    """A burst of volume in the direction of the last five minutes."""
    burst = features["vol_rel_tod"].astype(float) >= settings.volume_burst
    move = _pct(features["ret_5m"])
    long = burst & (move > settings.momentum_pct)
    short = burst & (move < -settings.momentum_pct)
    return pd.Series(
        np.select([long.fillna(False), short.fillna(False)], [LONG, SHORT], FLAT),
        index=features.index,
    )


def relative_strength(features: pd.DataFrame, settings: RuleSettings = DEFAULT_RULES) -> pd.Series:
    """Buy what is beating both the Nifty and its own sector since the open, and sell what is
    losing to both. Stocks without a sector index fall back to the Nifty alone."""
    nifty = _pct(features["rs_nifty_open"])
    sector = _pct(features["rs_sector_open"])
    sector_long = (sector > 0) | sector.isna()
    sector_short = (sector < 0) | sector.isna()
    long = (nifty > settings.rs_open_pct) & sector_long
    short = (nifty < -settings.rs_open_pct) & sector_short
    return pd.Series(
        np.select([long.fillna(False), short.fillna(False)], [LONG, SHORT], FLAT),
        index=features.index,
    )


RuleFn = Callable[[pd.DataFrame, RuleSettings], "pd.Series"]


@dataclass(frozen=True)
class Strategy:
    name: str
    rule: RuleFn
    description: str

    def signals(
        self,
        features: pd.DataFrame,
        settings: RuleSettings = DEFAULT_RULES,
        *,
        filters: bool = True,
    ) -> pd.Series:
        """+1 / -1 / 0 per row, after the no-trade and mood filters."""
        signal = self.rule(features, settings).astype("int8")
        if not filters:
            return signal
        signal = signal.where(~no_trade_mask(features, settings), FLAT)
        return mood_veto(signal, features, settings).astype("int8")


STRATEGIES: tuple[Strategy, ...] = (
    Strategy("orb_15", orb_15, "breaks out of the first 15 minutes' range"),
    Strategy("orb_30", orb_30, "breaks out of the first 30 minutes' range"),
    Strategy("vwap_trend", vwap_trend, "trades with the session VWAP"),
    Strategy("vwap_reversion", vwap_reversion, "fades a stretch away from VWAP"),
    Strategy("gap_and_go", gap_and_go, "follows an overnight gap that is still going"),
    Strategy("gap_fill", gap_fill, "fades an overnight gap that is already closing"),
    Strategy("volume_burst", volume_burst, "follows a burst of volume"),
    Strategy("relative_strength", relative_strength, "buys relative strength, sells weakness"),
)
STRATEGY_BY_NAME = {s.name: s for s in STRATEGIES}


def all_signals(features: pd.DataFrame, settings: RuleSettings = DEFAULT_RULES) -> pd.DataFrame:
    """One column per strategy, aligned to ``features``."""
    return pd.DataFrame(
        {s.name: s.signals(features, settings) for s in STRATEGIES}, index=features.index
    )
