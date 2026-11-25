"""The intraday meta-model: LightGBM over the intraday features and the rule signals, one model
per side, predicting P(the target barrier is hit first) (sprint S5i; decision 2026-11-25).

It is a *meta*-model: it does not invent a new signal, it judges the ones the rules already
produce, so its job is to say "this breakout is worth taking and that one is not". Its output
is a probability, which :mod:`market_intel.sizing` turns into an expected gain after costs; the
trade only happens if that is positive.

How it is evaluated (and why it is hard to fool):

- **Monthly walk-forward.** Each month is predicted by a model trained only on earlier months,
  in a rolling window, so nothing is ever scored by a model that saw it.
- **Purge and embargo.** An intraday label never leaves its own day (everything is flat by
  15:20), so training on strictly earlier days already purges the label windows; a one-day
  embargo at each boundary covers the overnight edge.
- **A holdout that is used once.** The last months are never touched during walk-forward or
  tuning, and are scored a single time at the end.
- **Training rows are capped** so the rolling fits stay quick on a laptop; the cap samples at
  random with a fixed seed, never by recency or outcome.

Models are registered as ``candidate`` and nothing is ever promoted.
"""

from __future__ import annotations

import json
import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from market_intel.config import Settings
from market_intel.intraday import ATR_LABEL_VERSIONS, FEATURES_V1
from market_intel.intraday_strategies import STRATEGIES
from market_intel.logging_setup import get_logger

log = get_logger("market_intel.intraday_models")

EXPERIMENT = "market-intel-intraday"
SIDES = ("long", "short")
SIGNAL_FEATURES = tuple(f"signal_{s.name}" for s in STRATEGIES)
TRAINING_FEATURES: tuple[str, ...] = tuple(FEATURES_V1) + SIGNAL_FEATURES

LGBM_PARAMS: dict[str, Any] = {
    "objective": "binary",
    "n_estimators": 300,
    "learning_rate": 0.05,
    "num_leaves": 63,
    "min_child_samples": 200,
    "subsample": 0.8,
    "subsample_freq": 1,
    "colsample_bytree": 0.8,
    "reg_lambda": 1.0,
    "n_jobs": -1,
    "verbose": -1,
    "random_state": 42,
}


@dataclass(frozen=True)
class TrainConfig:
    label_version: str = "v2"
    variant: str = "k15_b10"
    window_months: int = 12  # the rolling training window
    holdout_months: int = 3
    max_train_rows: int = 500_000
    min_train_rows: int = 20_000
    seed: int = 42


DEFAULT_CONFIG = TrainConfig()


@dataclass
class FoldScore:
    month: date
    side: str
    rows: int
    train_rows: int
    auc: float | None
    base_rate: float
    mean_probability: float
    constant: bool = False  # the fit could not split and predicts one value for every row


@dataclass
class TrainResult:
    config: TrainConfig
    holdout_from: date
    folds: list[FoldScore] = field(default_factory=list)
    predictions: pd.DataFrame = field(default_factory=pd.DataFrame)  # walk-forward
    holdout_predictions: pd.DataFrame = field(default_factory=pd.DataFrame)
    final_models: dict[str, Any] = field(default_factory=dict)
    calibrators: dict[str, Any] = field(default_factory=dict)
    importances: dict[str, pd.DataFrame] = field(default_factory=dict)

    def auc(self, side: str | None = None, *, holdout: bool = False) -> float | None:
        frame = self.holdout_predictions if holdout else self.predictions
        if frame.empty:
            return None
        if side:
            frame = frame[frame["side"] == side]
        return _auc(frame["y"].to_numpy(), frame["probability"].to_numpy())

    def lines(self) -> list[str]:
        out = [
            f"intraday meta-model ({self.config.label_version}"
            f" {self.config.variant}): {len(self.folds)} monthly folds,"
            f" holdout from {self.holdout_from}"
        ]
        constant = sum(1 for f in self.folds if f.constant)
        if constant:
            out.append(
                f"  WARNING: {constant} of {len(self.folds)} folds predicted a single value"
                " (too few training rows to split); treat their AUC as meaningless"
            )
        for side in SIDES:
            wf, ho = self.auc(side), self.auc(side, holdout=True)
            rows = int((self.predictions["side"] == side).sum()) if len(self.predictions) else 0
            out.append(
                f"  {side}: walk-forward AUC {_fmt(wf)}, holdout AUC {_fmt(ho)}, rows {rows:,}"
            )
        return out


