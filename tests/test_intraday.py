"""Intraday features v1, triple-barrier labels and the intraday leakage test (S4i). Synthetic
minute bars only (tests/intraday_fakes.py); no database."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, time, timedelta

import intraday_fakes as fakes
import numpy as np
import pandas as pd
import pytest

from market_intel import intraday as it


@pytest.fixture(scope="module")
def inp():
    return fakes.inputs(30, thin_day=25)


@pytest.fixture(scope="module")
def feats(inp):
    return it.build_minute_features(inp, 1)


# --- features ------------------------------------------------------------------------------


def test_decision_points_are_every_5_minutes_from_0930_to_1500(feats):
    d = it.decisions(feats)
    local = d.index.tz_convert(fakes.IST)
    assert local.time.min() == time(9, 30) and local.time.max() == time(15, 0)
    assert set(local.minute % 5) == {0}
    assert len(d) == 30 * 67
    assert list(d.columns) == ["trade_date", "minute", *it.FEATURES_V1]


def test_every_feature_belongs_to_one_group():
    grouped = [f for cols in it.GROUPS.values() for f in cols]
    assert sorted(grouped) == sorted(it.FEATURES_V1)
    assert len(set(grouped)) == len(grouped)


def test_returns_are_within_the_day(inp, feats):
    bars = inp.bars[1]
    t = fakes.at(fakes.business_days(date(2024, 3, 4), 30)[5], 10, 0)
    i = bars.index.get_loc(t)
    assert feats.loc[t, "ret_5m"] == pytest.approx(
        np.log(bars["close"].iloc[i] / bars["close"].iloc[i - 5])
    )
    first = fakes.at(fakes.business_days(date(2024, 3, 4), 30)[5], 9, 15)
    assert np.isnan(feats.loc[first, "ret_1m"])  # no look across the overnight gap


def test_vwap_and_range_so_far(inp, feats):
    day = fakes.business_days(date(2024, 3, 4), 30)[3]
    t = fakes.at(day, 9, 45)
    b = inp.bars[1]
    s = b[(b.index >= fakes.at(day, 9, 15)) & (b.index <= t)]
    typ = (s["high"] + s["low"] + s["close"]) / 3
    vwap = (typ * s["volume"]).sum() / s["volume"].sum()
    assert feats.loc[t, "vwap_dist"] == pytest.approx(s["close"].iloc[-1] / vwap - 1)
    assert feats.loc[t, "range_so_far"] == pytest.approx(
        (s["high"].max() - s["low"].min()) / s["open"].iloc[0]
    )


def test_opening_range_is_undefined_until_complete(feats):
    assert feats[feats["minute"] < 14][["or15_pos", "or15_up", "or15_down"]].isna().all().all()
    assert feats[feats["minute"] < 29][["or30_pos", "or30_up", "or30_down"]].isna().all().all()
    assert feats[feats["minute"] >= 29][["or30_pos"]].notna().mean().iloc[0] > 0.99


def test_time_of_day_volume_uses_the_previous_20_days(inp, feats):
    days = fakes.business_days(date(2024, 3, 4), 30)
    t = fakes.at(days[25], 11, 0)
    b = inp.bars[1]
    same = [b.loc[fakes.at(d, 11, 0), "volume"] for d in days[5:25]]
    assert feats.loc[t, "vol_rel_tod"] == pytest.approx(b.loc[t, "volume"] / np.mean(same))
    assert np.isnan(feats.loc[fakes.at(days[5], 11, 0), "vol_rel_tod"])  # < 10 days of history


def test_previous_day_features_use_daily_candles_before_today(inp, feats):
    days = fakes.business_days(date(2024, 3, 4), 30)
    t = fakes.at(days[20], 10, 0)
    d = inp.daily[1]
    assert feats.loc[t, "prev_ret"] == pytest.approx(
        np.log(d.loc[days[19], "close"] / d.loc[days[18], "close"])
    )
    assert feats.loc[t, "gap_overnight"] == pytest.approx(
        inp.bars[1].loc[fakes.at(days[20], 9, 15), "open"] / d.loc[days[19], "close"] - 1
    )


def test_market_sector_and_vix(inp, feats):
    days = fakes.business_days(date(2024, 3, 4), 30)
    t = fakes.at(days[7], 13, 0)
    m = inp.market
    mkt_open = np.log(m.loc[t, "close"] / m.loc[fakes.at(days[7], 9, 15), "open"])
    assert feats.loc[t, "mkt_ret_open"] == pytest.approx(mkt_open)
    assert feats.loc[t, "rs_nifty_open"] == pytest.approx(feats.loc[t, "ret_open"] - mkt_open)
    assert feats.loc[t, "vix_level"] == pytest.approx(inp.vix.loc[t, "close"])
    no_sector = it.build_minute_features(inp, 3)  # instrument 3 has no sector index
    assert no_sector["rs_sector_open"].isna().all()


def test_price_event_on_the_previous_day_is_a_feature(inp):
    days = fakes.business_days(date(2024, 3, 4), 30)
    f = it.build_minute_features(inp, 2)  # event on days[10]
    assert f[f["trade_date"] == days[11]]["price_event_prev_day"].eq(1).all()
    assert f[f["trade_date"] == days[12]]["price_event_prev_day"].eq(0).all()


def test_rule_signals_and_gap_bucket_take_known_values(feats):
    for col in ("sig_orb_up", "sig_orb_down", "sig_vwap_up", "sig_vwap_down", "sig_volume_burst"):
        assert set(feats[col].unique()) <= {0.0, 1.0}
    assert set(feats["gap_bucket"].dropna().unique()) <= {-2.0, -1.0, 0.0, 1.0, 2.0}
    assert feats["sig_vwap_up"].sum() > 0


# --- labels --------------------------------------------------------------------------------

DAY = date(2024, 3, 5)


def flat_inputs(changes=None, wiggle: float = 0.0) -> it.IntradayInputs:
    """One flat day at 100; ``changes``: {minute: (open, high, low, close)}. ``wiggle`` widens
    every untouched bar by that much either side, so the v2 ATR is not zero."""
    changes = changes or {}
    days = [date(2024, 3, 4), DAY]
    idx, rows = [], []
    for d in days:
        for m in range(375):
            idx.append(datetime.combine(d, time(9, 15), fakes.IST) + timedelta(minutes=m))
            flat = (100.0, 100.0 + wiggle, 100.0 - wiggle, 100.0)
            rows.append(changes.get(m, flat) if d == DAY else flat)
    bars = pd.DataFrame(rows, columns=["open", "high", "low", "close"],
                        index=pd.DatetimeIndex(idx).tz_convert("UTC"))  # fmt: skip
    bars["volume"] = 1000.0
    return it.IntradayInputs(bars={1: bars}, daily={1: fakes.daily_from(bars)})


def label(inputs, minute=60, barriers=it.DEFAULT_BARRIERS, side="long"):
    out = it.build_labels(inputs, 1, barriers)
    row = out[(out["trade_date"] == DAY) & (out["minute"] == minute) & (out["side"] == side)]
    return row.iloc[0]


def test_target_first_is_plus_one_and_the_short_side_is_stopped():
    inputs = flat_inputs({63: (100.0, 100.6, 100.0, 100.5)})
    long = label(inputs)
    assert (long.outcome, long.gross_return, long.bars_held) == (1, pytest.approx(0.005), 3)
    short = label(inputs, side="short")
    assert (short.outcome, short.gross_return) == (-1, pytest.approx(-0.0025))


def test_stop_counts_first_when_one_bar_hits_both():
    inputs = flat_inputs({62: (100.0, 100.6, 99.7, 100.0)})
    assert label(inputs).outcome == -1
    assert label(inputs, side="short").outcome == -1


def test_time_limit_exits_at_the_last_close_with_the_gross_return():
    inputs = flat_inputs({75: (100.0, 100.1, 100.0, 100.1)})
    row = label(inputs)
    assert (row.outcome, row.bars_held) == (0, 15)
    assert row.gross_return == pytest.approx(0.001)
    assert label(inputs, side="short").gross_return == pytest.approx(-0.001)
    # bar 76 is outside the 15-bar window
    assert label(flat_inputs({76: (100.0, 101.0, 100.0, 101.0)})).outcome == 0


def test_entry_is_the_open_of_the_next_bar():
    inputs = flat_inputs({61: (100.2, 100.2, 100.2, 100.2)})
    assert label(inputs).entry_price == pytest.approx(100.2)


def test_everything_closes_by_the_1520_bar():
    row = label(flat_inputs(), minute=345, barriers=it.Barriers(max_bars=100))
    assert row.bars_held == 20  # bars 346..365 (15:01..15:20)


def test_excluded_days_carry_the_reason():
    inputs = flat_inputs()
    assert label(replace(inputs, special={DAY})).excluded_reason == "special_session"
    assert label(replace(inputs, events={1: {DAY}})).excluded_reason == "price_event"
    assert label(replace(inputs, events={1: {date(2024, 3, 4)}})).excluded_reason == "price_event"
    b = inputs.bars[1]
    thin = b[~((b.index.tz_convert(fakes.IST).date == DAY) & (np.arange(len(b)) % 375 >= 299))]
    assert label(replace(inputs, bars={1: thin})).excluded_reason == "thin_day"
    gap = b.drop(index=fakes.at(DAY, 10, 16))
    row = label(replace(inputs, bars={1: gap}))
    assert row.excluded_reason == "no_entry" and pd.isna(row.outcome)


def test_label_config_defaults_match_the_decision():
    from market_intel.config import Settings

    fields = Settings.model_fields
    assert fields["intraday_target_pct"].default == 0.5
    assert fields["intraday_stop_pct"].default == 0.25
    assert fields["intraday_max_bars"].default == 15
    assert it.DEFAULT_BARRIERS == it.Barriers(0.5, 0.25, 15)


# --- the leakage test, and leaky fakes it must catch --------------------------------------


def test_the_real_features_and_labels_pass(inp):
    report = it.run_leakage_checks(inp, it.pick_samples(inp, 12))
    assert report.passed, report.lines()
    assert report.lines()[0] == "intraday leakage check: PASS"


def _with(build_col):
    def build(inputs, inst):
        f = it.build_minute_features(inputs, inst)
        return build_col(f, inputs.bars[inst])

    return build


def leak_next_close(f, bars):
    c = bars["close"].reindex(f.index)
    f["ret_1m"] = np.log(c.shift(-1) / c)
    return f


def leak_centred_average(f, bars):
    c = bars["close"].reindex(f.index)
    f["vwap_dist"] = c / c.rolling(5, center=True).mean() - 1
    return f


def leak_full_day_volume(f, bars):
    v = bars["volume"].reindex(f.index)
    f["vol_rel_tod"] = v / v.groupby(f["trade_date"]).transform("mean")
    return f


def leak_opening_range(f, bars):
    f["or15_pos"] = 0.5
    return f


@pytest.mark.parametrize("leak", [leak_next_close, leak_centred_average, leak_full_day_volume])
def test_truncation_catches_leaky_features(inp, leak):
    samples = it.pick_samples(inp, 8)
    assert it.check_truncation(inp, samples, _with(leak))


def test_the_time_of_day_check_catches_todays_full_day_volume(inp):
    samples = it.pick_samples(inp, 5)
    assert it.check_time_of_day_baseline(inp, samples, _with(leak_full_day_volume))
    assert not it.check_time_of_day_baseline(inp, samples)


def test_the_opening_range_check_catches_an_early_range(inp):
    assert it.check_opening_range(inp, [1], _with(leak_opening_range))
    assert not it.check_opening_range(inp, [1])


def entry_at_t_close(inputs, inst, barriers=it.DEFAULT_BARRIERS, only=None):
    bars = inputs.bars[inst].copy()
    bars["open"] = bars["close"].shift(1).fillna(bars["open"])  # "next open" := close of t
    bars["high"] = bars[["high", "open"]].max(axis=1)
    bars["low"] = bars[["low", "open"]].min(axis=1)
    return it.build_labels(replace(inputs, bars={**inputs.bars, inst: bars}), inst, barriers, only)


def test_the_label_check_catches_entry_at_bar_t_close(inp):
    samples = it.pick_samples(inp, 6)
    assert it.check_labels(inp, samples, labels=entry_at_t_close)
    assert not it.check_labels(inp, samples)


def test_a_failing_check_fails_the_report(inp):
    samples = it.pick_samples(inp, 4)
    report = it.run_leakage_checks(inp, samples, build=_with(leak_next_close))
    assert not report.passed
    assert "FAIL" in report.lines()[0]


def test_no_feature_name_looks_like_a_label():
    assert it.check_names() == []


def test_truncated_inputs_drop_the_future(inp):
    days = fakes.business_days(date(2024, 3, 4), 30)
    t = fakes.at(days[12], 11, 0)
    cut = inp.truncated(t)
    assert cut.bars[1].index.max() == t
    assert cut.market.index.max() == t
    assert max(cut.daily[1].index) == days[11]
    assert cut.events[2] == {days[10]}
    assert set(inp.truncated(t, keep=1).bars) == {1}


def test_month_helpers():
    assert it.month_starts(date(2023, 11, 20), date(2024, 2, 1)) == [
        date(2023, 11, 1), date(2023, 12, 1), date(2024, 1, 1), date(2024, 2, 1)
    ]  # fmt: skip
    assert it.month_end(date(2024, 2, 1)) == date(2024, 2, 29)


# --- labels v2: volatility-scaled barriers (S5i) -------------------------------------------


def test_the_variant_grid_is_three_ks_by_three_time_limits():
    assert len(it.VARIANTS) == 9
    assert [v.name for v in it.VARIANTS[:3]] == ["k10_b05", "k10_b10", "k10_b15"]
    assert it.VARIANT_BY_NAME["k15_b10"] == it.Variant(1.5, 10)
    assert len(it.variant_columns()) == 27  # outcome, return and bars held per variant


def test_true_range_and_atr_look_back_only():
    grid = np.full((it.SESSION_BARS, 4), np.nan)
    grid[:5] = [[100, 101, 99, 100], [100, 102, 100, 101], [101, 101, 98, 99],
                [99, 100, 98, 100], [100, 100, 100, 100]]  # fmt: skip
    tr = it.true_range(grid)
    assert tr[0] == pytest.approx(2.0)  # high - low, no previous close
    assert tr[1] == pytest.approx(2.0)  # max(2, |102-100|, |100-100|)
    assert tr[2] == pytest.approx(3.0)  # max(3, |101-101|, |98-101|)
    atr = it.atr_series(grid, bars=3)
    assert np.isnan(atr[1])  # the window is not full
    assert atr[2] == pytest.approx((2.0 + 2.0 + 3.0) / 3)


def test_v2_barriers_are_two_to_one_and_scale_with_the_atr(inp):
    frame = it.build_labels_volatility(inp, 1)
    row = frame[frame["excluded_reason"].isna()].iloc[0]
    atr = row["atr"]
    # a stop-first row loses exactly k/2 x ATR, a target-first row wins k x ATR
    for v in it.VARIANTS:
        hit, ret = row[f"outcome_{v.name}"], row[f"gross_return_{v.name}"]
        if hit == 1:
            assert ret == pytest.approx(v.k * atr / row["entry_price"])
        elif hit == -1:
            assert ret == pytest.approx(-v.k / 2 * atr / row["entry_price"])


def test_a_wider_k_times_out_more_often(inp):
    frame = it.build_labels_volatility(inp, 1)
    ok = frame[frame["excluded_reason"].isna()]
    narrow = (ok["outcome_k10_b05"] == 0).mean()
    wide = (ok["outcome_k20_b05"] == 0).mean()
    assert wide > narrow  # a barrier twice as far is reached less often in the same 5 bars


def test_v2_excludes_the_bars_before_the_atr_window_is_full():
    inputs = flat_inputs(wiggle=0.1)
    short_day = inputs.bars[1]
    frame = it.build_labels_volatility(inputs, 1, atr_bars=400)  # never fills
    assert set(frame["excluded_reason"].dropna()) == {"no_atr"}
    assert len(short_day) > 0


def test_v2_still_counts_the_stop_first_when_one_bar_hits_both():
    # ATR = 0.2, so k=1 puts the target 0.2 away and the stop 0.1 away; bar 62 spans both
    inputs = flat_inputs({62: (100.0, 100.3, 99.8, 100.0)}, wiggle=0.1)
    frame = it.build_labels_volatility(inputs, 1, variants=[it.Variant(1.0, 15)])
    row = frame[(frame["trade_date"] == DAY) & (frame["minute"] == 60)]
    assert row["atr"].tolist() == pytest.approx([0.2, 0.2])
    assert set(row["outcome_k10_b15"]) == {-1}  # both sides stop out


def test_v2_closes_by_the_1520_bar():
    inputs = flat_inputs(wiggle=0.1)
    frame = it.build_labels_volatility(inputs, 1, variants=[it.Variant(2.0, 100)])
    row = frame[(frame["trade_date"] == DAY) & (frame["minute"] == 345)].iloc[0]
    assert row["bars_held_k20_b100"] == 20  # 15:01 .. 15:20, not 100 bars


def leaky_atr(inputs, inst, variants=it.VARIANTS, only=None):
    """A fake whose barrier width peeks at the bars AFTER t."""
    bars = inputs.bars[inst].copy()
    shifted = bars.shift(-20).reindex(bars.index)
    bars[["high", "low"]] = shifted[["high", "low"]].fillna(bars[["high", "low"]])
    return it.build_labels_volatility(
        replace(inputs, bars={**inputs.bars, inst: bars}), inst, variants, only
    )


def test_the_v2_check_catches_an_atr_that_peeks_ahead(inp):
    samples = it.pick_samples(inp, 6)
    assert it.check_labels_volatility(inp, samples, labels=leaky_atr)
    assert not it.check_labels_volatility(inp, samples)
