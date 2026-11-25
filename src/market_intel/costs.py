"""Indian equity trading costs and slippage: delivery (S3) and intraday (S5i).

Charges for one DELIVERY order on NSE through Upstox. Values checked 2026-11-24 on Upstox's
official charges page, https://upstox.com/brokerage-charges/ (effective-date headings on that
page; NSE's own charges page timed out that day, so the exchange figures are Upstox's
published copy of them):

- brokerage: Rs 20 per executed order (equity delivery)
- STT: 0.1% on buy and on sell ("From 1st April 2026")
- NSE transaction charge: 0.00307% of turnover on each side ("From 1st March 2026"; the
  Investor Protection Fund Trust charge is combined with it)
- SEBI turnover fee: Rs 10 per crore (0.0001%) on each side
- stamp duty: 0.015% on the buy side
- GST: 18% on brokerage + transaction charge + DP charge
- DP charge: Rs 20 per scrip per sell day (CDSL Rs 3.5 + Upstox Rs 16.5), plus GST

Slippage (the price moves against us between the decision and the fill) is a share of the
trade value per side, by liquidity bucket from the 20-day mean traded value (feature
``log_traded_value_20d`` = log(1 + rupees)): 10 bps at Rs 100 crore a day or more, 20 bps from
Rs 10 crore, 40 bps below. All values are parameters of :class:`CostModel`.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

CRORE = 10_000_000.0


@dataclass(frozen=True)
class CostModel:
    brokerage_per_order: float = 20.0
    stt_rate: float = 0.001  # buy and sell, delivery
    exchange_rate: float = 0.0000307  # NSE transaction charge incl. IPFT, each side
    sebi_per_crore: float = 10.0  # each side
    stamp_rate: float = 0.00015  # buy side only
    gst_rate: float = 0.18  # on brokerage + exchange charge + DP charge
    dp_per_sell: float = 20.0  # per scrip per sell day
    slippage_bps: tuple[float, float, float] = (10.0, 20.0, 40.0)  # high / mid / low liquidity
    liquid_rupees: tuple[float, float] = (100 * CRORE, 10 * CRORE)  # bucket thresholds

    def buy_cost(self, value: float, slippage_bps: float = 0.0) -> float:
        """Rupees paid on a delivery BUY of ``value`` rupees (charges + slippage)."""
        exchange = value * self.exchange_rate
        charges = (
            self.brokerage_per_order
            + value * self.stt_rate
            + exchange
            + value * self.sebi_per_crore / CRORE
            + value * self.stamp_rate
            + self.gst_rate * (self.brokerage_per_order + exchange)
        )
        return charges + value * slippage_bps / 10_000

    def sell_cost(self, value: float, slippage_bps: float = 0.0) -> float:
        """Rupees paid on a delivery SELL of ``value`` rupees (charges + DP + slippage)."""
        exchange = value * self.exchange_rate
        charges = (
            self.brokerage_per_order
            + value * self.stt_rate
            + exchange
            + value * self.sebi_per_crore / CRORE
            + self.dp_per_sell
            + self.gst_rate * (self.brokerage_per_order + exchange + self.dp_per_sell)
        )
        return charges + value * slippage_bps / 10_000

    def slippage_for(self, log_traded_value_20d: float | None) -> float:
        """Slippage in bps per side for a stock's 20-day mean traded value (unknown -> worst)."""
        if log_traded_value_20d is None or math.isnan(log_traded_value_20d):
            return self.slippage_bps[2]
        rupees = math.expm1(log_traded_value_20d)
        if rupees >= self.liquid_rupees[0]:
            return self.slippage_bps[0]
        if rupees >= self.liquid_rupees[1]:
            return self.slippage_bps[1]
        return self.slippage_bps[2]

    def round_trip_fraction(self, value: float, slippage_bps: float) -> float:
        """Buy + sell costs as a share of the position value."""
        return (self.buy_cost(value, slippage_bps) + self.sell_cost(value, slippage_bps)) / value


DEFAULT_COSTS = CostModel()