def _fmt(value: float | None) -> str:
    return "-" if value is None else f"{value:.3f}"


def _auc(y: np.ndarray, p: np.ndarray) -> float | None:
    """Area under the ROC curve, by rank (no sklearn needed)."""
    mask = ~(np.isnan(y) | np.isnan(p))
    y, p = y[mask], p[mask]
    positives, negatives = int(y.sum()), int(len(y) - y.sum())
    if positives == 0 or negatives == 0:
        return None
    order = np.argsort(p, kind="stable")
    ranks = np.empty(len(p), dtype=float)
    ranks[order] = np.arange(1, len(p) + 1)
    # average the ranks of ties so a constant prediction scores exactly 0.5
    frame = pd.DataFrame({"p": p, "r": ranks})
    ranks = frame.groupby("p")["r"].transform("mean").to_numpy()
    return float((ranks[y == 1].sum() - positives * (positives + 1) / 2) / (positives * negatives))


# --- the dataset ---------------------------------------------------------------------------


def label_columns(config: TrainConfig) -> tuple[str, str, str]:
    if config.label_version in ATR_LABEL_VERSIONS:
        return (
            f"outcome_{config.variant}",
            f"gross_return_{config.variant}",
            f"bars_held_{config.variant}",
        )
    return ("outcome", "gross_return", "bars_held")


def build_dataset(
    features: pd.DataFrame, labels: pd.DataFrame, signals: pd.DataFrame, config: TrainConfig
) -> pd.DataFrame:
    """One row per (decision point, side): the features, the rule signals and y = target first.

    Rows the labels excluded never enter the dataset, so the model is never trained or scored
    on a day the labels refused to measure.
    """
    outcome_col, return_col, bars_col = label_columns(config)
    base = features[["instrument_id", "trade_date", "ts", "minute", *FEATURES_V1]].copy()
    for name in signals.columns:
        base[f"signal_{name}"] = signals[name].to_numpy()
    keep = ["instrument_id", "ts", "side", "entry_price", "excluded_reason",
            outcome_col, return_col, bars_col]  # fmt: skip
    if config.label_version in ATR_LABEL_VERSIONS:
        keep.append("atr")
    merged = base.merge(labels[keep], on=["instrument_id", "ts"], how="inner")
    merged = merged[merged["excluded_reason"].isna()].drop(columns=["excluded_reason"])
    merged = merged.rename(
        columns={outcome_col: "outcome", return_col: "gross_return", bars_col: "bars_held"}
    )
    merged["y"] = (merged["outcome"] == 1).astype("int8")
    return merged


def months_of(frame: pd.DataFrame) -> list[date]:
    days = pd.to_datetime(frame["trade_date"])
    return sorted({date(d.year, d.month, 1) for d in days})


def holdout_start(months: Sequence[date], holdout_months: int) -> date:
    """The first month of the holdout; everything before it is walk-forward territory."""
    if len(months) <= holdout_months:
        return months[-1]
    return months[-holdout_months]


def month_of(day: date) -> date:
    return date(day.year, day.month, 1)


def train_window(frame: pd.DataFrame, test_month: date, config: TrainConfig) -> pd.DataFrame:
    """The rows allowed to train for ``test_month``: earlier days only, with a one-day embargo
    before the test month, limited to the rolling window."""
    months = pd.Series([month_of(d) for d in frame["trade_date"]], index=frame.index)
    window_start = (pd.Timestamp(test_month) - pd.DateOffset(months=config.window_months)).date()
    embargo = (pd.Timestamp(test_month) - pd.Timedelta(days=1)).date()
    mask = (months >= month_of(window_start)) & (frame["trade_date"] < embargo)
    return frame[mask]


def _sample(frame: pd.DataFrame, limit: int, seed: int) -> pd.DataFrame:
    if len(frame) <= limit:
        return frame
    return frame.sample(limit, random_state=seed)


# --- training -------------------------------------------------------------------------------


def fit(train: pd.DataFrame, params: dict[str, Any] | None = None) -> Any:
    from lightgbm import LGBMClassifier

    model = LGBMClassifier(**{**LGBM_PARAMS, **(params or {})})
    model.fit(train[list(TRAINING_FEATURES)], train["y"])
    return model


