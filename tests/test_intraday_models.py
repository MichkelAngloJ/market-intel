"""The intraday meta-model (S5i): the dataset, the monthly walk-forward with purge and embargo,
the once-only holdout, and the AUC helper. Tiny synthetic frames; LightGBM only where needed."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from market_intel import intraday_models as M
from market_intel.intraday import FEATURES_V1
from market_intel.intraday_strategies import STRATEGIES

CONFIG = M.TrainConfig(label_version="v2", variant="k15_b10", window_months=12, holdout_months=3)


def business_days(start: date, n: int) -> list[date]:
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def dataset(days: int = 260, seed: int = 7) -> pd.DataFrame:
    """A dataset with a real (if weak) signal: y follows one feature plus noise."""
    rng = np.random.default_rng(seed)
    rows = []
    for day in business_days(date(2024, 1, 1), days):
        for minute in range(15, 115, 10):
            for side in M.SIDES:
                edge = rng.normal()
                rows.append(
                    {
                        "instrument_id": 1,
                        "trade_date": day,
                        "ts": datetime(day.year, day.month, day.day, 4, 0, tzinfo=UTC)
                        + timedelta(minutes=minute),
                        "minute": minute,
                        "side": side,
                        "entry_price": 100.0,
                        "atr": 1.0,
                        "outcome": 1 if edge + rng.normal(0, 0.5) > 0.5 else -1,
                        "gross_return": 0.01,
                        "bars_held": 5,
                        "y": 0,
                        **{f: float(rng.normal()) for f in FEATURES_V1},
                        **{f"signal_{s.name}": 0 for s in STRATEGIES},
                    }
                )
                rows[-1]["ret_5m"] = edge  # the feature the outcome follows
                rows[-1]["y"] = 1 if rows[-1]["outcome"] == 1 else 0
    return pd.DataFrame(rows)


# --- the dataset ---------------------------------------------------------------------------


def test_training_features_are_the_features_plus_the_rule_signals():
    assert len(M.TRAINING_FEATURES) == len(FEATURES_V1) + len(STRATEGIES)
    assert set(FEATURES_V1) <= set(M.TRAINING_FEATURES)
    assert "signal_orb_15" in M.TRAINING_FEATURES
    # no label-ish name may be a training feature
    for bad in ("outcome", "gross_return", "bars_held", "y"):
        assert bad not in M.TRAINING_FEATURES


def test_label_columns_follow_the_version():
    assert M.label_columns(CONFIG) == (
        "outcome_k15_b10", "gross_return_k15_b10", "bars_held_k15_b10",
    )  # fmt: skip
    assert M.label_columns(M.TrainConfig(label_version="v1")) == (
        "outcome", "gross_return", "bars_held",
    )  # fmt: skip


def test_build_dataset_joins_both_sides_and_drops_excluded_rows():
    ts = datetime(2024, 3, 5, 4, 0, tzinfo=UTC)
    features = pd.DataFrame([
        {"instrument_id": 1, "trade_date": date(2024, 3, 5), "ts": ts, "minute": 60,
         **{f: 0.0 for f in FEATURES_V1}},
    ])  # fmt: skip
    labels = pd.DataFrame([
        {"instrument_id": 1, "ts": ts, "side": "long", "entry_price": 100.0, "atr": 1.0,
         "excluded_reason": None, "outcome_k15_b10": 1, "gross_return_k15_b10": 0.01,
         "bars_held_k15_b10": 3},
        {"instrument_id": 1, "ts": ts, "side": "short", "entry_price": 100.0, "atr": 1.0,
         "excluded_reason": "thin_day", "outcome_k15_b10": None,
         "gross_return_k15_b10": None, "bars_held_k15_b10": None},
    ])  # fmt: skip
    signals = pd.DataFrame({s.name: [0] for s in STRATEGIES})
    built = M.build_dataset(features, labels, signals, CONFIG)
    assert built["side"].tolist() == ["long"]  # the excluded short is gone
    assert built["y"].tolist() == [1]
    assert "excluded_reason" not in built.columns
    assert set(M.TRAINING_FEATURES) <= set(built.columns)


def test_base_rates_report_how_often_each_barrier_came_first():
    frame = pd.DataFrame({
        "side": ["long"] * 4,
        "outcome": [1, -1, -1, 0],
        "gross_return": [0.01, -0.005, -0.005, 0.002],
    })  # fmt: skip
    rates = M.base_rates(frame)["long"]
    assert rates["target"] == pytest.approx(0.25)
    assert rates["stop"] == pytest.approx(0.5)
    assert rates["time_limit_return"] == pytest.approx(0.002)


# --- folds, purge, embargo, holdout -----------------------------------------------------------


def test_the_holdout_is_the_last_months():
    months = [date(2024, m, 1) for m in range(1, 13)]
    assert M.holdout_start(months, 3) == date(2024, 10, 1)
    assert M.holdout_start(months, 1) == date(2024, 12, 1)
    assert M.holdout_start([date(2024, 1, 1)], 3) == date(2024, 1, 1)  # too short to split


def test_training_stops_a_day_before_the_test_month():
    frame = dataset(days=200)
    test_month = date(2024, 6, 1)
    train = M.train_window(frame, test_month, CONFIG)
    assert train["trade_date"].max() < date(2024, 5, 31)  # the embargo day is excluded
    assert all(d < test_month for d in train["trade_date"])


def test_the_rolling_window_limits_how_far_back_training_reaches():
    frame = dataset(days=400)
    narrow = M.train_window(frame, date(2025, 1, 1), M.TrainConfig(window_months=2))
    wide = M.train_window(frame, date(2025, 1, 1), M.TrainConfig(window_months=12))
    assert narrow["trade_date"].min() > wide["trade_date"].min()
    assert narrow["trade_date"].min() >= date(2024, 11, 1)


def test_the_holdout_never_appears_in_any_training_window():
    frame = dataset(days=300)
    months = M.months_of(frame)
    first_holdout = M.holdout_start(months, 3)
    for test_month in [m for m in months if m < first_holdout]:
        train = M.train_window(frame, test_month, CONFIG)
        assert all(d < first_holdout for d in train["trade_date"])
    # and the holdout's own model trains only on earlier days
    holdout_train = M.train_window(frame, first_holdout, CONFIG)
    assert all(d < first_holdout for d in holdout_train["trade_date"])


# --- the AUC helper ------------------------------------------------------------------------


def test_auc_of_a_perfect_a_random_and_a_constant_prediction():
    y = np.array([0, 0, 1, 1], dtype=float)
    assert M._auc(y, np.array([0.1, 0.2, 0.8, 0.9])) == pytest.approx(1.0)
    assert M._auc(y, np.array([0.9, 0.8, 0.2, 0.1])) == pytest.approx(0.0)
    assert M._auc(y, np.array([0.5, 0.5, 0.5, 0.5])) == pytest.approx(0.5)
    assert M._auc(np.array([1.0, 1.0]), np.array([0.3, 0.7])) is None  # one class only


def test_annualising_a_return():
    assert M.annualise(10.0, 252) == pytest.approx(10.0)
    assert M.annualise(0.0, 252) == pytest.approx(0.0)
    assert M.annualise(-100.0, 100) == pytest.approx(-100.0)  # a wipeout stays a wipeout
    assert M.annualise(5.0, 0) == 0.0


# --- a tiny real training run ----------------------------------------------------------------


@pytest.fixture(scope="module")
def trained():
    config = M.TrainConfig(
        label_version="v2", variant="k15_b10", window_months=3, holdout_months=2,
        max_train_rows=5_000, min_train_rows=100,
    )  # fmt: skip
    return M.run_training(dataset(days=260), config)


def test_a_tiny_training_run_scores_every_month_once(trained):
    assert trained.folds, "no folds ran"
    assert {f.side for f in trained.folds} == set(M.SIDES)
    scored = trained.predictions
    assert not scored.empty
    # every walk-forward row is scored exactly once
    assert not scored.duplicated(subset=["instrument_id", "ts", "side"]).any()
    assert scored["probability"].between(0, 1).all()
    assert all(d < trained.holdout_from for d in scored["trade_date"])


def test_the_model_learns_the_planted_signal(trained):
    auc = trained.auc()
    assert auc is not None and auc > 0.6  # the outcome follows ret_5m by construction
    top = trained.importances["long"]
    assert top.iloc[0]["feature"] == "ret_5m"
    # the first folds have too little history to split; the rest must be real models
    grown = [f for f in trained.folds if f.train_rows >= 600]
    assert grown and not any(f.constant for f in grown)


def test_a_fold_with_too_few_rows_to_split_is_flagged_not_hidden():
    """LightGBM's min_child_samples can exceed a small fold, making every tree one leaf: the
    AUC then looks like 0.5 for a reason that has nothing to do with the market."""
    config = M.TrainConfig(
        label_version="v2", variant="k15_b10", window_months=1, holdout_months=2,
        max_train_rows=5_000, min_train_rows=10,
    )  # fmt: skip
    result = M.run_training(dataset(days=120), config)
    flagged = [f for f in result.folds if f.constant]
    assert flagged, "a one-month window should be too small to split"
    assert "predicted a single value" in " ".join(result.lines())


def test_the_holdout_is_scored_once_and_kept_apart(trained):
    holdout = trained.holdout_predictions
    assert not holdout.empty
    assert all(d >= trained.holdout_from for d in holdout["trade_date"])
    assert not holdout.duplicated(subset=["instrument_id", "ts", "side"]).any()
    # the walk-forward and the holdout never overlap
    keys = set(map(tuple, trained.predictions[["ts", "side"]].to_numpy()))
    assert not keys & set(map(tuple, holdout[["ts", "side"]].to_numpy()))


def test_the_report_lines_name_both_sides_and_the_holdout(trained):
    lines = "\n".join(trained.lines())
    assert "long:" in lines and "short:" in lines
    assert "holdout AUC" in lines and str(trained.holdout_from) in lines


def test_a_model_card_states_the_status_and_the_warning(trained):
    card = M.model_card("lgbm_intraday_long", 1, trained, "long")
    assert "candidate" in card and "never places orders" in card
    assert "71%" in card  # the SEBI finding
    assert "ret_5m" in card


def test_an_empty_dataset_is_refused():
    with pytest.raises(ValueError, match="empty"):
        M.run_training(pd.DataFrame({"trade_date": []}))
