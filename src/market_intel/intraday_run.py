"""What the intraday commands actually do: backtest, train, report and replay (sprint S5i).

The modules below hold the thinking; this one wires them to the data and prints the result:

- :mod:`market_intel.intraday` — features, labels and the Parquet store
- :mod:`market_intel.intraday_strategies` — the rule baselines
- :mod:`market_intel.intraday_backtest` — the event-driven simulation and its tables
- :mod:`market_intel.intraday_models` — the LightGBM meta-model
- :mod:`market_intel.sizing` and :mod:`market_intel.costs` — capital and charges

Nothing here places an order. ``no_trading`` is always in the comparison, and it is allowed to
win.
"""

from __future__ import annotations

import time as time_mod
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import pandas as pd

from market_intel import intraday as I  # noqa: N812  (the module is the namespace here)
from market_intel import intraday_backtest as B
from market_intel import intraday_models as M
from market_intel import intraday_strategies as S
from market_intel.config import ConfigError, Settings
from market_intel.costs import DEFAULT_INTRADAY_COSTS
from market_intel.logging_setup import get_logger
from market_intel.sizing import DayState, OutcomeOdds, SizingPolicy, plan_intraday_trade

log = get_logger("market_intel.intraday_run")

DEFAULT_CAPITAL = 100_000.0
SLIPPAGE_GRID = (0.0, 2.0, 5.0, 10.0)  # bps per side, reported side by side
HEADLINE_SLIPPAGE = 5.0


@dataclass
class BacktestReport:
    results: list[B.BacktestResult] = field(default_factory=list)
    seconds: float = 0.0
    sensitivity: dict[float, dict[str, float]] = field(default_factory=dict)
    note: str = ""

    def lines(self) -> list[str]:
        out = [*B.report_table(self.results), ""]
        if self.sensitivity:
            out.append(
                "by slippage per side (best per-trade edge of each run; the account return"
                " saturates once a run is ruined):"
            )
            out.append(
                f"  {'slippage':<12} {'best strategy':<22} {'edge bps':>9} {'net Rs':>12}"
                f" {'trades':>8}"
            )
            for bps in sorted(self.sensitivity):
                row = self.sensitivity[bps]
                out.append(
                    f"  {bps:>5.0f} bps    {row['name']:<22} {row['edge_bps']:>+9.2f}"
                    f" {row['net_profit']:>12,.0f} {int(row['trades']):>8,}"
                )
            out.append("")
        if self.note:
            out.append(self.note)
        out.append(f"duration {self.seconds:.0f} s")
        return out


def _policy(capital: float, **kw: Any) -> SizingPolicy:
    return SizingPolicy(capital=capital, **kw)


def _load(
    settings: Settings, label_version: str, start: date | None, end: date | None
) -> tuple[pd.DataFrame, pd.DataFrame]:
    features = I.load_built(settings, "intraday_features", I.VERSION, start=start, end=end)
    labels = I.load_built(settings, "intraday_labels", label_version, start=start, end=end)
    return features, labels


def run_backtest(
    settings: Settings,
    conn: Any,
    *,
    capital: float = DEFAULT_CAPITAL,
    label_version: str = "v2",
    variant: str = "k15_b10",
    start: date | None = None,
    end: date | None = None,
    slippage_grid: Sequence[float] = SLIPPAGE_GRID,
    headline_slippage: float = HEADLINE_SLIPPAGE,
    save: bool = True,
    period: str = "walk_forward",
    progress: Any = lambda message: None,
) -> BacktestReport:
    """Every rule strategy against every baseline, after costs, at several slippage levels."""
    t0 = time_mod.monotonic()
    progress("loading features and labels")
    features, labels = _load(settings, label_version, start, end)
    signals = S.all_signals(features)
    report = BacktestReport()
    days = features["trade_date"].nunique()
    # the join does not depend on the slippage, so do it once per strategy
    prepared: dict[str, pd.DataFrame] = {}
    for strategy in S.STRATEGIES:
        progress(f"joining {strategy.name}")
        rows = B.prepare(
            features,
            labels,
            signals[strategy.name],
            label_version=label_version,
            variant=variant if label_version == "v2" else None,
            target_pct=settings.intraday_target_pct,
            stop_pct=settings.intraday_stop_pct,
        )
        if not rows.empty:
            prepared[strategy.name] = rows
    for bps in sorted({*slippage_grid, headline_slippage}):
        best: B.BacktestResult | None = None
        for strategy in S.STRATEGIES:
            rows = prepared.get(strategy.name)
            if rows is None:
                continue
            progress(f"{strategy.name} at {bps:g} bps ({len(rows):,} signals)")
            result = B.simulate(
                rows,
                name=strategy.name,
                kind="strategy",
                label_version=label_version,
                variant=variant if label_version == "v2" else None,
                policy=_policy(capital),
                costs=DEFAULT_INTRADAY_COSTS,
                slippage_bps=bps,
                period=period,
                params={"rule": strategy.description, "slippage_bps": bps},
            )
            if bps == headline_slippage:
                report.results.append(result)
                if save:
                    B.save(conn, result)
            if best is None or (result.edge_bps or -1e9) > (best.edge_bps or -1e9):
                best = result
        if best is not None:
            report.sensitivity[bps] = {
                "name": best.name,
                "net_profit": best.net_profit,
                "trades": len(best.trades),
                "edge_bps": best.edge_bps or 0.0,
            }
    nothing = B.no_trading(
        capital,
        days,
        period=period,
        from_date=features["trade_date"].min(),
        to_date=features["trade_date"].max(),
    )
    report.results.append(nothing)
    if save:
        B.save(conn, nothing)
    report.seconds = time_mod.monotonic() - t0
    report.note = (
        f"Headline rows use {headline_slippage:g} bps of slippage per side; charges alone cost"
        f" {DEFAULT_INTRADAY_COSTS.breakeven_fraction(capital) * 100:.3f}% of a position per"
        " round trip."
    )
    log.info(
        "intraday_backtest_done",
        strategies=len(report.results),
        days=days,
        label_version=label_version,
        variant=variant,
    )
    return report


