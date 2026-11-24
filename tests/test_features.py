"""Features v1 formulas, NaN handling, the timing rule, labels and exclusions, and the leakage
test, including deliberately leaky features that the test must catch. Synthetic data only."""

import math
import os
import subprocess
import sys
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import numpy as np
import pandas as pd
import pytest
from feature_fakes import business_days, candles, inputs

from market_intel import features as F
from market_intel.feature_spec import FEATURE_NAMES, LABEL_COLUMNS, NOT_FOR_TRAINING
from market_intel.features import NewsItem, build_features, build_labels, prediction_time

INPUTS = inputs()


def feats(inst=10, data=INPUTS):
    return F.features_for(data, inst)


# --- timing ------------------------------------------------------------------------------


def test_prediction_time_is_0830_ist_on_the_next_weekday():
    assert prediction_time(date(2026, 11, 20)) == datetime(2026, 11, 23, 3, 0, tzinfo=UTC)  # Fri
    assert prediction_time(date(2026, 11, 24)) == datetime(2026, 11, 25, 3, 0, tzinfo=UTC)


# --- formulas ----------------------------------------------------------------------------


def test_return_volatility_trend_and_gap_formulas():
    c = INPUTS.candles[10]
    f = feats()
    t = c.index[260]
    close = c["close"]
    i = c.index.get_loc(t)
    assert f.loc[t, "ret_5d"] == pytest.approx(math.log(close.iloc[i] / close.iloc[i - 5]))
    daily = np.log(close / close.shift(1))
    assert f.loc[t, "vol_20d"] == pytest.approx(daily.iloc[i - 19 : i + 1].std())
    assert f.loc[t, "close_sma20"] == pytest.approx(
        close.iloc[i] / close.iloc[i - 19 : i + 1].mean() - 1
    )
    assert f.loc[t, "gap_open"] == pytest.approx(c["open"].iloc[i] / close.iloc[i - 1] - 1)
    hi = c["high"].iloc[i - 251 : i + 1].max()
    assert f.loc[t, "high_52w"] == pytest.approx(close.iloc[i] / hi - 1)
    tr = max(
        c["high"].iloc[i] - c["low"].iloc[i],
        abs(c["high"].iloc[i] - close.iloc[i - 1]),
        abs(c["low"].iloc[i] - close.iloc[i - 1]),
    )
    assert f["atr14_pct"].notna().iloc[13] and tr > 0


def test_windows_need_their_full_length():
    f = feats()
    assert f["ret_120d"].iloc[:120].isna().all() and f["ret_120d"].iloc[120:].notna().all()
    assert f["close_sma200"].iloc[198] != f["close_sma200"].iloc[198]  # NaN before 200 rows
    assert f["high_52w"].iloc[250] != f["high_52w"].iloc[250] and not pd.isna(
        f["high_52w"].iloc[251]
    )


def test_rsi_bounds_and_a_steady_rise():
    f = feats()
    rsi = f["rsi14"].dropna()
    assert ((rsi >= 0) & (rsi <= 100)).all()
    days = business_days(date(2024, 1, 1), 40)
    up = pd.DataFrame(
        {k: np.arange(1, 41, dtype=float) + 100 for k in ("open", "high", "low", "close")}
        | {"volume": 1.0},
        index=pd.Index(days),
    )
    assert (
        build_features(up, F.market_frame(None, None), pd.DataFrame(columns=["sector_r20"]))[
            "rsi14"
        ].iloc[-1]
        == 100
    )


def test_relative_strength_market_and_calendar():
    f = feats()
    t = INPUTS.candles[10].index[100]
    market = INPUTS.market()
    sector = INPUTS.sector_for(10)
    assert f.loc[t, "rs_nifty_20d"] == pytest.approx(
        f.loc[t, "ret_20d"] - market.loc[t, "mkt_ret_20d"]
    )
    members = [F.returns_20d(INPUTS.candles[k]["close"]).get(t) for k in (10, 11)]
    mean = np.nanmean([m for m in members if m is not None])
    assert sector.loc[t, "sector_r20"] == pytest.approx(mean)
    assert f.loc[t, "rs_sector_20d"] == pytest.approx(f.loc[t, "ret_20d"] - mean)
    assert f.loc[t, "vix_level"] == pytest.approx(INPUTS.vix.loc[t, "close"])
    assert f.loc[t, "day_of_week"] == t.weekday() and f.loc[t, "month"] == t.month