def run_training(
    dataset: pd.DataFrame,
    config: TrainConfig = DEFAULT_CONFIG,
    *,
    progress: Any = lambda message: None,
) -> TrainResult:
    """Monthly walk-forward per side, then one holdout scoring and a final model per side."""
    months = months_of(dataset)
    if not months:
        raise ValueError("the dataset is empty")
    first_holdout = holdout_start(months, config.holdout_months)
    result = TrainResult(config=config, holdout_from=first_holdout)
    walk_months = [m for m in months if m < first_holdout]
    predictions: list[pd.DataFrame] = []
    for side in SIDES:
        side_rows = dataset[dataset["side"] == side]
        for test_month in walk_months:
            test = side_rows[[month_of(d) == test_month for d in side_rows["trade_date"]]]
            if test.empty:
                continue
            train = train_window(side_rows, test_month, config)
            if len(train) < config.min_train_rows:
                continue
            train = _sample(train, config.max_train_rows, config.seed)
            progress(f"{side} {test_month:%Y-%m} (train {len(train):,})")
            model = fit(train)
            probability = model.predict_proba(test[list(TRAINING_FEATURES)])[:, 1]
            constant = bool(np.ptp(probability) < 1e-9)
            if constant:
                log.warning(
                    "intraday_fold_constant",
                    month=str(test_month),
                    side=side,
                    train_rows=len(train),
                    hint="too few training rows for min_child_samples to allow a split",
                )
            scored = test[["instrument_id", "trade_date", "ts", "minute", "side", "y"]].copy()
            scored["probability"] = probability
            predictions.append(scored)
            result.folds.append(
                FoldScore(
                    month=test_month,
                    side=side,
                    rows=len(test),
                    train_rows=len(train),
                    auc=_auc(test["y"].to_numpy(), probability),
                    base_rate=float(test["y"].mean()),
                    mean_probability=float(probability.mean()),
                    constant=constant,
                )
            )
    result.predictions = (
        pd.concat(predictions, ignore_index=True) if predictions else pd.DataFrame()
    )

    # the holdout: trained on everything before it, scored exactly once
    holdout_rows: list[pd.DataFrame] = []
    for side in SIDES:
        side_rows = dataset[dataset["side"] == side]
        test = side_rows[[month_of(d) >= first_holdout for d in side_rows["trade_date"]]]
        train = train_window(side_rows, first_holdout, config)
        if test.empty or len(train) < config.min_train_rows:
            continue
        progress(f"{side} holdout (train {len(train):,})")
        model = fit(_sample(train, config.max_train_rows, config.seed))
        probability = model.predict_proba(test[list(TRAINING_FEATURES)])[:, 1]
        scored = test[["instrument_id", "trade_date", "ts", "minute", "side", "y"]].copy()
        scored["probability"] = probability
        holdout_rows.append(scored)
        result.final_models[side] = model
        result.importances[side] = pd.DataFrame(
            {"feature": list(TRAINING_FEATURES), "gain": model.booster_.feature_importance("gain")}
        ).sort_values("gain", ascending=False, ignore_index=True)
    result.holdout_predictions = (
        pd.concat(holdout_rows, ignore_index=True) if holdout_rows else pd.DataFrame()
    )
    log.info(
        "intraday_model_trained",
        folds=len(result.folds),
        walk_forward_auc=result.auc(),
        holdout_auc=result.auc(holdout=True),
    )
    return result


# --- MLflow and the registry -----------------------------------------------------------------


def mlflow_setup(settings: Settings) -> Any:
    import mlflow

    root = settings.data_dir / "mlflow"
    root.mkdir(parents=True, exist_ok=True)
    mlflow.set_tracking_uri("sqlite:///" + (root / "mlflow.db").resolve().as_posix())
    if mlflow.get_experiment_by_name(EXPERIMENT) is None:
        mlflow.create_experiment(EXPERIMENT, artifact_location=(root / "artifacts").as_uri())
    mlflow.set_experiment(EXPERIMENT)
    return mlflow


