"""Walk-forward evaluation: folds with purge and embargo, classification metrics, and the
after-cost portfolio test (sprint S3; decision 2026-11-24). Pure functions over DataFrames.

Folds: yearly test folds (2018 ... the year before the holdout, plus the part of the holdout's
year before it). For each fold the model trains on an expanding window of earlier rows:

- PURGE: a training row's label window (t, t+21] must end before the test fold starts.
- EMBARGO: rows in the 21 trading days after an earlier test fold's end don't train later folds.

HOLDOUT: the last 6 months of usable labels; evaluated once, at the end, reported separately.

Portfolio (the honest test): every 21 trading days (non-overlapping) pick the top K by score
(tie-break: a second score), equal weight, enter at open(t+1), exit at close(t+21), pay costs.
A stock that was already held and is picked again pays nothing; buys and sells pay
:mod:`market_intel.costs` charges plus liquidity slippage. Prices are raw (not adjusted), so a
period return is "unmeasurable" when the stock has no next-day open, a suspected split/bonus in
the window, or a raw move below -90% / above +300% (most likely an unrecorded corporate action).
Such a pick earns 0, for every strategy alike, and the report counts them.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta

import numpy as np
import pandas as pd

from market_intel.costs import DEFAULT_COSTS, CostModel

HORIZON = 21
PERIODS_PER_YEAR = 252 / HORIZON
HOLDOUT_MONTHS = 6
# A 21-day raw return outside this range is treated as unmeasurable (on raw prices it is most
# likely an unrecorded corporate action, e.g. a consolidation, not a real move).
UNMEASURABLE_BELOW, UNMEASURABLE_ABOVE = -0.9, 3.0
FIRST_TEST_YEAR = 2018


# --- folds -------------------------------------------------------------------------------


@dataclass(frozen=True)
class Fold:
    name: str
    test_start: date
    test_end: date  # inclusive


def holdout_start(usable_dates: Sequence[date], months: int = HOLDOUT_MONTHS) -> date:
    """The first usable label day of the last ``months`` months."""
    last = max(usable_dates)
    cutoff = (pd.Timestamp(last) - pd.DateOffset(months=months)).date()
    return min(d for d in usable_dates if d > cutoff)


def make_folds(
    usable_dates: Sequence[date], holdout_from: date, first_year: int = FIRST_TEST_YEAR
) -> list[Fold]:
    """Yearly test folds from ``first_year`` up to the day before the holdout."""
    dates = sorted(d for d in usable_dates if d < holdout_from)
    if not dates:
        return []
    folds = []
    for year in range(max(first_year, dates[0].year + 1), holdout_from.year + 1):
        start = date(year, 1, 1)
        end = min(date(year, 12, 31), holdout_from - timedelta(days=1))
        if any(start <= d <= end for d in dates):
            folds.append(Fold(str(year), start, end))
    return folds


def label_end(
    trade_dates: pd.Series, calendar: Sequence[date], horizon: int = HORIZON
) -> pd.Series:
    """The last day of each row's label window, calendar[pos(t) + horizon] (NaT past the end)."""
    pos = {d: i for i, d in enumerate(calendar)}
    out = []
    for d in trade_dates:
        p = pos.get(d)
        out.append(calendar[p + horizon] if p is not None and p + horizon < len(calendar) else None)
    return pd.Series(out, index=trade_dates.index, dtype=object)


def embargo_end(fold_end: date, calendar: Sequence[date], days: int = HORIZON) -> date:
    """The last day embargoed after a fold: ``days`` trading days after its end."""
    later = [d for d in calendar if d > fold_end]
    return later[min(days, len(later)) - 1] if later else fold_end


def train_mask(
    trade_dates: pd.Series,
    label_ends: pd.Series,
    test_start: date,
    earlier_folds: Sequence[Fold],
    calendar: Sequence[date],
    embargo_days: int = HORIZON,
) -> pd.Series:
    """Rows allowed to train for a test period starting at ``test_start``: purged (label
    window ends before it) and not embargoed after any earlier test fold."""
    ends = label_ends.map(lambda d: d is not None and d < test_start)
    mask = ends.astype(bool)
    for fold in earlier_folds:
        if fold.test_end >= test_start:
            continue
        stop = embargo_end(fold.test_end, calendar, embargo_days)
        mask &= ~((trade_dates > fold.test_end) & (trade_dates <= stop))
    return mask


# --- classification metrics --------------------------------------------------------------


