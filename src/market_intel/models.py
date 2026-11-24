"""21-day models: walk-forward training, baselines after costs, SHAP, MLflow and the registry
(sprint S3; decisions 2026-11-24).

    uv run python -m market_intel.models train --horizon 21
    uv run python -m market_intel.models report
    uv run python -m market_intel.models ui          # MLflow UI on 127.0.0.1 only

- Rows: features v1 for training (news counts excluded) + 21-day labels that exist, stocks with
  at least 252 days of history (thinner stocks are excluded from training).
- Models: a LightGBM classifier (up/down) and regressor (fwd_return) per sector with >= 20,000
  training rows and >= 8 stocks; the other sectors use ONE pooled model (all sectors, sector as
  a categorical feature), flagged "pooled fallback". Fixed settings, deterministic seeds.
- Walk-forward: yearly test folds with purge and a 21-day embargo
  (:mod:`market_intel.evaluation`); then the final 6-month HOLDOUT, evaluated once; then every
  model is retrained on all usable rows as a CANDIDATE for S4 (status candidate, never active).
- Baselines on the same folds and costs: always-up, momentum (past 21-day return), SMA rule
  (close > SMA50), equal-weight universe, Nifty 50 hold.
- Everything is logged to a local MLflow (sqlite + artifacts under DATA_DIR/mlflow); model files
  and cards go to DATA_DIR/models (git-ignored); one ``model_versions`` row per model.
"""

from __future__ import annotations

import argparse
import io
import json
import math
import os
import re
import subprocess
import sys
import time as time_mod
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from market_intel import evaluation as ev
from market_intel.config import ConfigError, Settings, get_settings
from market_intel.costs import DEFAULT_COSTS, CostModel
from market_intel.db import postgres
from market_intel.explain import global_importance, top_reasons
from market_intel.feature_spec import FEATURE_VERSION, TRAINING_FEATURES
from market_intel.ingestion import ingestion_run
from market_intel.logging_setup import bound_context, configure_logging, get_logger, new_run_id

os.environ.setdefault("MLFLOW_DISABLE_AGENT_HINT", "1")

COMPONENT = "models"
JOB = "models_train"
EXPERIMENT = "market-intel-21d"
MIN_HISTORY = 252
MIN_SECTOR_ROWS = 20_000
MIN_SECTOR_STOCKS = 8
MIN_FOLD_SECTOR_ROWS = 2_000  # a sector model trains in a fold only with this many rows
TOP_K = 20
CAPITAL = 1_000_000.0
SHAP_SAMPLE = 2_000
POOLED = "pooled"
LGBM_PARAMS: dict[str, Any] = {
    "n_estimators": 200,
    "learning_rate": 0.05,
    "num_leaves": 31,
    "min_child_samples": 200,
    "subsample": 0.8,
    "subsample_freq": 1,
    "colsample_bytree": 0.8,
    "reg_lambda": 1.0,
    "random_state": 42,
    "deterministic": True,
    "force_row_wise": True,
    "n_jobs": 4,
    "verbose": -1,
}
BIASES = (
    "Survivorship bias: today's Nifty 500 members are used for the whole history, so companies "
    "that dropped out are missing and results look better than reality.",
    "Raw prices: prices are not adjusted for splits and bonuses; label windows with a suspected "
    "split/bonus are excluded, and a portfolio period with one, or with a raw 21-day move below "
    "-90% or above +300% (likely an unrecorded corporate action), earns 0 for every strategy.",
)

log = get_logger("market_intel.models")


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")[:40] or "unknown"


def model_name(horizon: int, sector: str | None) -> str:
    return f"lgbm_{horizon}d_{slug(sector) if sector else POOLED}"


# --- data --------------------------------------------------------------------------------


@dataclass
class Dataset:
    """Everything training and evaluation need, in memory."""

    panel: (
        pd.DataFrame
    )  # instrument_id, trade_date, sector, features, fwd_return, up, label_end, ret_21
    calendar: list[date]
    prices: dict[int, pd.DataFrame]  # open/close by date per stock
    nifty: pd.DataFrame | None
    events: dict[int, list[tuple[date, float]]] = field(default_factory=dict)
    symbols: dict[int, str] = field(default_factory=dict)
    horizon: int = ev.HORIZON


def _copy_frame(conn: Any, sql: str) -> pd.DataFrame:
    buf = io.BytesIO()
    with conn.cursor() as cur, cur.copy(f"COPY ({sql}) TO STDOUT WITH CSV HEADER") as copy:
        for chunk in copy:
            buf.write(chunk)
    buf.seek(0)
    return pd.read_csv(buf)


