"""Choosing an intraday universe from daily candles (S5i-c): the liquidity floor, the
volatility ranking, the history requirement and the owner's named picks. Synthetic rows only."""

from __future__ import annotations

from datetime import date

import pytest

from market_intel import universe as U

CRORE = U.CRORE


def candidate(
    symbol, *, turnover_cr=100.0, day_range=0.03, first_day=date(2018, 1, 1), named=False
):
    return U.Candidate(
        instrument_id=abs(hash(symbol)) % 100_000,
        symbol=symbol,
        turnover=turnover_cr * CRORE,
        day_range=day_range,
        first_day=first_day,
        days=400,
        named=named,
    )


def test_the_most_volatile_liquid_stocks_are_chosen():
    stocks = [
        candidate("CALM", day_range=0.01),
        candidate("WILD", day_range=0.06),
        candidate("MIDDLE", day_range=0.03),
    ]
    chosen, liquid = U.choose(stocks, size=2, picks=())
    assert [c.symbol for c in chosen] == ["WILD", "MIDDLE"]
    assert liquid == 3


def test_thin_stocks_are_dropped_however_volatile():
    stocks = [
        candidate("THIN", turnover_cr=5.0, day_range=0.20),
        candidate("LIQUID", turnover_cr=50.0, day_range=0.02),
    ]
    chosen, liquid = U.choose(stocks, size=10, min_turnover=25 * CRORE, picks=())
    assert [c.symbol for c in chosen] == ["LIQUID"]
    assert liquid == 1  # only one cleared the floor


def test_a_stock_without_history_before_the_intraday_start_is_dropped():
    stocks = [
        candidate("NEW", first_day=date(2026, 10, 28), day_range=0.09),
        candidate("OLD", first_day=date(2019, 5, 1), day_range=0.02),
    ]
    chosen, _ = U.choose(stocks, size=10, picks=())
    assert [c.symbol for c in chosen] == ["OLD"]


def test_the_named_picks_are_always_in_even_if_they_fail_the_screen():
    stocks = [
        candidate("HINDCOPPER", turnover_cr=1.0, day_range=0.001),  # would fail on both counts
        candidate("WILD", day_range=0.06),
    ]
    chosen, _ = U.choose(stocks, size=1, picks=("HINDCOPPER",))
    assert {c.symbol for c in chosen} == {"HINDCOPPER", "WILD"}
    assert [c.symbol for c in chosen if c.named] == ["HINDCOPPER"]


def test_a_named_pick_is_never_counted_twice():
    stocks = [candidate("HAL", day_range=0.06), candidate("OTHER", day_range=0.05)]
    chosen, _ = U.choose(stocks, size=5, picks=("HAL",))
    assert [c.symbol for c in chosen].count("HAL") == 1
    assert len(chosen) == 2


def test_the_selection_is_deterministic_and_breaks_ties_by_symbol():
    stocks = [candidate(s, day_range=0.03) for s in ("ZEBRA", "ALPHA", "MANGO")]
    first, _ = U.choose(stocks, size=2, picks=())
    second, _ = U.choose(list(reversed(stocks)), size=2, picks=())
    assert [c.symbol for c in first] == [c.symbol for c in second] == ["ALPHA", "MANGO"]


def test_the_owners_named_picks_are_the_agreed_seven():
    assert set(U.NAMED_PICKS) == {
        "HINDCOPPER", "MAZDOCK", "AEGISLOG", "PETRONET", "HAL", "FEDERALBNK", "IDFCFIRSTB",
    }  # fmt: skip
    assert "JPL" not in U.NAMED_PICKS  # listed 2026-10-28: too new for a 2022+ test
    assert not any("GOLD" in p for p in U.NAMED_PICKS)


def test_the_plan_summarises_what_was_picked():
    chosen = [candidate("HAL", day_range=0.04, named=True), candidate("WILD", day_range=0.06)]
    plan = U.UniversePlan(
        name="movers100", source="nifty500", size=1, min_turnover=25 * CRORE,
        lookback_days=500, considered=497, liquid=317, chosen=chosen, written=2,
    )  # fmt: skip
    text = "\n".join(plan.lines())
    assert "movers100: 2 stocks" in text and "497 in nifty500" in text
    assert "317 above Rs 25 crore" in text
    assert "named picks included: HAL" in text
    assert "rows written to universe_members: 2" in text


def test_a_missing_pick_is_reported_loudly():
    plan = U.UniversePlan(
        name="movers100", source="nifty500", size=1, min_turnover=25 * CRORE,
        lookback_days=500, chosen=[candidate("WILD")], missing_picks=["NOSUCH"],
    )  # fmt: skip
    assert "named picks NOT FOUND in instruments: NOSUCH" in "\n".join(plan.lines())


def test_a_dry_run_says_nothing_was_saved():
    plan = U.UniversePlan(
        name="movers100", source="nifty500", size=1, min_turnover=25 * CRORE,
        lookback_days=500, chosen=[candidate("WILD")], dry_run=True,
    )  # fmt: skip
    lines = plan.lines()
    assert lines[0].startswith("DRY RUN (nothing saved)")
    assert not any("rows written" in line for line in lines)


def test_a_bad_universe_name_is_refused_before_any_sql():
    class Boom:
        def execute(self, *a, **k):
            raise AssertionError("must not reach the database")

    with pytest.raises(ValueError, match="bad universe name"):
        U.write_members(Boom(), "movers100; DROP TABLE instruments", [], date(2026, 11, 26))


def test_an_empty_candidate_list_chooses_nothing():
    chosen, liquid = U.choose([], size=10, picks=())
    assert chosen == [] and liquid == 0