def classification_metrics(y: Sequence[bool], prob: Sequence[float]) -> dict[str, float]:
    from sklearn.metrics import brier_score_loss, roc_auc_score

    y_arr = np.asarray(y, dtype=bool)
    p = np.asarray(prob, dtype=float)
    out = {"rows": float(len(y_arr)), "up_rate": float(y_arr.mean()) if len(y_arr) else math.nan}
    if not len(y_arr):
        return out | {
            "auc": math.nan,
            "accuracy": math.nan,
            "precision_up": math.nan,
            "brier": math.nan,
        }
    pred = p >= 0.5
    out["auc"] = float(roc_auc_score(y_arr, p)) if len(set(y_arr)) == 2 and len(set(p)) > 1 else 0.5
    out["accuracy"] = float((pred == y_arr).mean())
    out["precision_up"] = float(y_arr[pred].mean()) if pred.any() else math.nan
    out["brier"] = float(brier_score_loss(y_arr, np.clip(p, 0, 1)))
    return out


def calibration_table(y: Sequence[bool], prob: Sequence[float], bins: int = 10) -> list[dict]:
    """Ten equal-width probability bins: mean predicted probability vs the share that went up."""
    frame = pd.DataFrame({"y": np.asarray(y, dtype=float), "p": np.asarray(prob, dtype=float)})
    frame["bin"] = np.minimum((frame["p"] * bins).astype(int), bins - 1)
    out = []
    for b, g in frame.groupby("bin"):
        out.append(
            {
                "bin": f"{b / bins:.1f}-{(b + 1) / bins:.1f}",
                "mean_prob": float(g.p.mean()),
                "share_up": float(g.y.mean()),
                "rows": int(len(g)),
            }
        )
    return out


# --- returns for the portfolio test ------------------------------------------------------


def rebalance_dates(
    calendar: Sequence[date], start: date, last_exit: date, step: int = HORIZON
) -> list[date]:
    """Every ``step`` trading days from the first calendar day >= start, while the exit
    (t + step) is on or before ``last_exit``."""
    cal = list(calendar)
    first = next((i for i, d in enumerate(cal) if d >= start), None)
    if first is None:
        return []
    out = []
    i = first
    while i + step < len(cal) and cal[i + step] <= last_exit:
        out.append(cal[i])
        i += step
    return out


def period_returns(
    prices: dict[int, pd.DataFrame],
    calendar: Sequence[date],
    dates: Sequence[date],
    events: dict[int, list[tuple[date, float]]] | None = None,
    step: int = HORIZON,
) -> pd.DataFrame:
    """Return from open(t+1) to close(t+step) per (date, instrument) on raw prices. NaN
    ("unmeasurable") when there is no open on t+1, when a suspected split/bonus lies in
    (t, t+step], or when the raw return is below -90% or above +300%. In the portfolio test a
    pick with an unmeasurable return earns 0, for every strategy alike."""
    pos = {d: i for i, d in enumerate(calendar)}
    rows = []
    for inst, frame in prices.items():
        opens, closes = frame["open"], frame["close"]
        inst_events = sorted((events or {}).get(inst, []))
        for t in dates:
            p = pos.get(t)
            if p is None or p + step >= len(calendar):
                continue
            t1, th = calendar[p + 1], calendar[p + step]
            entry = opens.get(t1)
            if entry is None or not entry > 0:
                rows.append((t, inst, math.nan))
                continue
            window = closes[(closes.index > t) & (closes.index <= th)]
            if window.empty:
                rows.append((t, inst, math.nan))
                continue
            if any(t < d <= th for d, _ in inst_events):
                rows.append((t, inst, math.nan))  # a suspected split/bonus: unmeasurable
                continue
            ret = float(window.iloc[-1]) / float(entry) - 1
            if not UNMEASURABLE_BELOW <= ret <= UNMEASURABLE_ABOVE:
                ret = math.nan  # likely an unrecorded corporate action on raw prices
            rows.append((t, inst, ret))
    return pd.DataFrame(rows, columns=["date", "instrument_id", "ret"])


# --- portfolio simulation ----------------------------------------------------------------


@dataclass
class PortfolioResult:
    name: str
    periods: int = 0
    total_return: float = 0.0
    annual_return: float = 0.0
    sharpe: float = 0.0
    max_drawdown: float = 0.0
    hit_rate: float = 0.0
    turnover: float = 0.0
    costs_paid: float = 0.0  # share of the starting capital
    net_returns: list[float] = field(default_factory=list)

    def summary(self) -> dict[str, float]:
        out = asdict(self)
        out.pop("net_returns")
        return out


