"""Training, SHAP reasons, model cards, MLflow and the registry on a SYNTHETIC dataset: the holdout
is never touched before the end, sector vs pooled models, the card fields, and registration
(local MLflow in a temporary folder, a fake database). No network."""

import io
import json
import math

import pandas as pd
import pytest
from model_fakes import dataset

from market_intel import explain
from market_intel import models as M
from market_intel.config import load_settings
from market_intel.feature_spec import NOT_FOR_TRAINING, TRAINING_FEATURES
from market_intel.logging_setup import configure_logging

DATA = dataset()


@pytest.fixture(scope="module")
def result():
    return M.run_training(DATA, min_sector_rows=500, min_sector_stocks=4)


# --- SHAP templates ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("feature", "value", "sentence"),
    [
        ("close_sma50", 0.12, "Price is 12% above its 50-day average"),
        ("close_sma50", -0.05, "Price is 5% below its 50-day average"),
        ("volume_z20", 2.3, "Volume is 2.3 standard deviations above its 20-day normal"),
        ("ret_20d", math.log(1.10), "The stock rose 10% over the last 20 days"),
        ("ret_5d", math.log(0.95), "The stock fell 5% over the last 5 days"),
        ("rsi14", 75.0, "RSI(14) is 75 (overbought zone)"),
        ("rsi14", 50.0, "RSI(14) is 50"),
        ("high_52w", -0.2, "Price is 20% below its 52-week high"),
        ("rs_sector_20d", 0.03, "Over 20 days the stock did 3% better than its sector"),
        ("vix_level", 14.25, "India VIX is at 14.2"),
        ("day_of_week", 0.0, "The day is a Monday"),
        ("month", 11.0, "The month is November"),
        ("price_event_5d", 1.0, "A suspected split or bonus happened in the last 5 days"),
        (
            "log_traded_value_20d",
            math.log1p(25e7),
            "Average daily traded value is about Rs 25.0 crore",
        ),
        ("sector", "Fake Sector", "Its sector is Fake Sector"),
    ],
)
def test_templates(feature, value, sentence):
    assert explain.describe(feature, value) == sentence


def test_every_training_feature_has_a_template_and_missing_values_are_said():
    assert set(TRAINING_FEATURES) <= set(explain.TEMPLATES)
    assert explain.describe("rsi14", float("nan")) == "rsi14 is unknown"


def test_reasons_are_ordered_by_size_with_direction_and_no_advice():
    row = pd.Series({"close_sma50": 0.12, "rsi14": 40.0, "vix_level": 20.0})
    reasons = explain.reasons_from_values(row, [0.05, -0.2, 0.01], k=2)
    assert reasons == [
        "RSI(14) is 40 (lowers the model's up-probability)",
        "Price is 12% above its 50-day average (raises the model's up-probability)",
    ]
    for word in ("buy", "sell", "recommend", "should"):
        assert all(word not in r.lower() for r in reasons)


# --- training ----------------------------------------------------------------------------


def test_the_holdout_is_never_used_before_the_end(monkeypatch):
    seen = []
    real = M.train_and_predict

    def spy(data, train_rows, test_rows, sector_models, sectors, label):
        seen.append(
            (
                label,
                data.panel.loc[train_rows, "trade_date"].max(),
                data.panel.loc[train_rows, "label_end"].dropna().max(),
            )
        )
        return real(data, train_rows, test_rows, sector_models, sectors, label)

    monkeypatch.setattr(M, "train_and_predict", spy)
    res = M.run_training(DATA, min_sector_rows=500, min_sector_stocks=4)
    labels = [s[0] for s in seen]
    assert labels[-2:] == ["holdout", "final"]  # holdout evaluated once, after every fold
    for label, last_day, last_end in seen[:-1]:  # every fold and the holdout model itself
        if last_day is not None and not pd.isna(last_day):
            assert last_end < res.holdout_from, label
    assert all(f.test_end < res.holdout_from for f in res.folds)


def test_news_features_are_never_trained_on(result):
    for pair in result.final.values():
        names = pair.clf.booster_.feature_name()
        assert not set(NOT_FOR_TRAINING) & set(names)


def test_sector_and_pooled_models(result):
    assert result.sector_models == ["Fake Sector A", "Fake Sector B", "Fake Sector C"]
    small = M.run_training(DATA, min_sector_rows=500, min_sector_stocks=5)
    assert small.sector_models == [] and len(small.pooled_sectors) == 3
    assert "sector" in small.final[M.POOLED].clf.booster_.feature_name()
    assert small.report["models"]["lgbm_21d_pooled"]["kind"] == "pooled"


def test_thin_stocks_are_excluded_from_training():
    panel = DATA.panel.copy()
    panel.loc[panel["instrument_id"] == 100, "history_days"] = 100.0
    thin = M.Dataset(panel, DATA.calendar, DATA.prices, DATA.nifty, {}, DATA.symbols)
    res = M.run_training(thin, min_sector_rows=500, min_sector_stocks=3)
    assert res.report["thin_stocks_excluded"] == 1
    assert all(p.stocks <= 11 for p in res.final.values())