def load_dataset(
    conn: Any,
    *,
    horizon: int = ev.HORIZON,
    universe: str = "nifty500",
    version: str = FEATURE_VERSION,
) -> Dataset:
    """Features + labels + prices for a universe (app login; COPY for speed)."""
    from market_intel.features import load_inputs

    if not re.fullmatch(r"[a-z0-9_]+", universe) or not re.fullmatch(r"v[0-9]+", version):
        raise ConfigError("bad universe or feature version name")
    cols = ", ".join(f"f.{c}" for c in TRAINING_FEATURES)
    feats = _copy_frame(
        conn,
        f"SELECT f.instrument_id, f.trade_date, i.sector, {cols} FROM features_daily f"
        " JOIN universe_members u ON u.instrument_id = f.instrument_id"
        f" AND u.universe = '{universe}'"
        f" JOIN instruments i ON i.id = f.instrument_id WHERE f.feature_version = '{version}'",
    )
    labels = _copy_frame(
        conn,
        "SELECT instrument_id, trade_date, fwd_return, up FROM labels_daily"
        f" WHERE horizon_days = {int(horizon)}",
    )
    for frame in (feats, labels):
        frame["trade_date"] = pd.to_datetime(frame["trade_date"]).dt.date
    panel = feats.merge(labels, on=["instrument_id", "trade_date"], how="left")
    inputs = load_inputs(conn, universe=universe, with_news=False)
    events: dict[int, list[tuple[date, float]]] = {}
    for inst, day, ratio in conn.execute(
        "SELECT instrument_id, trade_date, ratio::float8 FROM price_events"
    ).fetchall():
        events.setdefault(inst, []).append((day, ratio))
    return build_dataset(
        panel, inputs.calendar, inputs.candles, inputs.nifty, events, inputs.symbols, horizon
    )


def build_dataset(
    panel: pd.DataFrame,
    calendar: list[date],
    candles: dict[int, pd.DataFrame],
    nifty: pd.DataFrame | None,
    events: dict[int, list[tuple[date, float]]],
    symbols: dict[int, str],
    horizon: int = ev.HORIZON,
) -> Dataset:
    """Add the label end, the past 21-day return (momentum baseline) and tidy types."""
    panel = panel.copy()
    panel["sector"] = panel["sector"].fillna("Unknown")
    panel["up"] = panel["up"].map(
        lambda v: None if pd.isna(v) else bool(v) if not isinstance(v, str) else v == "t"
    )
    panel["label_end"] = ev.label_end(panel["trade_date"], calendar, horizon)
    ret21 = []
    for inst, frame in candles.items():
        close = frame["close"].astype(float)
        r = close / close.shift(horizon) - 1
        ret21.append(
            pd.DataFrame(
                {"instrument_id": inst, "trade_date": list(r.index), "ret_21": r.to_numpy()}
            )
        )
    if ret21:
        panel = panel.merge(pd.concat(ret21), on=["instrument_id", "trade_date"], how="left")
    else:
        panel["ret_21"] = np.nan
    prices = {k: v[["open", "close"]].astype(float) for k, v in candles.items()}
    return Dataset(
        panel.sort_values(["trade_date", "instrument_id"], ignore_index=True),
        list(calendar),
        prices,
        nifty,
        events,
        symbols,
        horizon,
    )


# --- training ----------------------------------------------------------------------------


def feature_columns(pooled: bool) -> list[str]:
    return [*TRAINING_FEATURES, *(["sector"] if pooled else [])]


def matrix(frame: pd.DataFrame, pooled: bool, sectors: Sequence[str]) -> pd.DataFrame:
    X = frame[list(TRAINING_FEATURES)].astype("float64")
    if pooled:
        X = X.assign(sector=pd.Categorical(frame["sector"], categories=list(sectors)))
    return X


@dataclass
class Pair:
    """A classifier (up/down) and a regressor (fwd_return) trained on the same rows."""

    name: str
    sector: str | None
    clf: Any
    reg: Any
    rows: int
    stocks: int
    train_from: date
    train_to: date


def fit_pair(name: str, sector: str | None, train: pd.DataFrame, sectors: Sequence[str]) -> Pair:
    from lightgbm import LGBMClassifier, LGBMRegressor

    pooled = sector is None
    X = matrix(train, pooled, sectors)
    clf = LGBMClassifier(**LGBM_PARAMS).fit(X, train["up"].astype(bool))
    reg = LGBMRegressor(**LGBM_PARAMS).fit(X, train["fwd_return"].astype(float))
    return Pair(
        name,
        sector,
        clf,
        reg,
        len(train),
        train["instrument_id"].nunique(),
        min(train["trade_date"]),
        max(train["trade_date"]),
    )


def choose_sector_models(train: pd.DataFrame, min_rows: int, min_stocks: int) -> list[str]:
    stats = train.groupby("sector").agg(
        rows=("instrument_id", "size"), stocks=("instrument_id", "nunique")
    )
    return sorted(stats[(stats.rows >= min_rows) & (stats.stocks >= min_stocks)].index)


def usable(panel: pd.DataFrame) -> pd.Series:
    return panel["fwd_return"].notna() & (panel["history_days"] >= MIN_HISTORY)


def candidates(panel: pd.DataFrame) -> pd.Series:
    return panel["history_days"] >= MIN_HISTORY