def model_card(name: str, version: int, result: TrainResult, side: str) -> str:
    wf, ho = result.auc(side), result.auc(side, holdout=True)
    top = result.importances.get(side)
    lines = [
        f"# {name} v{version}",
        "",
        f"- Trained: {datetime.now(UTC):%Y-%m-%d %H:%M UTC}",
        f"- Side: {side}; predicts P(target barrier first)",
        f"- Labels: {result.config.label_version} {result.config.variant}"
        f" (target = k x ATR, stop = k/2 x ATR)",
        f"- Walk-forward AUC: {_fmt(wf)} ({len(result.folds)} monthly folds)",
        f"- Holdout AUC: {_fmt(ho)} (from {result.holdout_from}, scored once)",
        "- Status: candidate. Nothing is promoted; the project never places orders.",
        "",
        "## What it is not",
        "",
        "A probability, not a promise. Intraday trading is a negative-sum game after costs;",
        "SEBI found 71% of individual intraday traders in the cash segment lost money in",
        "FY2023. Prices are unadjusted and the backtest assumes the modelled slippage.",
        "",
        "## Top features by gain",
        "",
    ]
    if top is not None:
        lines += [
            f"{i + 1}. {r.feature} ({r.gain:,.0f})" for i, r in enumerate(top.head(10).itertuples())
        ]
    return "\n".join(lines) + "\n"


def register(
    conn: Any,
    settings: Settings,
    result: TrainResult,
    *,
    use_mlflow: bool = True,
) -> list[dict[str, Any]]:
    """Save each side's final model and card, log to MLflow, insert candidate rows."""
    models_dir = settings.data_dir / "models"
    models_dir.mkdir(parents=True, exist_ok=True)
    mlflow = mlflow_setup(settings) if use_mlflow else None
    rows: list[dict[str, Any]] = []
    parent = (
        mlflow.start_run(run_name=f"intraday-{datetime.now(UTC):%Y%m%dT%H%M%SZ}")
        if mlflow
        else None
    )
    try:
        if mlflow:
            mlflow.log_params(
                {
                    "label_version": result.config.label_version,
                    "variant": result.config.variant,
                    "window_months": result.config.window_months,
                    "holdout_from": str(result.holdout_from),
                    "folds": len(result.folds),
                    **{f"lgbm_{k}": v for k, v in LGBM_PARAMS.items()},
                }
            )
        for side, model in result.final_models.items():
            name = f"lgbm_intraday_{side}"
            version = conn.execute(
                "SELECT coalesce(max(version), 0) + 1 FROM model_versions WHERE name = %s",
                (name,),
            ).fetchone()[0]
            stem = models_dir / f"{name}-v{version}"
            model.booster_.save_model(f"{stem}-clf.txt")
            result.importances[side].to_csv(f"{stem}-importance.csv", index=False)
            card = Path(f"{stem}.md")
            card.write_text(model_card(name, version, result, side), encoding="utf-8")
            metrics = {
                "walk_forward": {"auc": result.auc(side), "folds": len(result.folds)},
                "holdout": {"auc": result.auc(side, holdout=True)},
                "label_version": result.config.label_version,
                "variant": result.config.variant,
            }
            run_id = None
            if mlflow:
                with mlflow.start_run(run_name=f"{name}-v{version}", nested=True) as child:
                    run_id = child.info.run_id
                    mlflow.log_metric("walk_forward_auc", result.auc(side) or 0.0)
                    mlflow.log_metric("holdout_auc", result.auc(side, holdout=True) or 0.0)
                    mlflow.log_artifact(str(card))
            days = sorted(result.predictions["trade_date"]) if len(result.predictions) else []
            conn.execute(
                "INSERT INTO model_versions (name, version, horizon, sector, kind, train_from,"
                " train_to, holdout_from, metrics, beats_baseline, status, mlflow_run_id,"
                " card_path) VALUES (%s, %s, %s, NULL, 'intraday', %s, %s, %s, %s::jsonb, false,"
                " 'candidate', %s, %s)",
                (
                    name,
                    version,
                    int(result.config.variant.split("_b")[1]) if result.config.variant else 15,
                    days[0] if days else result.holdout_from,
                    days[-1] if days else result.holdout_from,
                    result.holdout_from,
                    json.dumps(metrics, default=str),
                    run_id,
                    str(card.relative_to(settings.data_dir)),
                ),
            )
            rows.append({"name": name, "version": version, "side": side, "metrics": metrics})
    finally:
        if parent is not None:
            mlflow.end_run()
    return rows