def test_the_report_holds_every_baseline_and_a_verdict(result):
    wf = result.report["walk_forward"]
    assert set(wf["portfolio"]) == {
        "model",
        "momentum",
        "sma_rule",
        "equal_weight",
        "nifty50",
        "always_up",
    }
    assert isinstance(wf["beats_baseline"], bool) and "beats_baseline" in result.report["holdout"]
    assert set(wf["baselines_classification"]) == {"always_up", "momentum", "sma_rule"}
    assert len(result.report["biases"]) == 2
    json.dumps(result.report)  # JSON-safe (no NaN)
    lines = M.report_lines(result.report, result.example)
    assert any(line.startswith("Walk-forward portfolio after costs") for line in lines)


def test_an_example_has_five_plain_reasons(result):
    ex = result.example
    assert ex["symbol"].startswith("FAKE") and len(ex["reasons"]) == 5
    assert all("up-probability" in r for r in ex["reasons"])


# --- cards, MLflow, registry -------------------------------------------------------------


def test_model_card_fields(result):
    pair = result.final["Fake Sector A"]
    entry = result.report["models"][pair.name]
    card = M.model_card(pair.name, 3, entry, result.report, pair, result.shap[pair.name])
    beats = "yes" if entry["walk_forward"]["beats_baseline"] else "no"
    assert card.startswith(f"# Model card: {pair.name} v3")
    assert f"**Beats the baselines after costs (walk-forward): {beats}.**" in card
    for text in (
        "Status: candidate",
        "Survivorship bias",
        "Raw prices",
        "Thin data",
        "Holdout",
        "| equal_weight",
        "| nifty50",
        "Top features",
        "news counts are not used",
    ):
        assert text in card
    pooled = result.final[M.POOLED]
    assert "pooled fallback" in M.model_card(
        pooled.name,
        1,
        result.report["models"][pooled.name],
        result.report,
        pooled,
        result.shap[pooled.name],
    )


class Registry:
    def __init__(self):
        self.rows = []

    def execute(self, sql, params=()):
        if sql.startswith("SELECT coalesce(max(version)"):
            n = sum(1 for r in self.rows if r[0] == params[0])
            return type("R", (), {"fetchone": lambda s: (n + 1,)})()
        self.rows.append(params)
        return type("R", (), {"fetchone": lambda s: (len(self.rows),)})()


def test_register_writes_files_mlflow_and_candidate_rows(result, tmp_path, monkeypatch):
    monkeypatch.setenv("APP_PROFILE", "full")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    settings = load_settings()
    configure_logging(settings, "models", console_stream=io.StringIO())
    conn = Registry()
    rows = M.register(conn, settings, result)
    assert len(rows) == len(result.final) and {r["version"] for r in rows} == {1}
    models_dir = settings.data_dir / "models"
    for r in rows:
        for suffix in ("-clf.txt", "-reg.txt", "-shap.csv", ".md"):
            assert (models_dir / f"{r['name']}-v1{suffix}").exists()
    assert (models_dir / "last_report.json").exists()
    import mlflow

    mlflow.set_tracking_uri(M.tracking_uri(settings))
    exp = mlflow.get_experiment_by_name(M.EXPERIMENT)
    runs = mlflow.search_runs([exp.experiment_id])
    assert len(runs) == len(result.final) + 1  # the parent run + one per model
    assert all(r[11] for r in conn.rows)  # every row has an MLflow run id
    again = M.register(conn, settings, result, use_mlflow=False)
    assert {r["version"] for r in again} == {2}


def test_the_mlflow_ui_is_local_only(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_PROFILE", "full")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    settings = load_settings()
    calls = []
    monkeypatch.setattr(M, "get_settings", lambda: settings)
    monkeypatch.setattr(M, "configure_logging", lambda *a, **k: None)
    monkeypatch.setattr(M.subprocess, "call", lambda cmd: calls.append(cmd) or 0)
    assert M.main(["ui"]) == 0
    [cmd] = calls
    assert cmd[cmd.index("--host") + 1] == "127.0.0.1" and "0.0.0.0" not in cmd
    assert cmd[cmd.index("--backend-store-uri") + 1] == M.tracking_uri(settings)


def test_unmeasurable_counts_only_candidates():
    from datetime import date

    panel = pd.DataFrame(
        {
            "trade_date": [date(2024, 1, 1)] * 2,
            "instrument_id": [1, 2],
            "history_days": [300.0, 10.0],
        }
    )
    returns = pd.DataFrame(
        {
            "date": [date(2024, 1, 1)] * 3,
            "instrument_id": [1, 2, 3],
            "ret": [float("nan"), float("nan"), float("nan")],
        }
    )
    assert M._unmeasurable(returns, [date(2024, 1, 1)], panel) == {"stock_periods": 1, "of": 1}