def train_and_predict(
    data: Dataset,
    train_rows: pd.Series,
    test_rows: pd.Series,
    sector_models: Sequence[str],
    sectors: Sequence[str],
    label: str,
) -> tuple[pd.DataFrame, dict[str, Pair]]:
    """Fit the sector models and the pooled model on ``train_rows``; predict ``test_rows``.
    A sector with too few rows in this training window falls back to the pooled model."""
    panel = data.panel
    train, test = panel[train_rows], panel[test_rows]
    pairs: dict[str, Pair] = {}
    if len(train) == 0 or len(test) == 0:
        return pd.DataFrame(), pairs
    pooled = fit_pair(model_name(data.horizon, None), None, train, sectors)
    pairs[POOLED] = pooled
    out = []
    covered: set[str] = set()
    for sector in sector_models:
        sub = train[train["sector"] == sector]
        if len(sub) < MIN_FOLD_SECTOR_ROWS:
            continue
        pair = fit_pair(model_name(data.horizon, sector), sector, sub, sectors)
        pairs[sector] = pair
        rows = test[test["sector"] == sector]
        if len(rows):
            out.append(_predict(pair, rows, sectors, label))
        covered.add(sector)
    rest = test[~test["sector"].isin(covered)]
    if len(rest):
        out.append(_predict(pooled, rest, sectors, label))
    return pd.concat(out, ignore_index=True), pairs


def _predict(pair: Pair, rows: pd.DataFrame, sectors: Sequence[str], label: str) -> pd.DataFrame:
    X = matrix(rows, pair.sector is None, sectors)
    return pd.DataFrame(
        {
            "trade_date": rows["trade_date"].to_numpy(),
            "instrument_id": rows["instrument_id"].to_numpy(),
            "sector": rows["sector"].to_numpy(),
            "model": pair.name,
            "fold": label,
            "prob": pair.clf.predict_proba(X)[:, 1],
            "pred_ret": pair.reg.predict(X),
            "up": rows["up"].to_numpy(),
            "fwd_return": rows["fwd_return"].to_numpy(),
        }
    )


# --- evaluation --------------------------------------------------------------------------


def _clean(obj: Any) -> Any:
    """JSON-safe: NaN/inf -> None, numpy -> Python, dates -> ISO."""
    if isinstance(obj, dict):
        return {str(k): _clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_clean(v) for v in obj]
    if isinstance(obj, (np.floating, float)):
        return None if math.isnan(obj) or math.isinf(obj) else float(obj)
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (date, datetime)):
        return obj.isoformat()
    return obj


def _unmeasurable(returns: pd.DataFrame, dates: Sequence[date]) -> dict[str, int]:
    """How many (stock, period) returns couldn't be measured on raw prices (earn 0)."""
    part = returns[returns["date"].isin(set(dates))]
    return {"stock_periods": int(part["ret"].isna().sum()), "of": int(len(part))}


def classification_block(preds: pd.DataFrame) -> dict[str, Any]:
    rows = preds[preds["up"].notna()]
    return ev.classification_metrics(rows["up"].astype(bool), rows["prob"])


def baseline_classification(panel_rows: pd.DataFrame) -> dict[str, dict[str, float]]:
    rows = panel_rows[panel_rows["up"].notna()]
    y = rows["up"].astype(bool)
    return {
        "always_up": ev.classification_metrics(y, np.ones(len(rows))),
        "momentum": ev.classification_metrics(y, (rows["ret_21"] > 0).astype(float)),
        "sma_rule": ev.classification_metrics(y, (rows["close_sma50"] > 0).astype(float)),
    }


def portfolios(
    data: Dataset,
    preds: pd.DataFrame,
    dates: Sequence[date],
    returns: pd.DataFrame,
    *,
    instruments: set[int] | None = None,
    k: int = TOP_K,
    costs: CostModel = DEFAULT_COSTS,
    capital: float = CAPITAL,
) -> dict[str, ev.PortfolioResult]:
    """The model's top-k portfolio and every baseline on the same dates and costs."""
    date_set = set(dates)
    panel = data.panel[candidates(data.panel) & data.panel["trade_date"].isin(date_set)]
    p = preds[preds["trade_date"].isin(date_set)]
    if instruments is not None:
        panel = panel[panel["instrument_id"].isin(instruments)]
        p = p[p["instrument_id"].isin(instruments)]
    slip = {
        (t, i): costs.slippage_for(v)
        for t, i, v in zip(
            panel["trade_date"], panel["instrument_id"], panel["log_traded_value_20d"], strict=True
        )
    }

    def frame(df: pd.DataFrame, score: str, tie: str | None) -> pd.DataFrame:
        return pd.DataFrame(
            {
                "date": df["trade_date"].to_numpy(),
                "instrument_id": df["instrument_id"].to_numpy(),
                "score": df[score].to_numpy(),
                "tiebreak": df[tie].to_numpy() if tie else 0.0,
            }
        )

    out = {
        "model": ev.simulate(
            "model",
            frame(p, "prob", "pred_ret"),
            returns,
            k=k,
            costs=costs,
            capital=capital,
            slippage=slip,
        ),
        "momentum": ev.simulate(
            "momentum",
            frame(panel, "ret_21", None),
            returns,
            k=k,
            costs=costs,
            capital=capital,
            slippage=slip,
        ),
        "sma_rule": ev.simulate(
            "sma_rule",
            frame(panel, "close_sma50", None),
            returns,
            k=k,
            costs=costs,
            capital=capital,
            slippage=slip,
        ),
        "equal_weight": ev.simulate(
            "equal_weight",
            frame(panel.assign(one=1.0), "one", None),
            returns,
            k=None,
            costs=costs,
            capital=capital,
            slippage=slip,
        ),
    }
    if data.nifty is not None and len(dates):
        idx = ev.period_returns({0: data.nifty}, data.calendar, dates)
        out["nifty50"] = ev.index_hold(
            "nifty50", list(idx.sort_values("date")["ret"]), costs=costs, capital=capital
        )
    else:
        out["nifty50"] = ev.PortfolioResult("nifty50")
    out["always_up"] = out["equal_weight"]  # everything predicted up -> the whole universe
    return out


