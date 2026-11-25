"""The intraday rule baselines (S5i): each rule on hand-made feature rows, plus the no-trade
and market-mood filters. No database, no candles: the rules read features only."""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

from market_intel import intraday_strategies as S
from market_intel.intraday import FEATURES_V1

NEUTRAL = {
    "instrument_id": 1,
    "trade_date": date(2024, 3, 5),
    "minute": 60,
    "or15_pos": 0.5,
    "or15_up": 0.0,
    "or15_down": 0.0,
    "or30_pos": 0.5,
    "or30_up": 0.0,
    "or30_down": 0.0,
    "vwap_dist": 0.0,
    "vwap_slope_15": 0.0,
    "ret_open": 0.0,
    "ret_5m": 0.0,
    "gap_bucket": 0.0,
    "vol_rel_tod": 1.0,
    "rs_nifty_open": 0.0,
    "rs_sector_open": 0.0,
    "vix_level": 14.0,
    "mkt_ret_open": 0.0,
    "price_event_prev_day": 0.0,
    "thin_so_far": 0.0,
}


def rows(*overrides: dict) -> pd.DataFrame:
    """A features frame of one row per override, on top of a neutral row."""
    out = []
    for i, over in enumerate(overrides or ({},)):
        row = {**NEUTRAL, **over}
        row["minute"] = NEUTRAL["minute"] + 5 * i
        out.append(row)
    return pd.DataFrame(out)


def signal(name: str, frame: pd.DataFrame, **kw) -> list[int]:
    return S.STRATEGY_BY_NAME[name].signals(frame, **kw).tolist()


# --- the rules ----------------------------------------------------------------------------


def test_the_catalogue_covers_every_promised_rule():
    assert [s.name for s in S.STRATEGIES] == [
        "orb_15", "orb_30", "vwap_trend", "vwap_reversion",
        "gap_and_go", "gap_fill", "volume_burst", "relative_strength",
    ]  # fmt: skip
    assert all(s.description for s in S.STRATEGIES)


def test_opening_range_breakout_fires_once_when_the_range_breaks():
    # three decision rows of one day: inside, breaks up, still up
    frame = rows({"or15_up": 0.0}, {"or15_up": 1.0}, {"or15_up": 1.0})
    assert signal("orb_15", frame) == [S.FLAT, S.LONG, S.FLAT]  # only the crossing
    down = rows({"or15_down": 0.0}, {"or15_down": 1.0})
    assert signal("orb_15", down) == [S.FLAT, S.SHORT]


def test_the_thirty_minute_range_is_a_separate_rule():
    frame = rows({"or30_up": 0.0}, {"or30_up": 1.0})
    assert signal("orb_30", frame) == [S.FLAT, S.LONG]
    assert signal("orb_15", frame) == [S.FLAT, S.FLAT]  # the 15-minute flags never moved


def test_a_breakout_on_a_new_day_fires_again():
    frame = rows({"or15_up": 1.0}, {"or15_up": 1.0, "trade_date": date(2024, 3, 6)})
    assert signal("orb_15", frame) == [S.LONG, S.LONG]


def test_vwap_trend_needs_the_price_and_the_slope_to_agree():
    assert signal("vwap_trend", rows({"vwap_dist": 0.002, "vwap_slope_15": 0.001})) == [S.LONG]
    assert signal("vwap_trend", rows({"vwap_dist": -0.002, "vwap_slope_15": -0.001})) == [S.SHORT]
    # above VWAP but falling: no trade
    assert signal("vwap_trend", rows({"vwap_dist": 0.002, "vwap_slope_15": -0.001})) == [S.FLAT]


def test_vwap_reversion_fades_a_stretch_and_is_the_opposite_of_the_trend_rule():
    far_below = rows({"vwap_dist": -0.005, "vwap_slope_15": -0.001})
    assert signal("vwap_reversion", far_below) == [S.LONG]  # buy the dip
    assert signal("vwap_trend", far_below) == [S.SHORT]  # the trend rule sells it
    assert signal("vwap_reversion", rows({"vwap_dist": 0.005})) == [S.SHORT]
    assert signal("vwap_reversion", rows({"vwap_dist": 0.001})) == [S.FLAT]  # not far enough


def test_gap_and_go_follows_while_gap_fill_fades():
    still_up = rows({"gap_bucket": 2.0, "ret_open": 0.004})
    fading = rows({"gap_bucket": 2.0, "ret_open": -0.004})
    assert signal("gap_and_go", still_up) == [S.LONG]
    assert signal("gap_fill", still_up) == [S.FLAT]
    assert signal("gap_and_go", fading) == [S.FLAT]
    assert signal("gap_fill", fading) == [S.SHORT]
    assert signal("gap_and_go", rows({"gap_bucket": 0.0, "ret_open": 0.004})) == [S.FLAT]