def test_flags_history_and_price_events():
    f = feats(13)
    event = INPUTS.events[13][0]
    i = INPUTS.candles[13].index.get_loc(event)
    assert list(f["price_event_5d"].iloc[i - 1 : i + 6]) == [0, 1, 1, 1, 1, 1, 0]
    assert f["history_days"].iloc[0] == 1 and f["history_days"].iloc[-1] == len(f)
    assert feats(11)["history_days"].iloc[-1] == len(INPUTS.candles[11])  # thin history kept


def test_news_counts_use_only_items_available_by_the_prediction_time():
    days = business_days(date(2024, 3, 4), 10)
    c = candles(days, 7)
    t = days[5]
    cut = prediction_time(t)
    items = [
        NewsItem("exchange", cut),
        NewsItem("exchange", cut + timedelta(seconds=1)),
        NewsItem("exchange", prediction_time(days[4])),
        NewsItem("company_news", cut - timedelta(hours=1)),
    ]
    f = build_features(
        c, F.market_frame(None, None), pd.DataFrame(columns=["sector_r20"]), news=items
    )
    assert (
        f.loc[t, "news_exchange_1d"] == 1
    )  # the one at exactly the cutoff; the earlier one is the previous window
    assert f.loc[t, "news_exchange_5d"] == 2 and f.loc[t, "news_company_5d"] == 1


def test_no_infinities_and_zero_volume_is_handled():
    c = INPUTS.candles[10].copy()
    c["volume"] = 0.0
    f = build_features(c, INPUTS.market(), INPUTS.sector_for(10))
    assert not np.isinf(f.to_numpy(dtype=float)).any()
    assert list(f.columns) == list(FEATURE_NAMES)


def test_news_features_are_marked_not_for_training():
    assert set(NOT_FOR_TRAINING) == {"news_exchange_1d", "news_exchange_5d", "news_company_5d"}


# --- labels ------------------------------------------------------------------------------


def label_frame(c, cal, **kw):
    return build_labels(c, cal, horizons=(3,), **kw).set_index("trade_date")


def test_label_is_close_after_h_days_over_the_next_open():
    days = business_days(date(2024, 1, 1), 10)
    c = candles(days, 5)
    lab = label_frame(c, days)
    t = days[2]
    expected = c.loc[days[5], "close"] / c.loc[days[3], "open"] - 1
    assert lab.loc[t, "fwd_return"] == pytest.approx(expected)
    assert lab.loc[t, "up"] == (expected > 0) and lab.loc[t, "excluded_reason"] is None


def test_label_exclusions():
    days = business_days(date(2024, 1, 1), 12)
    c = candles(days, 6).drop(index=[days[8]])  # a gap on day 8
    special = [days[4] + timedelta(days=1)]  # a special session between day 4 and day 5 (Sat)
    lab = label_frame(c, days, special=special, events=[days[2]])
    assert lab.loc[days[9], "excluded_reason"] == "beyond_data"
    assert lab.loc[days[3], "excluded_reason"] == "special_session"
    assert lab.loc[days[0], "excluded_reason"] == "price_event"  # the event day 2 is in (0, 3]
    assert lab.loc[days[6], "excluded_reason"] == "gap"  # day 8 missing in (6, 9]
    # an event on t itself is outside (t, t+h]
    only_event = label_frame(candles(days, 6), days, events=[days[2]])
    assert only_event.loc[days[2], "excluded_reason"] is None
    assert pd.isna(lab.loc[days[6], "fwd_return"]) and lab.loc[days[6], "up"] is None


def test_labels_and_features_share_no_column():
    assert not set(FEATURE_NAMES) & set(LABEL_COLUMNS)