def probabilities_for(
    result: TrainResult, *, holdout: bool = False, calibrated: bool = True
) -> pd.DataFrame:
    """The walk-forward (or holdout) probabilities, ready to join onto a backtest frame.

    Once :func:`calibrate` has run, the calibrated probability is what callers get: it is the
    number the expected-value gate should trust. Pass ``calibrated=False`` for the raw one.
    """
    frame = result.holdout_predictions if holdout else result.predictions
    if frame.empty:
        return frame
    column = (
        "probability_calibrated"
        if calibrated and "probability_calibrated" in frame
        else "probability"
    )
    out = frame[["instrument_id", "ts", "side", column]].copy()
    return out.rename(columns={column: "probability"})


def base_rates(dataset: pd.DataFrame) -> dict[str, dict[str, float]]:
    """How often each barrier came first, per side: the odds a rule has without a model."""
    out: dict[str, dict[str, float]] = {}
    for side in SIDES:
        part = dataset[dataset["side"] == side]
        if part.empty:
            continue
        n = len(part)
        out[side] = {
            "target": float((part["outcome"] == 1).sum() / n),
            "stop": float((part["outcome"] == -1).sum() / n),
            "time_limit_return": float(part.loc[part["outcome"] == 0, "gross_return"].mean() or 0.0)
            if (part["outcome"] == 0).any()
            else 0.0,
        }
    return out


def annualise(total_return_pct: float, days: int) -> float:
    """A total return over ``days`` trading days as an annual rate."""
    if days <= 0:
        return 0.0
    growth = 1 + total_return_pct / 100
    if growth <= 0:
        return -100.0
    return 100 * (math.pow(growth, 252 / days) - 1)


# --- confidence calibration and precision (sprint S5i-b) ----------------------------------
#
# Two questions the AUC does not answer:
#
# (a) when the model says 70%, does the target really come first about 70% of the time?
#     (calibration — a model can rank well and still be badly calibrated);
# (b) are the MOST confident "yes" signals actually profitable after costs? A model with no
#     usable edge on average can still be right about its strongest few, and that is exactly
#     the case worth knowing about before dismissing it.
#
# Everything here is printed, never stored: it describes a training run, not a decision.

DECILES = 10
TOP_SHARES = (0.10, 0.05, 0.01)


@dataclass
class Bucket:
    """One row of a calibration or precision table."""

    label: str
    trades: int
    mean_probability: float
    target_rate: float  # how often the target really came first
    win_rate: float  # how often the trade made money AFTER costs
    mean_net_bps: float  # average net result per trade, in bps of the position
    mean_gross_bps: float

    def line(self) -> str:
        return (
            f"  {self.label:<12} {self.trades:>8,} {self.mean_probability:>9.1%}"
            f" {self.target_rate:>10.1%} {self.win_rate:>9.1%}"
            f" {self.mean_gross_bps:>+11.2f} {self.mean_net_bps:>+9.2f}"
        )


BUCKET_HEADER = (
    f"  {'bucket':<12} {'trades':>8} {'said':>9} {'actual':>10} {'won Rs':>9}"
    f" {'gross bps':>11} {'net bps':>9}"
)


def _bucket(label: str, part: pd.DataFrame, cost_bps: float) -> Bucket:
    gross_bps = part["gross_return"].astype(float) * 10_000
    net_bps = gross_bps - cost_bps
    return Bucket(
        label=label,
        trades=len(part),
        mean_probability=float(part["probability"].mean()),
        target_rate=float((part["outcome"] == 1).mean()),
        win_rate=float((net_bps > 0).mean()),
        mean_net_bps=float(net_bps.mean()),
        mean_gross_bps=float(gross_bps.mean()),
    )


@dataclass
class PrecisionReport:
    """Calibration and top-confidence precision, per side."""

    cost_bps: float
    capital: float
    slippage_bps: float
    period: str = "walk_forward"
    calibration: dict[str, list[Bucket]] = field(default_factory=dict)
    tops: dict[str, list[Bucket]] = field(default_factory=dict)
    warning: str = ""

    def lines(self) -> list[str]:
        out = [
            f"confidence calibration and precision ({self.period}):"
            f" capital Rs {self.capital:,.0f}, slippage {self.slippage_bps:g} bps a side,"
            f" so a round trip costs {self.cost_bps:.2f} bps"
        ]
        if self.warning:
            out.append(f"  {self.warning}")
        for side in SIDES:
            buckets = self.calibration.get(side)
            if not buckets:
                continue
            out += ["", f"{side}: does the probability mean anything?", BUCKET_HEADER]
            out += [b.line() for b in buckets]
            tops = self.tops.get(side) or []
            if tops:
                out += [f"{side}: the most confident signals", BUCKET_HEADER]
                out += [b.line() for b in tops]
                best = max(tops, key=lambda b: b.mean_net_bps)
                verdict = (
                    f"net positive at {best.label} ({best.mean_net_bps:+.2f} bps a trade)"
                    if best.mean_net_bps > 0
                    else "every confidence band loses money after costs"
                )
                out.append(f"  -> {side}: {verdict}")
        return out


