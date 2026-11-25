"""Event-driven intraday backtest after every cost (sprint S5i; decision 2026-11-25).

The bar-by-bar walk already happened: the triple-barrier labels (:mod:`market_intel.intraday`)
stepped through each minute after the decision bar, entered at ``open(t+1)``, counted the stop
first when one bar touched both barriers, and closed everything by the 15:20 bar. A label row
therefore carries exactly what a simulated trade would have done: the outcome, the gross return
and how many bars it was held.

This module adds everything a label cannot know, in time order through each day:

- **sequencing** — a position occupies the account until its own exit bar, so a signal that
  fires while a trade is open is skipped (not quietly taken in parallel);
- **sizing** — quantity from the owner's capital and risk per trade;
- **costs** — brokerage, STT, exchange, SEBI, stamp duty, GST and slippage on both sides;
- **the daily limits** — the loss limit and the maximum number of trades stop the day;
- **the expected-value gate** — with ``ev_gate`` the trade must be worth taking after costs
  (used for the model, whose probabilities make that computable). Rule baselines run without
  it, so they stay honest yardsticks rather than a filtered version of themselves.

Nothing here places an order, and nothing is ever promoted. "No trading" is one of the
baselines, and it wins more often than not.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import date
from typing import Any

import numpy as np
import pandas as pd

from market_intel.costs import DEFAULT_INTRADAY_COSTS, IntradayCostModel
from market_intel.sizing import DayState, OutcomeOdds, SizingPolicy, plan_intraday_trade

TRADING_DAYS = 252
EXIT_REASON = {1: "target", -1: "stop", 0: "time_limit"}


@dataclass
class Trade:
    """One simulated trade, after every charge."""

    instrument_id: int
    trade_date: date
    decided_at: pd.Timestamp
    side: str
    entry: float
    exit_price: float
    target: float
    stop: float
    quantity: int
    bars_held: int
    exit_reason: str
    gross_profit: float
    costs: float
    net_profit: float
    probability: float | None = None


@dataclass
class BacktestResult:
    name: str
    kind: str  # strategy, model or baseline
    label_version: str
    variant: str | None
    period: str  # walk_forward or holdout
    capital: float
    slippage_bps: float
    from_date: date | None = None
    to_date: date | None = None
    trades: list[Trade] = field(default_factory=list)
    days: int = 0
    days_without_trade: int = 0
    daily_profit: dict[date, float] = field(default_factory=dict)
    params: dict[str, Any] = field(default_factory=dict)
    skipped: dict[str, int] = field(default_factory=dict)
    ruined: bool = False

    # --- metrics ---------------------------------------------------------------------
    @property
    def net_profit(self) -> float:
        return sum(t.net_profit for t in self.trades)

    @property
    def costs_paid(self) -> float:
        return sum(t.costs for t in self.trades)

    @property
    def gross_profit(self) -> float:
        return sum(t.gross_profit for t in self.trades)

    @property
    def hit_rate(self) -> float | None:
        if not self.trades:
            return None
        return sum(1 for t in self.trades if t.net_profit > 0) / len(self.trades)

    @property
    def return_pct(self) -> float:
        return 100 * self.net_profit / self.capital

    @property
    def equity_curve(self) -> pd.Series:
        """Capital at the end of each day that the backtest covered."""
        if not self.daily_profit:
            return pd.Series(dtype=float)
        days = sorted(self.daily_profit)
        return pd.Series(self.capital + np.cumsum([self.daily_profit[d] for d in days]), index=days)

    @property
    def max_drawdown_pct(self) -> float:
        curve = self.equity_curve
        if curve.empty:
            return 0.0
        peak = curve.cummax()
        return float((-(curve - peak) / peak).max() * 100)

    @property
    def sharpe(self) -> float | None:
        """Annualised Sharpe of the daily returns (no risk-free rate; None if it is undefined)."""
        curve = self.equity_curve
        if len(curve) < 2:
            return None
        returns = curve.pct_change().dropna()
        if returns.empty or returns.std(ddof=1) == 0:
            return None
        return float(returns.mean() / returns.std(ddof=1) * math.sqrt(TRADING_DAYS))

    def row(self) -> dict[str, Any]:
        """The ``backtest_runs`` row for this result."""
        return {
            "kind": self.kind,
            "name": self.name,
            "label_version": self.label_version,
            "variant": self.variant,
            "period": self.period,
            "from_date": self.from_date,
            "to_date": self.to_date,
            "capital": round(self.capital, 2),
            "slippage_bps": self.slippage_bps,
            "trades": len(self.trades),
            "hit_rate": self.hit_rate,
            "net_profit": round(self.net_profit, 2),
            "costs_paid": round(self.costs_paid, 2),
            "return_pct": self.return_pct,
            "max_drawdown_pct": self.max_drawdown_pct,
            "sharpe": self.sharpe,
            "days": self.days,
            "days_without_trade": self.days_without_trade,
            "params": self.params,
        }

    def line(self) -> str:
        hit = f"{self.hit_rate:.0%}" if self.hit_rate is not None else "-"
        sharpe = f"{self.sharpe:+.2f}" if self.sharpe is not None else "-"
        return (
            f"{self.name:<22} {self.variant or self.label_version:<8} {len(self.trades):>7,}"
            f" {hit:>6} {self.net_profit:>13,.0f} {self.return_pct:>8.1f}%"
            f" {self.max_drawdown_pct:>8.1f}% {sharpe:>7}"
            f" {100 * self.days_without_trade / max(self.days, 1):>7.0f}%"
            + ("  RUINED" if self.ruined else "")
        )


HEADER = (
    f"{'strategy':<22} {'labels':<8} {'trades':>7} {'hit':>6} {'net Rs':>13} {'return':>9}"
    f" {'maxDD':>9} {'Sharpe':>7} {'no-trade':>8}"
)


def no_trading(
    capital: float,
    days: int,
    period: str = "walk_forward",
    *,
    from_date: date | None = None,
    to_date: date | None = None,
) -> BacktestResult:
    """The baseline that does nothing: no trades, no costs, no loss.

    It still records the period it covered, so it can be compared with the runs it is the
    yardstick for.
    """
    return BacktestResult(
        name="no_trading",
        kind="baseline",
        label_version="-",
        variant=None,
        period=period,
        capital=capital,
        slippage_bps=0.0,
        from_date=from_date,
        to_date=to_date,
        days=days,
        days_without_trade=days,
    )


def _label_columns(version: str, variant: str | None) -> tuple[str, str, str]:
    if version == "v2":
        if not variant:
            raise ValueError("a v2 backtest needs a barrier variant")
        return (f"outcome_{variant}", f"gross_return_{variant}", f"bars_held_{variant}")
    return ("outcome", "gross_return", "bars_held")


def prepare(
    features: pd.DataFrame,
    labels: pd.DataFrame,
    signal: pd.Series,
    *,
    label_version: str = "v1",
    variant: str | None = None,
    target_pct: float = 0.5,
    stop_pct: float = 0.25,
) -> pd.DataFrame:
    """Join each non-zero signal to the label row of the side it points at.

    Rows the labels excluded (special session, price event, thin day, no entry, no ATR) drop
    out here, so a strategy can never trade a day the labels refused to measure.
    """
    outcome_col, return_col, bars_col = _label_columns(label_version, variant)
    picked = features[["instrument_id", "trade_date", "ts", "minute"]].copy()
    picked["signal"] = np.asarray(signal)
    picked = picked[picked["signal"] != 0]
    if picked.empty:
        return picked.assign(side="", entry_price=np.nan)
    picked["side"] = np.where(picked["signal"] > 0, "long", "short")
    keep = ["instrument_id", "ts", "side", "entry_price", "excluded_reason",
            outcome_col, return_col, bars_col]  # fmt: skip
    if label_version == "v2":
        keep.append("atr")
    merged = picked.merge(labels[keep], on=["instrument_id", "ts", "side"], how="inner")
    merged = merged[merged["excluded_reason"].isna()]
    if label_version == "v1":
        merged["target_pct"] = target_pct
        merged["stop_pct"] = stop_pct
    return merged


def simulate(
    rows: pd.DataFrame,
    *,
    name: str,
    kind: str = "strategy",
    label_version: str = "v1",
    variant: str | None = None,
    policy: SizingPolicy,
    costs: IntradayCostModel = DEFAULT_INTRADAY_COSTS,
    slippage_bps: float = 10.0,
    period: str = "walk_forward",
    ev_gate: bool = False,
    odds_by_side: dict[str, OutcomeOdds] | None = None,
    max_open_positions: int = 1,
    compound: bool = True,
    ruin_fraction: float = 0.25,
    params: dict[str, Any] | None = None,
) -> BacktestResult:
    """Walk the decision points in time order and trade the ``signal`` column.

    ``rows`` needs: instrument_id, trade_date, ts, minute, side, signal (+1/-1/0 or the side's
    own flag), entry_price, the label columns for ``label_version``/``variant``, and optionally
    ``probability`` and ``symbol``. One position at a time by default, because the exposure cap
    puts the whole capital behind each trade (raising ``max_open_positions`` does NOT split the
    capital between them, so leave it at 1 unless that is what you mean).

    Note the interaction between the stop distance and the exposure cap: a 0.25% stop would
    need four times the capital as position value to risk a full 1%, so the cap binds and the
    trade actually risks about 0.25% of capital. Intraday (MIS) leverage is not modelled.

    With ``compound`` (the default) each day is sized from the equity the run has actually
    reached, not from the starting capital, so losses shrink the next day's risk as they would
    in a real account. Trading stops for good once equity falls below ``ruin_fraction`` of the
    starting capital: a real account would have been shut long before.
    """
    outcome_col, return_col, bars_col = _label_columns(label_version, variant)
    result = BacktestResult(
        name=name,
        kind=kind,
        label_version=label_version,
        variant=variant,
        period=period,
        capital=policy.capital,
        slippage_bps=slippage_bps,
        params=params or {},
    )
    if rows.empty:
        return result
    needed = {"instrument_id", "trade_date", "minute", "side", "signal", "entry_price"}
    missing = needed - set(rows.columns)
    if missing:
        raise KeyError(f"missing columns: {', '.join(sorted(missing))}")
    frame = rows.sort_values(["trade_date", "minute", "instrument_id"], kind="stable")
    result.from_date = frame["trade_date"].min()
    result.to_date = frame["trade_date"].max()
    skipped = result.skipped

    equity = policy.capital
    ruined = False
    for day, group in frame.groupby("trade_date", sort=True):
        result.days += 1
        if ruined:
            result.days_without_trade += 1
            result.daily_profit.setdefault(day, 0.0)
            continue
        day_policy = policy
        if compound and equity != policy.capital:
            day_policy = replace(policy, capital=equity)
        state = DayState()
        open_exits: list[int] = []  # the minutes at which held positions close
        took = 0
        for row in group.itertuples(index=False):
            if int(row.signal) == 0:
                continue
            open_exits = [m for m in open_exits if m > row.minute]
            if len(open_exits) >= max_open_positions:
                skipped["a trade was already open"] = skipped.get("a trade was already open", 0) + 1
                continue
            if (why := state.blocked(day_policy)) is not None:
                skipped[why] = skipped.get(why, 0) + 1
                break  # the day is over for this strategy
            outcome = getattr(row, outcome_col)
            gross_return = getattr(row, return_col)
            bars_held = getattr(row, bars_col)
            if pd.isna(outcome) or pd.isna(gross_return) or pd.isna(bars_held):
                skipped["no label"] = skipped.get("no label", 0) + 1
                continue
            side = "long" if int(row.signal) > 0 else "short"
            entry = float(row.entry_price)
            probability = getattr(row, "probability", None)
            odds = (odds_by_side or {}).get(side)
            if probability is not None and not pd.isna(probability):
                odds = (
                    odds.with_target(float(probability))
                    if odds is not None
                    else OutcomeOdds(target=float(probability))
                )
            plan = plan_intraday_trade(
                str(getattr(row, "symbol", row.instrument_id)),
                side,
                entry,
                target_pct=abs(_target_pct(row, outcome_col, return_col, label_version, variant)),
                stop_pct=abs(_stop_pct(row, label_version, variant)),
                odds=odds or OutcomeOdds(target=0.5),
                policy=day_policy,
                day=state,
                costs=costs,
                slippage_bps=slippage_bps,
                require_positive_ev=ev_gate,
            )
            if not plan.taken:
                reason = plan.reason or "refused"
                skipped[reason] = skipped.get(reason, 0) + 1
                continue
            quantity = plan.quantity
            ret = float(gross_return)
            exit_price = entry * (1 + ret) if side == "long" else entry * (1 - ret)
            gross = quantity * entry * ret
            cost = costs.round_trip(entry * quantity, exit_price * quantity, slippage_bps)
            net = gross - cost
            result.trades.append(
                Trade(
                    instrument_id=int(row.instrument_id),
                    trade_date=day,
                    decided_at=getattr(row, "ts", pd.NaT),
                    side=side,
                    entry=entry,
                    exit_price=exit_price,
                    target=plan.target,
                    stop=plan.stop,
                    quantity=quantity,
                    bars_held=int(bars_held),
                    exit_reason=EXIT_REASON.get(int(outcome), "session_end"),
                    gross_profit=gross,
                    costs=cost,
                    net_profit=net,
                    probability=None
                    if probability is None or pd.isna(probability)
                    else float(probability),
                )
            )
            state.record(net)
            result.daily_profit[day] = result.daily_profit.get(day, 0.0) + net
            open_exits.append(int(row.minute) + int(bars_held))
            took += 1
        equity += result.daily_profit.get(day, 0.0)
        if not took:
            result.days_without_trade += 1
            result.daily_profit.setdefault(day, 0.0)
        if equity <= policy.capital * ruin_fraction:
            ruined = True
            skipped["ruined"] = skipped.get("ruined", 0) + 1
    result.ruined = ruined
    return result


def _target_pct(
    row: Any, outcome_col: str, return_col: str, version: str, variant: str | None
) -> float:
    """The target distance in percent for this row's barriers."""
    if version == "v2":
        k = float(variant.split("_")[0][1:]) / 10 if variant else 1.0
        return 100 * k * float(row.atr) / float(row.entry_price)
    return float(getattr(row, "target_pct", 0.5))