def test_volume_burst_needs_both_the_burst_and_the_move():
    assert signal("volume_burst", rows({"vol_rel_tod": 4.0, "ret_5m": 0.002})) == [S.LONG]
    assert signal("volume_burst", rows({"vol_rel_tod": 4.0, "ret_5m": -0.002})) == [S.SHORT]
    assert signal("volume_burst", rows({"vol_rel_tod": 4.0, "ret_5m": 0.0})) == [S.FLAT]
    assert signal("volume_burst", rows({"vol_rel_tod": 1.5, "ret_5m": 0.002})) == [S.FLAT]


def test_relative_strength_needs_the_nifty_and_the_sector_to_agree():
    assert signal("relative_strength", rows({"rs_nifty_open": 0.005, "rs_sector_open": 0.002})) == [
        S.LONG
    ]
    # beating the Nifty but losing to its own sector: no trade
    assert signal(
        "relative_strength", rows({"rs_nifty_open": 0.005, "rs_sector_open": -0.002})
    ) == [S.FLAT]
    # no sector index at all (NULL): the Nifty alone decides
    assert signal(
        "relative_strength", rows({"rs_nifty_open": 0.005, "rs_sector_open": np.nan})
    ) == [S.LONG]
    assert signal(
        "relative_strength", rows({"rs_nifty_open": -0.005, "rs_sector_open": np.nan})
    ) == [S.SHORT]


# --- the filters --------------------------------------------------------------------------


def test_the_no_trade_filter_removes_events_thin_sessions_and_a_missing_range():
    strong = {"vwap_dist": 0.005}
    assert signal("vwap_reversion", rows({**strong, "price_event_prev_day": 1.0})) == [S.FLAT]
    assert signal("vwap_reversion", rows({**strong, "thin_so_far": 1.0})) == [S.FLAT]
    assert signal("vwap_reversion", rows({**strong, "or15_pos": np.nan})) == [S.FLAT]
    # the signal itself is still there without the filters
    assert signal("vwap_reversion", rows({**strong, "thin_so_far": 1.0}), filters=False) == [
        S.SHORT
    ]


def test_panic_in_the_vix_stops_everything():
    frame = rows({"vwap_dist": 0.005, "vix_level": 45.0})
    assert signal("vwap_reversion", frame) == [S.FLAT]
    calm = rows({"vwap_dist": 0.005, "vix_level": 20.0})
    assert signal("vwap_reversion", calm) == [S.SHORT]


def test_the_market_vetoes_only_the_side_fighting_it():
    # the Nifty is down 1% since the open: longs are vetoed, shorts are not
    falling = {"mkt_ret_open": -0.01}
    assert signal("vwap_reversion", rows({**falling, "vwap_dist": -0.005})) == [S.FLAT]
    assert signal("vwap_reversion", rows({**falling, "vwap_dist": 0.005})) == [S.SHORT]
    rising = {"mkt_ret_open": 0.01}
    assert signal("vwap_reversion", rows({**rising, "vwap_dist": 0.005})) == [S.FLAT]
    assert signal("vwap_reversion", rows({**rising, "vwap_dist": -0.005})) == [S.LONG]


def test_thresholds_are_settings_not_hidden_constants():
    frame = rows({"vwap_dist": 0.002})
    assert signal("vwap_reversion", frame) == [S.FLAT]  # 0.2% is inside the default 0.3%
    loose = S.RuleSettings(vwap_far_pct=0.1)
    assert signal("vwap_reversion", frame, settings=loose) == [S.SHORT]


def test_all_signals_returns_one_column_per_strategy():
    frame = rows({"vwap_dist": 0.005}, {"or15_up": 1.0})
    table = S.all_signals(frame)
    assert list(table.columns) == [s.name for s in S.STRATEGIES]
    assert len(table) == 2
    assert table["vwap_reversion"].tolist() == [S.SHORT, S.FLAT]


def test_every_feature_a_rule_reads_exists_in_features_v1():
    used = {
        "or15_up", "or15_down", "or30_up", "or30_down", "or15_pos", "vwap_dist",
        "vwap_slope_15", "ret_open", "ret_5m", "gap_bucket", "vol_rel_tod",
        "rs_nifty_open", "rs_sector_open", "vix_level", "mkt_ret_open",
        "price_event_prev_day", "thin_so_far",
    }  # fmt: skip
    assert used <= set(FEATURES_V1), used - set(FEATURES_V1)


def test_signals_are_small_integers():
    frame = rows({"vwap_dist": 0.005})
    assert S.all_signals(frame).dtypes.unique().tolist() == [np.dtype("int8")]
    with pytest.raises(KeyError):
        S.STRATEGY_BY_NAME["vwap_trend"].signals(pd.DataFrame({"instrument_id": [1]}))