def precision_report(
    dataset: pd.DataFrame,
    predictions: pd.DataFrame,
    *,
    capital: float,
    costs: Any = None,
    slippage_bps: float = 5.0,
    period: str = "walk_forward",
    deciles: int = DECILES,
    shares: Sequence[float] = TOP_SHARES,
    warning: str = "",
) -> PrecisionReport:
    """Calibration deciles and the top ``shares`` most-confident signals, per side.

    The net result per trade is the realised gross return minus the full round-trip cost at
    ``capital`` of notional (the backtest's exposure cap), so it is comparable with the
    backtest's ``edge bps`` and independent of position size.
    """
    from market_intel.costs import DEFAULT_INTRADAY_COSTS

    costs = costs or DEFAULT_INTRADAY_COSTS
    cost_bps = costs.breakeven_fraction(capital, slippage_bps) * 10_000
    report = PrecisionReport(
        cost_bps=cost_bps,
        capital=capital,
        slippage_bps=slippage_bps,
        period=period,
        warning=warning,
    )
    if predictions.empty or dataset.empty:
        return report
    # the probability comes from the predictions; drop any stale copy so the merge cannot
    # produce probability_x / probability_y
    base = dataset.drop(columns=["probability"], errors="ignore")
    joined = base.merge(
        predictions[["instrument_id", "ts", "side", "probability"]],
        on=["instrument_id", "ts", "side"],
        how="inner",
    )
    for side in SIDES:
        part = joined[joined["side"] == side]
        if part.empty:
            continue
        ranked = part.sort_values("probability", kind="stable")
        groups: list[Bucket] = []
        # equal-count deciles by probability, lowest first
        edges = np.linspace(0, len(ranked), deciles + 1).astype(int)
        for i in range(deciles):
            chunk = ranked.iloc[edges[i] : edges[i + 1]]
            if not chunk.empty:
                groups.append(_bucket(f"decile {i + 1}", chunk, cost_bps))
        report.calibration[side] = groups
        tops: list[Bucket] = []
        for share in shares:
            n = max(1, int(round(len(ranked) * share)))
            tops.append(_bucket(f"top {share:.0%}", ranked.iloc[-n:], cost_bps))
        report.tops[side] = tops
    return report


# --- probability calibration (sprint S5i-c) ------------------------------------------------
#
# S5i-b showed the raw LightGBM probabilities rank well but are badly overconfident where it
# matters: the top 1% said 57.5% and delivered 33.6%. The expected-value gate multiplies that
# probability by the target distance, so an overconfident number makes losing trades look
# worth taking. Isotonic regression fixes the LEVEL without touching the ORDER: it is the
# weakest assumption that can repair calibration (monotone, non-parametric), so it cannot
# invent an edge that the ranking does not already contain.
#
# It is fitted ONLY on walk-forward out-of-fold predictions — never on the holdout, and never
# on a fold's own test rows.


@dataclass
class Calibrator:
    """A monotone map from raw probability to calibrated probability, for one side."""

    knots_x: np.ndarray
    knots_y: np.ndarray
    rows: int

    def apply(self, probability: Any) -> np.ndarray:
        values = np.asarray(probability, dtype=float)
        if len(self.knots_x) < 2:
            return values
        return np.interp(values, self.knots_x, self.knots_y)


def fit_calibrator(frame: pd.DataFrame, *, min_rows: int = 500) -> Calibrator | None:
    """Isotonic regression of the realised outcome on the raw probability."""
    if len(frame) < min_rows or frame["y"].nunique() < 2:
        return None
    from sklearn.isotonic import IsotonicRegression

    model = IsotonicRegression(y_min=0.0, y_max=1.0, out_of_bounds="clip")
    x = frame["probability"].to_numpy(dtype=float)
    model.fit(x, frame["y"].to_numpy(dtype=float))
    grid = np.unique(np.quantile(x, np.linspace(0, 1, 201)))
    return Calibrator(knots_x=grid, knots_y=model.predict(grid), rows=len(frame))