def _stop_pct(row: Any, version: str, variant: str | None) -> float:
    if version == "v2":
        k = float(variant.split("_")[0][1:]) / 10 if variant else 1.0
        return 100 * (k / 2) * float(row.atr) / float(row.entry_price)
    return float(getattr(row, "stop_pct", 0.25))


# --- storage -------------------------------------------------------------------------------


def save(conn: Any, result: BacktestResult, *, with_trades: bool = True) -> int:
    """Store a result and its trades; returns the run id."""
    import json

    row = result.row()
    if row["from_date"] is None or row["to_date"] is None:
        raise ValueError(
            f"backtest {result.name!r} has no date range: it cannot be stored or compared"
        )
    run_id = conn.execute(
        "INSERT INTO backtest_runs (kind, name, label_version, variant, period, from_date,"
        " to_date, capital, slippage_bps, trades, hit_rate, net_profit, costs_paid, return_pct,"
        " max_drawdown_pct, sharpe, days, days_without_trade, params)"
        " VALUES (%(kind)s, %(name)s, %(label_version)s, %(variant)s, %(period)s, %(from_date)s,"
        " %(to_date)s, %(capital)s, %(slippage_bps)s, %(trades)s, %(hit_rate)s, %(net_profit)s,"
        " %(costs_paid)s, %(return_pct)s, %(max_drawdown_pct)s, %(sharpe)s, %(days)s,"
        " %(days_without_trade)s, %(params)s::jsonb) RETURNING id",
        {**row, "params": json.dumps(row["params"])},
    ).fetchone()[0]
    if with_trades and result.trades:
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO backtest_trades (run_id, instrument_id, trade_date, decided_at,"
                " side, entry, exit_price, target, stop, quantity, bars_held, exit_reason,"
                " gross_profit, costs, net_profit, probability)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                [
                    (
                        run_id,
                        t.instrument_id,
                        t.trade_date,
                        t.decided_at,
                        t.side,
                        round(t.entry, 4),
                        round(t.exit_price, 4),
                        round(t.target, 4),
                        round(t.stop, 4),
                        t.quantity,
                        t.bars_held,
                        t.exit_reason,
                        round(t.gross_profit, 2),
                        round(t.costs, 2),
                        round(t.net_profit, 2),
                        t.probability,
                    )
                    for t in result.trades
                ],  # fmt: skip
            )
    return run_id


def report_table(results: Sequence[BacktestResult]) -> list[str]:
    """The comparison table: every strategy and model next to every baseline."""
    ordered = sorted(results, key=lambda r: (-r.net_profit, r.name))
    return [HEADER, "-" * len(HEADER), *[r.line() for r in ordered]]
