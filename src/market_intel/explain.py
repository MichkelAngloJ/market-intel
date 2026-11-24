"""SHAP explanations (sprint S3; decision 2026-11-24).

- :func:`global_importance`: mean |SHAP value| per feature (TreeExplainer) for one model.
- :func:`top_reasons`: the k features that pushed one prediction most, as plain sentences
  from tested templates, e.g. "Price is 12% above its 50-day average (raises the model's
  up-probability)".

SHAP explains the MODEL, not the market: a reason says why the model thinks so, not that it is
true. The sentences never recommend anything.
"""

from __future__ import annotations

import math
import warnings
from collections.abc import Callable, Sequence
from typing import Any

import numpy as np
import pandas as pd


def _pct(v: float) -> str:
    return f"{abs(v) * 100:.0f}%"


def _log_pct(v: float) -> str:
    return f"{abs(math.expm1(v)) * 100:.0f}%"


def _above(v: float) -> str:
    return "above" if v >= 0 else "below"


def _rose(v: float) -> str:
    return "rose" if v >= 0 else "fell"


def _ret(days: int) -> Callable[[float], str]:
    return lambda v: f"The stock {_rose(v)} {_log_pct(v)} over the last {days} days"


DOW = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
MONTHS = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)

TEMPLATES: dict[str, Callable[[float], str]] = {
    "ret_1d": lambda v: f"The stock {_rose(v)} {_log_pct(v)} on the last day",
    "ret_5d": _ret(5),
    "ret_20d": _ret(20),
    "ret_60d": _ret(60),
    "ret_120d": _ret(120),
    "vol_20d": lambda v: f"Daily moves over 20 days vary by about {v * 100:.1f}% (volatility)",
    "vol_60d": lambda v: f"Daily moves over 60 days vary by about {v * 100:.1f}% (volatility)",
    "atr14_pct": lambda v: f"The average daily range is {v * 100:.1f}% of the price",
    "close_sma20": lambda v: f"Price is {_pct(v)} {_above(v)} its 20-day average",
    "close_sma50": lambda v: f"Price is {_pct(v)} {_above(v)} its 50-day average",
    "close_sma200": lambda v: f"Price is {_pct(v)} {_above(v)} its 200-day average",
    "sma20_sma50": lambda v: f"The 20-day average is {_pct(v)} {_above(v)} the 50-day average",
    "rsi14": lambda v: (
        f"RSI(14) is {v:.0f}"
        + (" (overbought zone)" if v >= 70 else " (oversold zone)" if v <= 30 else "")
    ),
    "volume_z20": lambda v: (
        f"Volume is {abs(v):.1f} standard deviations {_above(v)} its 20-day normal"
    ),
    "log_traded_value_20d": lambda v: (
        f"Average daily traded value is about Rs {math.expm1(v) / 1e7:,.1f} crore"
    ),
    "high_52w": lambda v: (
        f"Price is {_pct(v)} below its 52-week high" if v < 0 else "Price is at its 52-week high"
    ),
    "low_52w": lambda v: f"Price is {_pct(v)} above its 52-week low",
    "gap_open": lambda v: f"The last day opened {_pct(v)} {_above(v)} the previous close",
    "rs_nifty_20d": lambda v: (
        f"Over 20 days the stock did {_pct(v)} {'better' if v >= 0 else 'worse'} than Nifty 50"
    ),
    "rs_sector_20d": lambda v: (
        f"Over 20 days the stock did {_pct(v)} {'better' if v >= 0 else 'worse'} than its sector"
    ),
    "mkt_ret_5d": lambda v: f"Nifty 50 {_rose(v)} {_log_pct(v)} over 5 days",
    "mkt_ret_20d": lambda v: f"Nifty 50 {_rose(v)} {_log_pct(v)} over 20 days",
    "mkt_close_sma200": lambda v: f"Nifty 50 is {_pct(v)} {_above(v)} its 200-day average",
    "vix_level": lambda v: f"India VIX is at {v:.1f}",
    "vix_chg_5d": lambda v: f"India VIX {_rose(v)} {_pct(v)} over 5 days",
    "day_of_week": lambda v: f"The day is a {DOW[int(v)] if 0 <= v < 7 else 'weekday'}",
    "month": lambda v: f"The month is {MONTHS[int(v) - 1] if 1 <= v <= 12 else 'unknown'}",
    "history_days": lambda v: f"The stock has {int(v)} trading days of history",
    "price_event_5d": lambda v: (
        "A suspected split or bonus happened in the last 5 days"
        if v
        else "No split or bonus in the last 5 days"
    ),
    "sector": lambda v: "Its sector",
}


def describe(feature: str, value: Any) -> str:
    """The sentence for one feature value (a generic fallback for unknown features)."""
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return f"{feature} is unknown"
    template = TEMPLATES.get(feature)
    if template is None:
        return f"{feature} is {value}"
    if feature == "sector":
        return f"Its sector is {value}"
    return template(float(value))


def _shap_matrix(model: Any, X: pd.DataFrame) -> np.ndarray:
    import shap

    with warnings.catch_warnings():
        # SHAP notes that LightGBM binary output is a list per class; handled just below
        warnings.filterwarnings("ignore", message=".*shap values output has changed.*")
        values = shap.TreeExplainer(model).shap_values(X)
    if isinstance(values, list):  # older SHAP: one array per class
        values = values[-1]
    values = np.asarray(values)
    if values.ndim == 3:  # (rows, features, classes)
        values = values[..., -1]
    return values


def global_importance(model: Any, X: pd.DataFrame) -> pd.DataFrame:
    """Mean |SHAP| per feature, highest first."""
    values = _shap_matrix(model, X)
    imp = pd.DataFrame({"feature": list(X.columns), "mean_abs_shap": np.abs(values).mean(axis=0)})
    return imp.sort_values("mean_abs_shap", ascending=False, ignore_index=True)


def reasons_from_values(row: pd.Series, contributions: Sequence[float], k: int = 5) -> list[str]:
    """The k features with the largest |contribution|, as sentences with their direction."""
    order = np.argsort(-np.abs(np.asarray(contributions, dtype=float)))[:k]
    out = []
    for i in order:
        feature, c = row.index[i], float(contributions[i])
        effect = "raises" if c > 0 else "lowers"
        out.append(f"{describe(feature, row.iloc[i])} ({effect} the model's up-probability)")
    return out


def top_reasons(model: Any, row: pd.Series | pd.DataFrame, k: int = 5) -> list[str]:
    """Plain-language top-k reasons for ONE prediction. ``row``: a one-row slice of the model's
    input frame (keeps the column types), or a Series of numbers."""
    frame = row.iloc[[0]] if isinstance(row, pd.DataFrame) else row.to_frame().T.astype(float)
    values = _shap_matrix(model, frame)[0]
    return reasons_from_values(frame.iloc[0], values, k)