def calibration_error(frame: pd.DataFrame, column: str = "probability", buckets: int = 10) -> float:
    """Expected calibration error: the count-weighted mean gap between what the model said and
    what happened, over equal-count buckets. 0 is perfect; 0.09 means "off by 9 points"."""
    if frame.empty or frame["y"].nunique() < 2:
        return float("nan")
    ranked = frame.sort_values(column, kind="stable")
    edges = np.linspace(0, len(ranked), buckets + 1).astype(int)
    total, gap = 0, 0.0
    for i in range(buckets):
        chunk = ranked.iloc[edges[i] : edges[i + 1]]
        if chunk.empty:
            continue
        gap += len(chunk) * abs(chunk[column].mean() - chunk["y"].mean())
        total += len(chunk)
    return gap / total if total else float("nan")


def calibrate(result: TrainResult, *, min_rows: int = 500) -> dict[str, Calibrator]:
    """Fit one calibrator per side on the walk-forward predictions and add the calibrated
    column to both the walk-forward and the holdout predictions."""
    fitted: dict[str, Calibrator] = {}
    if result.predictions.empty:
        return fitted
    for side in SIDES:
        part = result.predictions[result.predictions["side"] == side]
        model = fit_calibrator(part, min_rows=min_rows)
        if model is not None:
            fitted[side] = model
    for frame in (result.predictions, result.holdout_predictions):
        if frame.empty:
            continue
        calibrated = frame["probability"].to_numpy(dtype=float).copy()
        for side, model in fitted.items():
            mask = (frame["side"] == side).to_numpy()
            if mask.any():
                calibrated[mask] = model.apply(calibrated[mask])
        frame["probability_raw"] = frame["probability"]
        frame["probability_calibrated"] = calibrated
    result.calibrators = fitted
    return fitted


def calibration_lines(result: TrainResult) -> list[str]:
    """Before and after, per side, so the fix can be checked rather than assumed."""
    out = ["probability calibration (isotonic, fitted on the walk-forward only):"]
    for side in SIDES:
        for period, frame in (
            ("walk-forward", result.predictions),
            ("holdout", result.holdout_predictions),
        ):
            if frame.empty or "probability_calibrated" not in frame:
                continue
            part = frame[frame["side"] == side]
            if part.empty:
                continue
            before = calibration_error(part, "probability_raw")
            after = calibration_error(part, "probability_calibrated")
            top = part.nlargest(max(1, len(part) // 100), "probability_raw")
            out.append(
                f"  {side:<5} {period:<12} error {before:.3f} -> {after:.3f};"
                f" top 1% said {top['probability_raw'].mean():.1%} ->"
                f" {top['probability_calibrated'].mean():.1%}, actual {top['y'].mean():.1%}"
            )
    if len(out) == 1:
        out.append("  not enough rows to calibrate")
    return out


# --- caching the walk-forward probabilities -------------------------------------------------


def cache_dir(settings: Settings, universe: str, version: str, variant: str) -> Path:
    return settings.data_dir / "predictions" / "intraday" / universe / f"{version}_{variant}"


def save_predictions(
    settings: Settings, result: TrainResult, *, universe: str, config: TrainConfig
) -> list[Path]:
    """Store the walk-forward and holdout probabilities so `precision` need not retrain.

    These are model output about past days, never committed (DATA_DIR is git-ignored).
    """
    folder = cache_dir(settings, universe, config.label_version, config.variant)
    folder.mkdir(parents=True, exist_ok=True)
    written = []
    for name, frame in (
        ("walk_forward", result.predictions),
        ("holdout", result.holdout_predictions),
    ):
        if frame.empty:
            continue
        path = folder / f"{name}.parquet"
        frame.to_parquet(path, index=False, compression="zstd")
        written.append(path)
    (folder / "holdout_from.txt").write_text(str(result.holdout_from), encoding="utf-8")
    return written


def load_predictions(
    settings: Settings, *, universe: str, version: str, variant: str, holdout: bool = False
) -> pd.DataFrame:
    """Read cached probabilities back; an empty frame when nothing was cached."""
    path = cache_dir(settings, universe, version, variant) / (
        "holdout.parquet" if holdout else "walk_forward.parquet"
    )
    if not path.exists():
        return pd.DataFrame()
    frame = pd.read_parquet(path)
    frame["trade_date"] = pd.to_datetime(frame["trade_date"]).dt.date
    return frame