def run_train(
    settings: Settings,
    conn: Any,
    *,
    capital: float = DEFAULT_CAPITAL,
    label_version: str = "v2",
    variant: str = "k15_b10",
    start: date | None = None,
    end: date | None = None,
    config: M.TrainConfig | None = None,
    slippage_bps: float = HEADLINE_SLIPPAGE,
    use_mlflow: bool = True,
    save: bool = True,
    progress: Any = lambda message: None,
) -> tuple[M.TrainResult, list[B.BacktestResult], list[dict[str, Any]]]:
    """Train the meta-model, then backtest its walk-forward and holdout probabilities."""
    config = config or M.TrainConfig(label_version=label_version, variant=variant)
    progress("loading features and labels")
    features, labels = _load(settings, config.label_version, start, end)
    signals = S.all_signals(features)
    dataset = M.build_dataset(features, labels, signals, config)
    if dataset.empty:
        raise ConfigError("no usable rows: build the features and labels first")
    result = M.run_training(dataset, config, progress=progress)
    odds = {
        side: OutcomeOdds(
            target=rate["target"], stop=rate["stop"], time_limit_return=rate["time_limit_return"]
        )  # fmt: skip
        for side, rate in M.base_rates(dataset).items()
    }
    runs: list[B.BacktestResult] = []
    for holdout in (False, True):
        probabilities = M.probabilities_for(result, holdout=holdout)
        if probabilities.empty:
            continue
        rows = dataset.merge(probabilities, on=["instrument_id", "ts", "side"], how="inner")
        if rows.empty:
            continue
        # the model trades the side it scored, so the signal is just that side
        rows["signal"] = rows["side"].map({"long": 1, "short": -1}).astype("int8")
        simulated = B.simulate(
            _as_backtest_rows(rows, config),
            name=f"lgbm_intraday{'_holdout' if holdout else ''}",
            kind="model",
            label_version=config.label_version,
            variant=config.variant,
            policy=_policy(capital),
            costs=DEFAULT_INTRADAY_COSTS,
            slippage_bps=slippage_bps,
            period="holdout" if holdout else "walk_forward",
            ev_gate=True,
            odds_by_side=odds,
            params={"model": "lgbm_intraday", "slippage_bps": slippage_bps},
        )
        runs.append(simulated)
        if save:
            B.save(conn, simulated)
    registered = M.register(conn, settings, result, use_mlflow=use_mlflow) if save else []
    return result, runs, registered


def _as_backtest_rows(rows: pd.DataFrame, config: M.TrainConfig) -> pd.DataFrame:
    """The dataset already carries outcome / gross_return / bars_held under plain names, but the
    backtest looks the v2 columns up by variant, so give it the names it expects."""
    out = rows.copy()
    if config.label_version == "v2":
        out[f"outcome_{config.variant}"] = out["outcome"]
        out[f"gross_return_{config.variant}"] = out["gross_return"]
        out[f"bars_held_{config.variant}"] = out["bars_held"]
    return out


def run_report(conn: Any, *, limit: int = 40) -> list[str]:
    """The stored backtest runs, newest first."""
    rows = conn.execute(
        "SELECT kind, name, label_version, variant, period, from_date, to_date, trades,"
        " hit_rate, net_profit, return_pct, max_drawdown_pct, sharpe, days,"
        " days_without_trade, slippage_bps FROM backtest_runs ORDER BY id DESC LIMIT %s",
        (limit,),
    ).fetchall()
    if not rows:
        return ["no backtests stored yet: run `intraday backtest` first"]
    header = (
        f"{'kind':<9} {'name':<22} {'labels':<9} {'period':<13} {'slip':>5} {'trades':>7}"
        f" {'hit':>5} {'net Rs':>12} {'return':>8} {'maxDD':>7} {'Sharpe':>7}"
    )
    out = [header, "-" * len(header)]
    for r in rows:
        hit = f"{r[8]:.0%}" if r[8] is not None else "-"
        sharpe = f"{r[12]:+.2f}" if r[12] is not None else "-"
        out.append(
            f"{r[0]:<9} {r[1]:<22} {(r[3] or r[2]):<9} {r[4]:<13} {r[15]:>5.0f} {r[7]:>7,}"
            f" {hit:>5} {float(r[9]):>12,.0f} {r[10]:>7.1f}% {r[11]:>6.1f}% {sharpe:>7}"
        )
    return out


