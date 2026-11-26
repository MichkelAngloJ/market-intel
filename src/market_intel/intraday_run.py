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
    settings: Settings,
    label_version: str,
    start: date | None,
    end: date | None,
    universe: str = "nifty50",
) -> tuple[pd.DataFrame, pd.DataFrame]:
    features = I.load_built(
        settings, "intraday_features", I.VERSION, universe=universe, start=start, end=end
    )
    labels = I.load_built(
        settings, "intraday_labels", label_version, universe=universe, start=start, end=end
    )
    return features, labels


def run_backtest(
    settings: Settings,
    conn: Any,
    *,
    universe: str = "nifty50",
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
    features, labels = _load(settings, label_version, start, end, universe)
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
            variant=variant if label_version != "v1" else None,
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
                variant=variant if label_version != "v1" else None,
                policy=_policy(capital),
                costs=DEFAULT_INTRADAY_COSTS,
                slippage_bps=bps,
                period=period,
                params={
                    "rule": strategy.description,
                    "slippage_bps": bps,
                    "universe": universe,
                },
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
    universe: str = "nifty50",
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
    features, labels = _load(settings, config.label_version, start, end, universe)
    signals = S.all_signals(features)
    dataset = M.build_dataset(features, labels, signals, config)
    if dataset.empty:
        raise ConfigError("no usable rows: build the features and labels first")
    result = M.run_training(dataset, config, progress=progress)
    # calibrate before anything uses the probability: S5i-b showed the raw numbers are badly
    # overconfident at the top, and the expected-value gate trusts them directly
    M.calibrate(result)
    if save:
        M.save_predictions(settings, result, universe=universe, config=config)
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
        # only the backtest's own columns: merging all 45 features of 15M rows needs gigabytes
        # and the simulation never looks at a feature
        slim = dataset[[c for c in BACKTEST_COLUMNS if c in dataset]]
        rows = slim.merge(probabilities, on=["instrument_id", "ts", "side"], how="inner")
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
            params={
                "model": "lgbm_intraday",
                "slippage_bps": slippage_bps,
                "universe": universe,
                "calibrated": True,
            },
        )
        runs.append(simulated)
        if save:
            B.save(conn, simulated)
    registered = M.register(conn, settings, result, use_mlflow=use_mlflow) if save else []
    return result, runs, registered


# what the simulation reads; the features are not among them
BACKTEST_COLUMNS = (
    "instrument_id",
    "trade_date",
    "ts",
    "minute",
    "side",
    "entry_price",
    "outcome",
    "gross_return",
    "bars_held",
    "atr",
)


def _as_backtest_rows(rows: pd.DataFrame, config: M.TrainConfig) -> pd.DataFrame:
    """The dataset already carries outcome / gross_return / bars_held under plain names, but the
    backtest looks the v2 columns up by variant, so give it the names it expects."""
    out = rows.copy()
    if config.label_version in I.ATR_LABEL_VERSIONS:
        out[f"outcome_{config.variant}"] = out["outcome"]
        out[f"gross_return_{config.variant}"] = out["gross_return"]
        out[f"bars_held_{config.variant}"] = out["bars_held"]
    return out


def _cached_probabilities(frame: pd.DataFrame) -> pd.DataFrame:
    """A cached predictions frame as the calibrated (probability, side, key) a report wants."""
    if frame.empty:
        return frame
    column = "probability_calibrated" if "probability_calibrated" in frame else "probability"
    out = frame[["instrument_id", "ts", "side", column]].copy()
    return out.rename(columns={column: "probability"})