def verdict(results: dict[str, ev.PortfolioResult]) -> bool:
    others = [results[n] for n in ("momentum", "sma_rule", "nifty50")]
    return ev.beats_baseline(results["model"], results["equal_weight"], others)


# --- the whole run -----------------------------------------------------------------------


@dataclass
class TrainResult:
    horizon: int
    folds: list[ev.Fold]
    holdout_from: date
    sector_models: list[str]
    pooled_sectors: list[str]
    report: dict[str, Any]
    final: dict[str, Pair]
    shap: dict[str, pd.DataFrame]
    example: dict[str, Any]
    thin_stocks: int
    seconds: float = 0.0


def run_training(
    data: Dataset,
    *,
    min_sector_rows: int = MIN_SECTOR_ROWS,
    min_sector_stocks: int = MIN_SECTOR_STOCKS,
    k: int = TOP_K,
    first_year: int = ev.FIRST_TEST_YEAR,
    costs: CostModel = DEFAULT_COSTS,
    capital: float = CAPITAL,
    progress: Callable[[str], None] = lambda msg: None,
) -> TrainResult:
    """Walk-forward + holdout + final retrain (see the module docstring). Pure: no database."""
    t0 = time_mod.monotonic()
    panel = data.panel
    use = usable(panel)
    usable_dates = sorted(set(panel.loc[use, "trade_date"]))
    if not usable_dates:
        raise ConfigError("no usable training rows (features with labels and 252+ days of history)")
    holdout_from = ev.holdout_start(usable_dates)
    folds = ev.make_folds(usable_dates, holdout_from, first_year)
    if not folds:
        raise ConfigError("not enough history for a walk-forward fold before the holdout")
    sectors = sorted(panel["sector"].unique())
    pre_holdout = use & (panel["trade_date"] < holdout_from)
    sector_models = choose_sector_models(panel[pre_holdout], min_sector_rows, min_sector_stocks)
    pooled_sectors = [s for s in sectors if s not in sector_models]
    cand = candidates(panel)

    preds = []
    for i, fold in enumerate(folds):
        progress(f"fold {fold.name} ({i + 1}/{len(folds)})")
        train_rows = use & ev.train_mask(
            panel["trade_date"], panel["label_end"], fold.test_start, folds[:i], data.calendar
        )
        test_rows = (
            cand & (panel["trade_date"] >= fold.test_start) & (panel["trade_date"] <= fold.test_end)
        )
        fold_preds, _ = train_and_predict(
            data, train_rows, test_rows, sector_models, sectors, fold.name
        )
        preds.append(fold_preds)
    # a fold without training rows (too early) makes no predictions: leave it out
    folds = [f for f, p in zip(folds, preds, strict=True) if len(p)]
    if not folds:
        raise ConfigError("no walk-forward fold had training rows before it")
    wf = pd.concat([p for p in preds if len(p)], ignore_index=True)

    progress("holdout (evaluated once)")
    hold_train = use & ev.train_mask(
        panel["trade_date"], panel["label_end"], holdout_from, folds, data.calendar
    )
    hold_rows = cand & (panel["trade_date"] >= holdout_from)
    hold, _ = train_and_predict(data, hold_train, hold_rows, sector_models, sectors, "holdout")

    progress("final retrain on all usable rows")
    _, final = train_and_predict(
        data,
        use,
        cand & (panel["trade_date"] == panel["trade_date"].max()),
        sector_models,
        sectors,
        "final",
    )

    progress("evaluation")
    last_wf_exit = max(d for d in data.calendar if d < holdout_from)
    # every strategy is evaluated on the same dates: those where the model has predictions
    wf_dates = [
        d
        for d in ev.rebalance_dates(data.calendar, folds[0].test_start, last_wf_exit, data.horizon)
        if d in set(wf["trade_date"])
    ]
    hold_dates = [
        d
        for d in ev.rebalance_dates(data.calendar, holdout_from, data.calendar[-1], data.horizon)
        if d in set(hold["trade_date"])
    ]
    all_dates = sorted(set(wf_dates) | set(hold_dates))
    returns = ev.period_returns(data.prices, data.calendar, all_dates, data.events, data.horizon)

    def block(
        dates: Sequence[date], p: pd.DataFrame, insts: set[int] | None, kk: int
    ) -> dict[str, Any]:
        res = portfolios(
            data, p, dates, returns, instruments=insts, k=kk, costs=costs, capital=capital
        )
        return {
            "portfolio": {n: r.summary() for n, r in res.items()},
            "beats_baseline": verdict(res),
        }

    wf_panel = panel[cand & panel["trade_date"].between(folds[0].test_start, folds[-1].test_end)]
    hold_panel = panel[cand & (panel["trade_date"] >= holdout_from)]
    report: dict[str, Any] = {
        "horizon": data.horizon,
        "holdout_from": holdout_from,
        "folds": [
            {"name": f.name, "test_start": f.test_start, "test_end": f.test_end} for f in folds
        ],
        "costs": costs.__dict__,
        "top_k": k,
        "walk_forward": {
            "classification": classification_block(wf),
            "baselines_classification": baseline_classification(wf_panel),
            "per_fold": {f: classification_block(g) for f, g in wf.groupby("fold")},
            "per_sector": {s: classification_block(g) for s, g in wf.groupby("sector")},
            "calibration": ev.calibration_table(
                wf.loc[wf.up.notna(), "up"].astype(bool), wf.loc[wf.up.notna(), "prob"]
            ),
            **block(wf_dates, wf, None, k),
            "rebalances": len(wf_dates),
            "unmeasurable": _unmeasurable(returns, wf_dates),
        },
        "holdout": {
            "classification": classification_block(hold),
            "baselines_classification": baseline_classification(hold_panel),
            **block(hold_dates, hold, None, k),
            "rebalances": len(hold_dates),
            "unmeasurable": _unmeasurable(returns, hold_dates),
        },
        "models": {},
    }
    for key, pair in final.items():
        if key == POOLED:
            insts = set(panel.loc[panel["sector"].isin(pooled_sectors), "instrument_id"])
            name_preds, hold_preds = (
                wf[wf["sector"].isin(pooled_sectors)],
                hold[hold["sector"].isin(pooled_sectors)],
            )
        else:
            insts = set(panel.loc[panel["sector"] == key, "instrument_id"])
            name_preds, hold_preds = wf[wf["sector"] == key], hold[hold["sector"] == key]
        kk = min(k, max(3, len(insts) // 3)) if insts else k
        report["models"][pair.name] = {
            "sector": pair.sector,
            "kind": "pooled" if key == POOLED else "sector",
            "covers_sectors": pooled_sectors if key == POOLED else [key],
            "stocks": len(insts),
            "top_k": kk,
            "walk_forward": {
                "classification": classification_block(name_preds),
                **block(wf_dates, name_preds, insts, kk),
            },
            "holdout": {
                "classification": classification_block(hold_preds),
                **block(hold_dates, hold_preds, insts, kk),
            },
            "train_rows_final": pair.rows,
        }

    progress("SHAP")
    shap_tables: dict[str, pd.DataFrame] = {}
    for key, pair in final.items():
        rows = panel[use] if key == POOLED else panel[use & (panel["sector"] == key)]
        sample = rows.sample(min(SHAP_SAMPLE, len(rows)), random_state=7)
        shap_tables[pair.name] = global_importance(
            pair.clf, matrix(sample, pair.sector is None, sectors)
        )
    latest = panel[cand & (panel["trade_date"] == panel["trade_date"].max())].sort_values(
        "instrument_id"
    )
    example: dict[str, Any] = {}
    if len(latest):
        row = latest.iloc[[0]]
        sector = row["sector"].iloc[0]
        pair = final.get(sector) or final[POOLED]
        example = {
            "instrument_id": int(row["instrument_id"].iloc[0]),
            "symbol": data.symbols.get(int(row["instrument_id"].iloc[0])),
            "trade_date": row["trade_date"].iloc[0],
            "model": pair.name,
            "prob_up": float(
                pair.clf.predict_proba(matrix(row, pair.sector is None, sectors))[:, 1][0]
            ),
            "reasons": top_reasons(pair.clf, matrix(row, pair.sector is None, sectors), k=5),
        }
    # stocks that never reach 252 days of history: excluded from training ("thin data")
    thin = int((panel.groupby("instrument_id")["history_days"].max() < MIN_HISTORY).sum())
    report["sector_models"] = sector_models
    report["pooled_sectors"] = pooled_sectors
    report["thin_stocks_excluded"] = thin
    report["biases"] = list(BIASES)
    return TrainResult(
        data.horizon,
        folds,
        holdout_from,
        sector_models,
        pooled_sectors,
        _clean(report),
        final,
        shap_tables,
        _clean(example),
        thin,
        time_mod.monotonic() - t0,
    )


# --- cards, MLflow, registry -------------------------------------------------------------


def _pct(v: Any) -> str:
    return "-" if v is None else f"{v * 100:.1f}%"


def portfolio_table(block: dict[str, Any]) -> list[str]:
    head = (
        f"| {'strategy':<13} | total | annual | Sharpe | max DD | hit rate | turnover"
        " | costs paid |"
    )
    lines = [head, "|---|---|---|---|---|---|---|---|"]
    for name in ("model", "equal_weight", "always_up", "momentum", "sma_rule", "nifty50"):
        r = block["portfolio"].get(name)
        if not r:
            continue
        lines.append(
            f"| {name:<13} | {_pct(r['total_return'])} | {_pct(r['annual_return'])}"
            f" | {r['sharpe'] or 0:.2f} | {_pct(r['max_drawdown'])} | {_pct(r['hit_rate'])}"
            f" | {_pct(r['turnover'])} | {_pct(r['costs_paid'])} |"
        )
    return lines


def model_card(
    name: str,
    version: int,
    entry: dict[str, Any],
    report: dict[str, Any],
    pair: Pair,
    shap_table: pd.DataFrame,
) -> str:
    wf, hold = entry["walk_forward"], entry["holdout"]
    beats = "yes" if wf["beats_baseline"] else "no"
    c = wf["classification"]
    lines = [
        f"# Model card: {name} v{version}",
        "",
        f"- **Beats the baselines after costs (walk-forward): {beats}.** "
        + (
            "Its top picks beat the equal-weight universe and the best other baseline."
            if beats == "yes"
            else "Its top picks did NOT beat both the equal-weight universe and the best other"
            " baseline."
        ),
        "- Status: candidate (not active; activation needs paper-trading results and the owner).",
        f"- Kind: {entry['kind']}"
        + (
            " (pooled fallback for: " + ", ".join(entry["covers_sectors"]) + ")"
            if entry["kind"] == "pooled"
            else f" (sector: {entry['sector']})"
        ),
        f"- Horizon: {report['horizon']} trading days; classifier (up/down) + regressor"
        " (forward return); LightGBM, fixed settings, seed 42.",
        f"- Final training data: {pair.train_from} to {pair.train_to}, {pair.rows:,} rows,"
        f" {pair.stocks} stocks.",
        f"- Walk-forward folds: {', '.join(f['name'] for f in report['folds'])}"
        " (purge + 21-day embargo).",
        f"- Holdout (evaluated once): from {report['holdout_from']}.",
        f"- Features: {len(TRAINING_FEATURES)} features v1 ({', '.join(TRAINING_FEATURES)});"
        " news counts are not used.",
        f"- Thin data: stocks with fewer than {MIN_HISTORY} days of history are excluded"
        " from training"
        f" ({report['thin_stocks_excluded']} stocks in the universe); their predictions must be"
        ' flagged "thin data".',
        "",
        "## Walk-forward classification",
        "",
        f"AUC {c.get('auc') or 0:.3f}, accuracy {_pct(c.get('accuracy'))},"
        f" precision for up {_pct(c.get('precision_up'))}, Brier {c.get('brier') or 0:.4f},"
        f" share up {_pct(c.get('up_rate'))}, rows {int(c.get('rows') or 0):,}.",
        "",
        f"## Portfolio after costs (top {entry['top_k']} every {report['horizon']} trading days)",
        "",
        "Walk-forward:",
        "",
        *portfolio_table(wf),
        "",
        f"Holdout (beats the baselines: {'yes' if hold['beats_baseline'] else 'no'}):",
        "",
        *portfolio_table(hold),
        "",
        "## Top features (mean |SHAP|, classifier)",
        "",
        *[
            f"{i + 1}. {r.feature}: {r.mean_abs_shap:.4f}"
            for i, r in shap_table.head(10).iterrows()
        ],
        "",
        "## Known biases",
        "",
        *[f"- {b}" for b in report["biases"]],
        "- SHAP explains the model, not the market: reasons say why the model thinks so,"
        " not that it is true.",
        "",
    ]
    return "\n".join(lines)


def mlflow_setup(settings: Settings) -> Any:
    import mlflow

    root = settings.data_dir / "mlflow"
    (root / "artifacts").mkdir(parents=True, exist_ok=True)
    mlflow.set_tracking_uri(tracking_uri(settings))
    if mlflow.get_experiment_by_name(EXPERIMENT) is None:
        mlflow.create_experiment(
            EXPERIMENT, artifact_location=(root / "artifacts").resolve().as_uri()
        )
    mlflow.set_experiment(EXPERIMENT)
    return mlflow


def tracking_uri(settings: Settings) -> str:
    return "sqlite:///" + (settings.data_dir / "mlflow" / "mlflow.db").resolve().as_posix()


def _metric_key(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_\-./ ]", "_", name)[:250]


def register(
    conn: Any, settings: Settings, result: TrainResult, *, use_mlflow: bool = True
) -> list[dict[str, Any]]:
    """Save model files and cards, log to MLflow, insert one candidate row per model."""
    models_dir = settings.data_dir / "models"
    models_dir.mkdir(parents=True, exist_ok=True)
    report = result.report
    (models_dir / "last_report.json").write_text(
        json.dumps({**report, "example": result.example}, indent=2), encoding="utf-8"
    )
    mlflow = mlflow_setup(settings) if use_mlflow else None
    rows = []
    parent = (
        mlflow.start_run(run_name=f"train-{result.horizon}d-{datetime.now(UTC):%Y%m%dT%H%M%SZ}")
        if mlflow
        else None
    )
    try:
        if mlflow:
            mlflow.log_params(
                {
                    "horizon": result.horizon,
                    "holdout_from": str(result.holdout_from),
                    "folds": ",".join(f.name for f in result.folds),
                    "top_k": report["top_k"],
                    "feature_version": FEATURE_VERSION,
                    **{f"lgbm_{k}": v for k, v in LGBM_PARAMS.items()},
                }
            )
            for name, r in report["walk_forward"]["portfolio"].items():
                mlflow.log_metric(
                    _metric_key(f"wf_{name}_annual_return"), r["annual_return"] or 0.0
                )
            for name, r in report["holdout"]["portfolio"].items():
                mlflow.log_metric(
                    _metric_key(f"holdout_{name}_annual_return"), r["annual_return"] or 0.0
                )
            mlflow.log_metric("wf_auc", report["walk_forward"]["classification"].get("auc") or 0.0)
            mlflow.log_metric("wf_beats_baseline", float(report["walk_forward"]["beats_baseline"]))
            mlflow.log_artifact(str(models_dir / "last_report.json"))
        for pair in result.final.values():
            entry = report["models"][pair.name]
            version = conn.execute(
                "SELECT coalesce(max(version), 0) + 1 FROM model_versions WHERE name = %s",
                (pair.name,),
            ).fetchone()[0]
            stem = models_dir / f"{pair.name}-v{version}"
            pair.clf.booster_.save_model(f"{stem}-clf.txt")
            pair.reg.booster_.save_model(f"{stem}-reg.txt")
            shap_table = result.shap[pair.name]
            shap_table.to_csv(f"{stem}-shap.csv", index=False)
            card = Path(f"{stem}.md")
            card.write_text(
                model_card(pair.name, version, entry, report, pair, shap_table), encoding="utf-8"
            )
            run_id = None
            if mlflow:
                with mlflow.start_run(run_name=f"{pair.name}-v{version}", nested=True) as child:
                    run_id = child.info.run_id
                    mlflow.log_params(
                        {
                            "name": pair.name,
                            "kind": entry["kind"],
                            "sector": pair.sector or "pooled",
                            "version": version,
                            "train_rows": pair.rows,
                            "stocks": pair.stocks,
                            "train_from": str(pair.train_from),
                            "train_to": str(pair.train_to),
                        }
                    )
                    for scope in ("walk_forward", "holdout"):
                        cls = entry[scope]["classification"]
                        for metric in ("auc", "accuracy", "precision_up", "brier"):
                            if cls.get(metric) is not None:
                                mlflow.log_metric(f"{scope}_{metric}", cls[metric])
                        for name, r in entry[scope]["portfolio"].items():
                            if r.get("annual_return") is not None:
                                mlflow.log_metric(
                                    _metric_key(f"{scope}_{name}_annual_return"), r["annual_return"]
                                )
                    mlflow.log_metric(
                        "beats_baseline", float(entry["walk_forward"]["beats_baseline"])
                    )
                    for path in (
                        f"{stem}-clf.txt",
                        f"{stem}-reg.txt",
                        f"{stem}-shap.csv",
                        str(card),
                    ):
                        mlflow.log_artifact(path)
            row_id = conn.execute(
                "INSERT INTO model_versions (name, version, horizon, sector, kind, train_from,"
                " train_to,"
                " holdout_from, metrics, beats_baseline, status, mlflow_run_id, card_path)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, 'candidate', %s, %s)"
                " RETURNING id",
                (
                    pair.name,
                    version,
                    result.horizon,
                    pair.sector,
                    entry["kind"],
                    pair.train_from,
                    pair.train_to,
                    result.holdout_from,
                    json.dumps(entry),
                    bool(entry["walk_forward"]["beats_baseline"]),
                    run_id,
                    str(card),
                ),
            ).fetchone()[0]
            rows.append(
                {
                    "id": row_id,
                    "name": pair.name,
                    "version": version,
                    "kind": entry["kind"],
                    "beats_baseline": entry["walk_forward"]["beats_baseline"],
                }
            )
    finally:
        if mlflow and parent:
            mlflow.end_run()
    return rows


# --- report ------------------------------------------------------------------------------


def _count(block: dict[str, Any]) -> str:
    u = block.get("unmeasurable") or {}
    return f"{u.get('stock_periods', 0):,} of {u.get('of', 0):,}"


def report_lines(report: dict[str, Any], example: dict[str, Any] | None = None) -> list[str]:
    wf, hold = report["walk_forward"], report["holdout"]
    c = wf["classification"]
    lines = [
        f"{report['horizon']}-day models: folds {', '.join(f['name'] for f in report['folds'])};"
        f" holdout from {report['holdout_from']}; top {report['top_k']}",
        f"  sector models ({len(report['sector_models'])}): {', '.join(report['sector_models'])}",
        f"  pooled fallback for: {', '.join(report['pooled_sectors']) or 'none'};"
        f" thin stocks excluded from training: {report['thin_stocks_excluded']}",
        "",
        "Walk-forward classification (all models):"
        f" AUC {c.get('auc') or 0:.3f}, accuracy {_pct(c.get('accuracy'))},"
        f" precision up {_pct(c.get('precision_up'))}, Brier {c.get('brier') or 0:.4f},"
        f" rows {int(c.get('rows') or 0):,}",
        "  baselines: "
        + ", ".join(
            f"{n} AUC {m.get('auc') or 0:.3f} acc {_pct(m.get('accuracy'))}"
            for n, m in wf["baselines_classification"].items()
        ),
        "",
        "Per fold (AUC, accuracy, rows):",
        *[
            f"  {f}: {m.get('auc') or 0:.3f}, {_pct(m.get('accuracy'))},"
            f" {int(m.get('rows') or 0):,}"
            for f, m in wf["per_fold"].items()
        ],
        "",
        "Unmeasurable stock-periods (raw prices; earn 0 for every strategy):"
        f" walk-forward {_count(wf)}, holdout {_count(hold)}",
        "",
        f"Walk-forward portfolio after costs ({wf['rebalances']} rebalances):"
        f" beats the baselines: {'YES' if wf['beats_baseline'] else 'NO'}",
        *portfolio_table(wf),
        "",
        f"Holdout portfolio after costs ({hold['rebalances']} rebalances, evaluated once):"
        f" beats the baselines: {'YES' if hold['beats_baseline'] else 'NO'}",
        *portfolio_table(hold),
        "",
        "Per model (walk-forward annual return after costs: model vs equal-weight; beats):",
    ]
    for name, m in report["models"].items():
        p = m["walk_forward"]["portfolio"]
        lines.append(
            f"  {name:<44} {m['kind']:<6} stocks {m['stocks']:>3} top {m['top_k']:>2}:"
            f" {_pct(p['model']['annual_return'])} vs {_pct(p['equal_weight']['annual_return'])};"
            f" beats {'yes' if m['walk_forward']['beats_baseline'] else 'no'};"
            f" holdout beats {'yes' if m['holdout']['beats_baseline'] else 'no'}"
        )
    if example:
        lines += [
            "",
            f"Example reasons ({example.get('symbol')} on {example.get('trade_date')},"
            f" {example.get('model')},"
            f" P(up) {example.get('prob_up', 0):.2f}):",
            *[f"  - {r}" for r in example.get("reasons", [])],
        ]
    lines += ["", "Known biases:", *[f"  - {b}" for b in report["biases"]]]
    return lines


# --- entry point -------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """Entry point for ``python -m market_intel.models``."""
    parser = argparse.ArgumentParser(prog="python -m market_intel.models")
    sub = parser.add_subparsers(dest="command", required=True)
    train = sub.add_parser("train", help="walk-forward, holdout, final retrain, registry")
    train.add_argument("--horizon", type=int, default=21, choices=[21])
    train.add_argument("--universe", default="nifty500")
    train.add_argument("--min-sector-rows", type=int, default=MIN_SECTOR_ROWS)
    train.add_argument("--min-sector-stocks", type=int, default=MIN_SECTOR_STOCKS)
    train.add_argument("--top-k", type=int, default=TOP_K)
    train.add_argument("--first-year", type=int, default=ev.FIRST_TEST_YEAR)
    sub.add_parser("report", help="print the last training report")
    ui = sub.add_parser("ui", help="start the MLflow UI on 127.0.0.1")
    ui.add_argument("--port", type=int, default=5000)
    args = parser.parse_args(argv)
    try:
        settings = get_settings()
    except ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 1
    configure_logging(settings, COMPONENT)
    if args.command == "report":
        path = settings.data_dir / "models" / "last_report.json"
        if not path.exists():
            print("No training report yet: run `models train --horizon 21` first.", file=sys.stderr)
            return 1
        data = json.loads(path.read_text(encoding="utf-8"))
        print("\n".join(report_lines(data, data.get("example"))))
        return 0
    if args.command == "ui":
        cmd = [
            sys.executable,
            "-m",
            "mlflow",
            "ui",
            "--backend-store-uri",
            tracking_uri(settings),
            "--host",
            "127.0.0.1",
            "--port",
            str(args.port),
        ]
        print(f"MLflow UI on http://127.0.0.1:{args.port} (Ctrl+C to stop)")
        return subprocess.call(cmd)
    with bound_context(run_id=new_run_id(), component=COMPONENT):
        try:
            with postgres.connect(settings.require("database")) as conn:
                params = {"horizon": args.horizon, "universe": args.universe}
                with ingestion_run(settings, JOB, source="features_daily", params=params) as run:
                    t0 = time_mod.monotonic()
                    data = load_dataset(conn, horizon=args.horizon, universe=args.universe)
                    print(
                        f"loaded {len(data.panel):,} feature rows"
                        f" in {time_mod.monotonic() - t0:.0f} s",
                        flush=True,
                    )
                    result = run_training(
                        data,
                        min_sector_rows=args.min_sector_rows,
                        min_sector_stocks=args.min_sector_stocks,
                        k=args.top_k,
                        first_year=args.first_year,
                        progress=lambda m: print(f"  {m} ...", flush=True),
                    )
                    rows = register(conn, settings, result)
                    run.add_rows(len(rows))
            print(
                f"trained in {result.seconds:.0f} s;"
                f" registered {len(rows)} candidate model versions"
            )
            print("\n".join(report_lines(result.report, result.example)))
            log.info(
                "models_trained",
                models=len(rows),
                seconds=round(result.seconds),
                beats=result.report["walk_forward"]["beats_baseline"],
            )
        except ConfigError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