def _stats(
    name: str, net: list[float], hits: list[float], turnover: list[float], costs: float
) -> PortfolioResult:
    r = np.asarray(net, dtype=float)
    res = PortfolioResult(name=name, periods=len(r), net_returns=list(r))
    if not len(r):
        return res
    curve = np.cumprod(1 + r)
    res.total_return = float(curve[-1] - 1)
    years = len(r) / PERIODS_PER_YEAR
    res.annual_return = float(curve[-1] ** (1 / years) - 1) if curve[-1] > 0 else -1.0
    res.sharpe = (
        float(r.mean() / r.std(ddof=1) * math.sqrt(PERIODS_PER_YEAR))
        if len(r) > 1 and r.std(ddof=1) > 0
        else 0.0
    )
    peak = np.maximum.accumulate(np.concatenate([[1.0], curve]))
    res.max_drawdown = float((np.concatenate([[1.0], curve]) / peak - 1).min())
    res.hit_rate = float(np.mean(hits)) if hits else 0.0
    res.turnover = float(np.mean(turnover)) if turnover else 0.0
    res.costs_paid = float(costs)
    return res


def simulate(
    name: str,
    scores: pd.DataFrame,
    returns: pd.DataFrame,
    *,
    k: int | None,
    costs: CostModel = DEFAULT_COSTS,
    capital: float = 1_000_000.0,
    slippage: dict[tuple[date, int], float] | None = None,
) -> PortfolioResult:
    """Top-``k`` portfolio (``k`` None: every scored stock, equal weight). ``scores``: columns
    date, instrument_id, score, tiebreak. ``returns``: :func:`period_returns`. ``slippage``:
    bps per side for (date, instrument); default the worst bucket."""
    rets = returns.set_index(["date", "instrument_id"])["ret"]
    slip = slippage or {}
    equity, held = capital, set()
    net_returns, hits, turnover = [], [], []
    paid = 0.0
    for t, group in scores.sort_values("date").groupby("date", sort=True):
        group = group.dropna(subset=["score"])
        if group.empty:
            continue
        ranked = group.sort_values(["score", "tiebreak"], ascending=False, kind="mergesort")
        picks = list(ranked["instrument_id"] if k is None else ranked["instrument_id"].head(k))
        n = len(picks)
        value = equity / n
        buys = [i for i in picks if i not in held]
        sells = [i for i in held if i not in picks]
        cost = sum(costs.buy_cost(value, slip.get((t, i), costs.slippage_bps[2])) for i in buys)
        cost += sum(costs.sell_cost(value, slip.get((t, i), costs.slippage_bps[2])) for i in sells)
        period = [rets.get((t, i), math.nan) for i in picks]
        gross = float(np.mean([0.0 if math.isnan(x) else x for x in period]))
        hits += [1.0 if x > 0 else 0.0 for x in period if not math.isnan(x)]
        turnover.append(len(buys) / n)
        net = gross - cost / equity
        paid += cost / capital
        equity *= 1 + net
        net_returns.append(net)
        held = set(picks)
    if held and net_returns:  # sell everything at the end
        value = equity / len(held)
        final = sum(costs.sell_cost(value, costs.slippage_bps[0]) for _ in held)
        net_returns[-1] = (1 + net_returns[-1]) * (1 - final / equity) - 1
        paid += final / capital
    return _stats(name, net_returns, hits, turnover, paid)


def index_hold(
    name: str,
    index_returns: Sequence[float],
    *,
    costs: CostModel = DEFAULT_COSTS,
    capital: float = 1_000_000.0,
) -> PortfolioResult:
    """Buy the index once, hold, sell at the end (delivery charges + the best slippage bucket)."""
    r = [0.0 if math.isnan(x) else float(x) for x in index_returns]
    if not r:
        return PortfolioResult(name=name)
    buy = costs.buy_cost(capital, costs.slippage_bps[0]) / capital
    r[0] = (1 - buy) * (1 + r[0]) - 1
    end_value = capital * float(np.prod([1 + x for x in r]))
    sell = costs.sell_cost(end_value, costs.slippage_bps[0]) / end_value
    r[-1] = (1 + r[-1]) * (1 - sell) - 1
    return _stats(
        name,
        r,
        [1.0 if x > 0 else 0.0 for x in index_returns if not math.isnan(x)],
        [0.0] * len(r),
        buy + sell,
    )


def beats_baseline(
    model: PortfolioResult, equal_weight: PortfolioResult, others: Sequence[PortfolioResult]
) -> bool:
    """The model's after-cost annual return beats the equal-weight universe AND the best other
    baseline."""
    if model.periods == 0:
        return False
    best_other = max((o.annual_return for o in others if o.periods), default=-math.inf)
    return model.annual_return > equal_weight.annual_return and model.annual_return > best_other