def run_precision(
    settings: Settings,
    conn: Any,
    *,
    universe: str = "nifty50",
    capital: float = DEFAULT_CAPITAL,
    label_version: str = "v2",
    variant: str = "k15_b10",
    start: date | None = None,
    end: date | None = None,
    config: M.TrainConfig | None = None,
    slippage_bps: float = HEADLINE_SLIPPAGE,
    retrain: bool = False,
    progress: Any = lambda message: None,
) -> list[str]:
    """Ask whether the model's confidence means anything, reusing the cached probabilities.

    `train` caches its walk-forward and holdout probabilities, so this normally costs seconds.
    With ``retrain`` (or no cache) it trains the walk-forward again, which takes much longer.
    No model is registered: this describes a training run so the owner can see whether the
    strongest "yes" signals pay for themselves.
    """
    config = config or M.TrainConfig(label_version=label_version, variant=variant)
    progress("loading features and labels")
    features, labels = _load(settings, config.label_version, start, end, universe)
    signals = S.all_signals(features)
    dataset = M.build_dataset(features, labels, signals, config)
    if dataset.empty:
        raise ConfigError("no usable rows: build the features and labels first")
    cached = M.load_predictions(
        settings, universe=universe, version=config.label_version, variant=config.variant
    )
    out: list[str] = []
    if cached.empty or retrain:
        progress("no cached probabilities: training the walk-forward")
        result = M.run_training(dataset, config, progress=progress)
        M.calibrate(result)
        M.save_predictions(settings, result, universe=universe, config=config)
        walk_probabilities = M.probabilities_for(result)
        holdout = M.probabilities_for(result, holdout=True)
        out += list(result.lines()) + [""] + M.calibration_lines(result)
    else:
        progress(f"using {len(cached):,} cached walk-forward probabilities")
        walk_probabilities = _cached_probabilities(cached)
        holdout = _cached_probabilities(
            M.load_predictions(
                settings,
                universe=universe,
                version=config.label_version,
                variant=config.variant,
                holdout=True,
            )
        )
        out += [
            f"cached probabilities for {universe} {config.label_version} {config.variant}:"
            f" {len(cached):,} walk-forward rows, {len(holdout):,} holdout rows"
        ]
    walk = M.precision_report(
        dataset,
        walk_probabilities,
        capital=capital,
        slippage_bps=slippage_bps,
        period="walk_forward",
    )
    out += [""] + walk.lines()
    if not holdout.empty:
        note = ""
        if len(holdout) < 2000:
            note = (
                f"only {len(holdout):,} scored rows in the holdout:"
                " read the bands below as an illustration, not as evidence"
            )
        out += [""] + M.precision_report(
            dataset,
            holdout,
            capital=capital,
            slippage_bps=slippage_bps,
            period="holdout (scored once)",
            warning=note,
        ).lines()
    return out


def run_report(conn: Any, *, limit: int = 40) -> list[str]:
    """The stored backtest runs as plain percentages, worst first, against "no trading".

    Doing nothing is the reference: it returns exactly 0%, pays nothing and risks nothing, so
    the column that matters is how far each strategy falls short of it.
    """
    rows = conn.execute(
        "SELECT kind, name, label_version, variant, period, from_date, to_date, trades,"
        " hit_rate, net_profit, return_pct, max_drawdown_pct, sharpe, days,"
        " days_without_trade, slippage_bps, params ->> 'edge_bps' FROM backtest_runs"
        " ORDER BY id DESC LIMIT %s",
        (limit,),
    ).fetchall()
    if not rows:
        return ["no backtests stored yet: run `intraday backtest` first"]
    header = (
        f"{'kind':<9} {'name':<22} {'labels':<9} {'period':<13} {'slip':>5} {'trades':>7}"
        f" {'hit':>5} {'return':>8} {'vs none':>9} {'maxDD':>7} {'edge bps':>9} {'net Rs':>12}"
    )
    out = [
        header,
        "-" * len(header),
    ]
    ordered = sorted(rows, key=lambda r: r[10] if r[10] is not None else 0.0)
    for r in ordered:
        hit = f"{r[8]:.0%}" if r[8] is not None else "-"
        edge = f"{float(r[16]):+.2f}" if r[16] is not None else "-"
        gap = (r[10] or 0.0) - 0.0  # no_trading is 0% by definition
        out.append(
            f"{r[0]:<9} {r[1]:<22} {(r[3] or r[2]):<9} {r[4]:<13} {r[15]:>5.0f} {r[7]:>7,}"
            f" {hit:>5} {r[10]:>7.2f}% {gap:>+8.2f}% {r[11]:>6.1f}% {edge:>9}"
            f" {float(r[9]):>12,.0f}"
        )
    out.append("")
    out.append(
        '"vs none" is the gap to doing nothing, which returns 0%: a negative number means the'
        " strategy lost money that no trading would have kept."
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
        variant=variant if label_version != "v1" else None,
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
        variant=variant if label_version != "v1" else None,
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
    notional = sum(t.entry * t.quantity for t in result.trades)
    move = sum(abs(t.exit_price - t.entry) * t.quantity for t in result.trades)
    out += [
        f"day total: {len(result.trades)} trade(s) on Rs {notional:,.0f} of traded value",
        f"  price moved   Rs {move:>10,.0f}"
        f"  ({100 * move / notional if notional else 0:.3f}% of the traded value)",
        f"  charges paid  Rs {result.costs_paid:>10,.0f}"
        f"  ({100 * result.costs_paid / capital:.3f}% of capital,"
        f" {100 * result.costs_paid / notional if notional else 0:.3f}% of the traded value)",
        f"  net result    Rs {result.net_profit:>10,.0f}"
        f"  ({100 * result.net_profit / capital:+.3f}% of capital)",
    ]
    if result.costs_paid > move:
        out.append("  -> the charges were LARGER than the whole price move the trades captured.")
    for reason, n in sorted(result.skipped.items()):
        out.append(f"  skipped {n}: {reason}")
    return out