# --- the leakage test --------------------------------------------------------------------

SAMPLES = F.pick_samples(INPUTS, 25)


def test_the_leakage_test_passes_on_the_real_builder():
    report = F.run_leakage_checks(INPUTS, SAMPLES)
    assert report.passed, report.lines()
    assert report.lines()[0] == "leakage check: PASS"


def leaky_future_close(candles_, market, sector, **kw):
    out = build_features(candles_, market, sector, **kw)
    out["ret_1d"] = np.log(candles_["close"].shift(-1) / candles_["close"])  # tomorrow: leaky!
    return out


def leaky_centered_mean(candles_, market, sector, **kw):
    out = build_features(candles_, market, sector, **kw)
    out["close_sma20"] = candles_["close"].rolling(21, center=True).mean()  # looks ahead
    return out


def leaky_news(candles_, market, sector, *, events=(), news=()):
    out = build_features(candles_, market, sector, events=events, news=news)
    out["news_exchange_5d"] = float(len(news))  # counts news from any time
    return out


@pytest.mark.parametrize("leaky", [leaky_future_close, leaky_centered_mean])
def test_a_leaky_feature_is_caught_by_truncation_and_shifted_data(leaky):
    assert F.check_truncation(INPUTS, SAMPLES, build=leaky)
    assert F.check_shifted(INPUTS, SAMPLES, build=leaky)
    assert not F.run_leakage_checks(INPUTS, SAMPLES, build=leaky).passed


def test_leaky_news_is_caught():
    assert F.check_late_news(INPUTS, SAMPLES, build=leaky_news)


def test_a_label_named_feature_or_a_builder_reading_labels_is_caught(monkeypatch):
    monkeypatch.setattr(F, "FEATURE_NAMES", (*FEATURE_NAMES, "fwd_return_lag"))
    assert any("fwd_return_lag" in f for f in F.check_names())
    monkeypatch.setattr(F, "FEATURE_NAMES", FEATURE_NAMES)

    def reads_labels(candles_, market, sector, **kw):
        return build_labels(candles_, [])  # a builder must never touch labels

    assert any("build_labels" in f for f in F.check_names(reads_labels))
    assert F.check_names() == []


def test_a_label_that_peeks_at_t_is_caught(monkeypatch):
    real = F.build_labels

    def peeking(candles_, calendar, **kw):
        out = real(candles_, calendar, **kw)
        c = candles_.sort_index()
        out["fwd_return"] = [
            c["close"].get(d, np.nan) * 1e-9 + (v if v is not None and v == v else 0)
            for d, v in zip(out["trade_date"], out["fwd_return"], strict=True)
        ]
        return out

    monkeypatch.setattr(F, "build_labels", peeking)
    assert any("past candles changed" in f for f in F.check_shifted(INPUTS, SAMPLES))


def test_truncated_inputs_hold_nothing_after_t():
    t = INPUTS.calendar[200]
    cut = INPUTS.truncated(t)
    assert all(f.index.max() <= t for f in cut.candles.values())
    assert cut.nifty.index.max() <= t and cut.calendar[-1] == t
    assert all(n.available_at <= prediction_time(t) for v in cut.news.values() for n in v)
    assert replace(INPUTS).candles is INPUTS.candles  # the original is untouched


# --- yfinance guard (S1 review answer 3) ------------------------------------------------


def test_yfinance_loads_without_curl_cffi_and_keeps_our_session():
    code = (
        "from market_intel.sources import daily_candles as dc\n"
        "yf = dc._load_yfinance()\n"
        "from yfinance import _http\n"
        "from yfinance.data import YfData\n"
        "s = dc.honest_session()\n"
        "d = YfData(session=s)\n"
        "print(_http.HAS_CURL_CFFI, d._session is s, type(d._session).__module__)\n"
    )
    env = {k: v for k, v in os.environ.items() if k != "YF_DISABLE_CURL_CFFI"}
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, env=env, timeout=120
    )
    assert out.returncode == 0, out.stderr[-2000:]
    assert out.stdout.split() == ["False", "True", "requests.sessions"]
