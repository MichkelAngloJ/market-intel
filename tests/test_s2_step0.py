"""S1 review answers applied in S2 step 0: the BHARATBOND indices left the indices universe;
special sessions are not part of the loader's gap calendar. Fakes only."""

from datetime import date, timedelta
from decimal import Decimal

import test_candles_loader as loader_tests
from test_candles_loader import DAYS, NIFTY, STOCK_A, FakeStore, FakeUpstox, FakeYahoo, candle, load

from market_intel import candles

settings = loader_tests.settings  # the same fixture


def test_the_indices_universe_leaves_out_the_bharatbond_indices():
    assert candles.NOT_SERVED_INDEX_PREFIX == "NSE_INDEX|BHARATBOND"
    assert "NOT LIKE 'NSE_INDEX|BHARATBOND%" in candles.UNIVERSE_SQL["indices"]


class SpecialStore(FakeStore):
    def __init__(self, universe, special):
        super().__init__(universe)
        self.special = special

    def special_sessions(self):
        return self.special


def test_a_special_session_is_not_a_gap(settings):
    special = DAYS[6] + timedelta(days=(5 - DAYS[6].weekday()))  # the Saturday after day 6
    store = SpecialStore([NIFTY, STOCK_A], [special])
    store.upsert(NIFTY.id, [candle("upstox", "NIFTY", special, Decimal(100))])  # Nifty traded it
    load(settings, store, FakeUpstox(), FakeYahoo())
    assert (STOCK_A.id, special) not in store.rows
    assert not store.gap_rows  # the stock missing the special session is no gap


def test_without_the_special_session_rule_it_would_be_a_gap(settings):
    special = DAYS[6] + timedelta(days=(5 - DAYS[6].weekday()))
    store = FakeStore([NIFTY, STOCK_A])
    store.upsert(NIFTY.id, [candle("upstox", "NIFTY", special, Decimal(100))])
    load(settings, store, FakeUpstox(), FakeYahoo(fail={"FAKEA"}))
    assert [a for (_, a) in store.gap_rows] == [special]
    assert special > date(2026, 11, 1)