# --- intraday (sprint S5i; decision 2026-11-25) -------------------------------------------
#
# Charges for one INTRADAY (MIS) equity order on NSE through Upstox. Values checked
# 2026-11-25 on Upstox's official pages, https://upstox.com/brokerage-charges/ (with the
# effective-date headings quoted below) and
# https://upstox.com/calculator/brokerage-calculator/:
#
# - brokerage: Rs 20 per executed order OR 0.1% of turnover, whichever is LOWER, on each side
#   (so 0.1% binds below Rs 20,000 of order value); charged on buy and on sell
# - STT: 0.025% on the SELL side only ("STT/CTT (From 1st April 2026)"; the rate is unchanged
#   from the 1 Oct 2024 - 31 Mar 2026 heading)
# - NSE transaction charge: 0.00307% of turnover on each side ("Transaction charges (From 1st
#   March 2026)"; it was 0.00297% before that). The page says IPFT is now combined with the
#   transaction charge, but its separate IPFT table still shows conflicting figures
#   (Rs 0.01 per crore vs Rs 0.10 per lakh); IPFT is therefore treated as included here and
#   the ambiguity is an open question for the planner. At Rs 0.10 per lakh (the larger of the
#   two) it would add 0.0001% per side, about 3% of the charges on a round trip.
# - SEBI turnover fee: Rs 10 per crore (0.0001%) on each side
# - stamp duty: 0.003% on the BUY side only
# - GST: 18% on brokerage + transaction charge (+ IPFT, which is combined into it)
# - DP charge: NONE (nothing is delivered)
#
# Slippage is charged per side exactly as for delivery, but intraday fills are at the next
# bar's open, so the default buckets are the delivery ones.


@dataclass(frozen=True)
class IntradayCostModel:
    """Charges for an intraday round trip; every rate is a parameter."""

    brokerage_per_order: float = 20.0
    brokerage_rate: float = 0.001  # Rs 20 or 0.1%, whichever is lower, per side
    stt_rate_sell: float = 0.00025  # sell side only
    exchange_rate: float = 0.0000307  # NSE, each side, IPFT combined
    sebi_per_crore: float = 10.0  # each side
    stamp_rate_buy: float = 0.00003  # buy side only
    gst_rate: float = 0.18  # on brokerage + transaction charge
    slippage_bps: tuple[float, float, float] = (10.0, 20.0, 40.0)
    liquid_rupees: tuple[float, float] = (100 * CRORE, 10 * CRORE)

    def brokerage(self, value: float) -> float:
        """Rs 20 per executed order or 0.1% of the order value, whichever is lower."""
        return min(self.brokerage_per_order, value * self.brokerage_rate)

    def buy_cost(self, value: float, slippage_bps: float = 0.0) -> float:
        """Rupees paid on an intraday BUY of ``value`` rupees (charges + slippage)."""
        brokerage = self.brokerage(value)
        exchange = value * self.exchange_rate
        charges = (
            brokerage
            + exchange
            + value * self.sebi_per_crore / CRORE
            + value * self.stamp_rate_buy
            + self.gst_rate * (brokerage + exchange)
        )
        return charges + value * slippage_bps / 10_000

    def sell_cost(self, value: float, slippage_bps: float = 0.0) -> float:
        """Rupees paid on an intraday SELL of ``value`` rupees (charges + slippage)."""
        brokerage = self.brokerage(value)
        exchange = value * self.exchange_rate
        charges = (
            brokerage
            + value * self.stt_rate_sell
            + exchange
            + value * self.sebi_per_crore / CRORE
            + self.gst_rate * (brokerage + exchange)
        )
        return charges + value * slippage_bps / 10_000

    def round_trip(self, entry_value: float, exit_value: float, slippage_bps: float = 0.0) -> float:
        """Total rupees of cost for one intraday trade, either side of the market.

        A long buys first and sells last; a short sells first and buys back. The charges are
        the same either way, because each side is charged on its own turnover.
        """
        return self.buy_cost(min(entry_value, exit_value), slippage_bps) + self.sell_cost(
            max(entry_value, exit_value), slippage_bps
        )

    def slippage_for(self, log_traded_value_20d: float | None) -> float:
        """Slippage in bps per side for a stock's 20-day mean traded value (unknown -> worst)."""
        return CostModel(
            slippage_bps=self.slippage_bps, liquid_rupees=self.liquid_rupees
        ).slippage_for(log_traded_value_20d)

    def breakeven_fraction(self, value: float, slippage_bps: float = 0.0) -> float:
        """The gross move a trade must make just to pay its own costs, as a share of ``value``."""
        return self.round_trip(value, value, slippage_bps) / value


DEFAULT_INTRADAY_COSTS = IntradayCostModel()