def last_built_day(settings: Settings, label_version: str) -> date:
    """The most recent day with both features and labels built (the default for a replay)."""
    features = I.load_built(settings, "intraday_features", I.VERSION, columns=["trade_date"])
    labels = I.load_built(settings, "intraday_labels", label_version, columns=["trade_date"])
    both = set(features["trade_date"]) & set(labels["trade_date"])
    if not both:
        raise ConfigError("no day has both features and labels built")
    return max(both)


def run_replay(
    settings: Settings,
    conn: Any,
    *,
    day: date | None = None,
    capital: float = DEFAULT_CAPITAL,
    label_version: str = "v2",
    variant: str = "k15_b10",
    strategy: str = "orb_15",
    slippage_bps: float = HEADLINE_SLIPPAGE,
    universe: str = "nifty50",
) -> list[str]:
    """Step through one past day and print each trade plan and what happened to it."""
    if strategy not in S.STRATEGY_BY_NAME:
        raise ConfigError(f"unknown strategy {strategy!r}")
    if day is None:
        day = last_built_day(settings, label_version)
    features, labels = _load(settings, label_version, day, day)
    if features.empty:
        raise ConfigError(f"no built features for {day}")
    symbols = dict(
        conn.execute(
            "SELECT i.id, i.symbol FROM instruments i JOIN universe_members u"
            " ON u.instrument_id = i.id AND u.universe = %s",
            (universe,),
        ).fetchall()
    )
    rule = S.STRATEGY_BY_NAME[strategy]
    signals = rule.signals(features)
    rows = B.prepare(
        features,
        labels,
        signals,
        label_version=label_version,
        variant=variant if label_version == "v2" else None,
        target_pct=settings.intraday_target_pct,
        stop_pct=settings.intraday_stop_pct,
    )
    policy = _policy(capital)
    out = [
        f"replay {day} - {strategy} ({rule.description}), labels {label_version}"
        f" {variant if label_version == 'v2' else ''}".rstrip(),
        f"capital Rs {capital:,.0f}; risk {policy.risk_per_trade:.0%} per trade;"
        f" daily loss limit {policy.daily_loss_limit:.0%}; at most"
        f" {policy.max_trades_per_day} trades; slippage {slippage_bps:g} bps per side",
        "",
    ]
    if rows.empty:
        out.append("no signals on this day: no trade.")
        return out
    result = B.simulate(
        rows,
        name=f"replay_{strategy}",
        label_version=label_version,
        variant=variant if label_version == "v2" else None,
        policy=policy,
        costs=DEFAULT_INTRADAY_COSTS,
        slippage_bps=slippage_bps,
        period="walk_forward",
    )
    state = DayState()
    for trade in result.trades:
        clock = (
            pd.Timestamp(trade.decided_at).tz_convert(I.IST).strftime("%H:%M")
            if pd.notna(trade.decided_at)
            else "??:??"
        )
        plan = plan_intraday_trade(
            symbols.get(trade.instrument_id, str(trade.instrument_id)),
            trade.side,
            trade.entry,
            target_pct=abs(100 * (trade.target - trade.entry) / trade.entry),
            stop_pct=abs(100 * (trade.entry - trade.stop) / trade.entry),
            odds=OutcomeOdds(target=trade.probability if trade.probability else 0.5),
            policy=policy,
            day=state,
            slippage_bps=slippage_bps,
            require_positive_ev=False,
        )
        out.append(f"{clock}  decided:")
        lines = plan.lines()
        if trade.probability is None:  # a rule has no probability of its own
            lines = [
                line.replace("P(target) 50%", "P(target) 50% (assumed: this rule has no model)")
                for line in lines
            ]
        out += [f"    {line}" for line in lines]
        out.append(
            f"    -> {trade.exit_reason} after {trade.bars_held} bar(s) at"
            f" {trade.exit_price:.2f}: gross Rs {trade.gross_profit:,.0f},"
            f" costs Rs {trade.costs:,.0f}, net Rs {trade.net_profit:,.0f}"
        )
        out.append("")
        state.record(trade.net_profit)
    out.append(
        f"day total: {len(result.trades)} trade(s), net Rs {result.net_profit:,.0f}"
        f" after Rs {result.costs_paid:,.0f} of costs"
    )
    for reason, n in sorted(result.skipped.items()):
        out.append(f"  skipped {n}: {reason}")
    return out
