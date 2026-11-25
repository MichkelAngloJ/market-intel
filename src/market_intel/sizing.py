"""Position sizing from the owner's capital — the one shared component every module uses
(sprint S5i; decision 2026-11-25).

Intraday is built here; the other modules get the same treatment when they arrive:

- intraday: risk a fixed share of capital per trade, cap the exposure, stop for the day after
  a loss limit, and never take more than a few trades a day;
- long-term stocks: an amount per stock, but at least one whole share;
- mutual funds: split an amount across funds, respecting each fund's minimum;
- IPO: whole lots only.

Nothing here places an order. A plan is a *proposal*: entry, target, stop, exit time,
quantity, the worst loss it can cause, the probability it was sized from, and the expected
gain AFTER all costs. A plan whose expected gain after costs is not positive is not a trade,
and :func:`plan_intraday_trade` returns the reason instead.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import time

from market_intel.costs import DEFAULT_INTRADAY_COSTS, IntradayCostModel

FLAT_BY = time(15, 20)  # the intraday timing convention (S4i)
SIDES = ("long", "short")


@dataclass(frozen=True)
class SizingPolicy:
    """What the owner is willing to risk. Rates are fractions of capital."""

    capital: float
    risk_per_trade: float = 0.01  # 1% of capital at risk if the stop is hit
    daily_loss_limit: float = 0.02  # stop trading for the day after losing 2%
    max_trades_per_day: int = 5
    allow_short: bool = True
    max_exposure_per_trade: float = 1.0  # at most this share of capital in one position
    min_quantity: int = 1

    def __post_init__(self) -> None:
        if self.capital <= 0:
            raise ValueError("capital must be positive")
        for name in ("risk_per_trade", "daily_loss_limit", "max_exposure_per_trade"):
            value = getattr(self, name)
            if not 0 < value <= 1:
                raise ValueError(f"{name} must be in (0, 1]")
        if self.max_trades_per_day < 1:
            raise ValueError("max_trades_per_day must be at least 1")

    @property
    def risk_amount(self) -> float:
        return self.capital * self.risk_per_trade

    @property
    def loss_limit_amount(self) -> float:
        return self.capital * self.daily_loss_limit


@dataclass
class DayState:
    """What has already happened today, so the limits can be enforced in order."""

    trades: int = 0
    realised: float = 0.0  # rupees, negative when losing

    def record(self, profit: float) -> None:
        self.trades += 1
        self.realised += profit

    def blocked(self, policy: SizingPolicy) -> str | None:
        """Why no further trade may be taken today, if any."""
        if self.trades >= policy.max_trades_per_day:
            return "max trades reached"
        if self.realised <= -policy.loss_limit_amount:
            return "daily loss limit reached"
        return None


@dataclass(frozen=True)
class OutcomeOdds:
    """How a triple-barrier trade is expected to end.

    ``target`` and ``stop`` are the probabilities that each barrier is hit first; whatever is
    left is a time-limit exit, worth ``time_limit_return`` as a fraction of the entry price
    (0 by default: a flat exit). Passing only ``target`` assumes every other outcome is the
    stop, which is the most pessimistic reading.
    """

    target: float
    stop: float | None = None
    time_limit_return: float = 0.0

    def __post_init__(self) -> None:
        if not 0 <= self.target <= 1:
            raise ValueError("target probability must be in [0, 1]")
        if self.stop is not None and not 0 <= self.stop <= 1:
            raise ValueError("stop probability must be in [0, 1]")
        if self.target + self.p_stop > 1 + 1e-9:
            raise ValueError("target and stop probabilities add to more than 1")

    @property
    def p_stop(self) -> float:
        return 1.0 - self.target if self.stop is None else self.stop

    @property
    def p_time(self) -> float:
        return max(0.0, 1.0 - self.target - self.p_stop)


@dataclass(frozen=True)
class TradePlan:
    """A proposed intraday trade. ``taken`` is False when the rules refuse it."""

    symbol: str
    side: str
    entry: float
    target: float
    stop: float
    quantity: int
    max_loss: float  # rupees, including the costs of getting out
    probability: float  # P(target first), as sized
    expected_gain: float  # rupees after ALL costs
    costs: float  # rupees of charges + slippage for the round trip
    exit_time: time = FLAT_BY
    taken: bool = True
    reason: str | None = None  # why not, when taken is False
    details: dict[str, float] = field(default_factory=dict)

    @property
    def value(self) -> float:
        return self.entry * self.quantity

    def lines(self) -> list[str]:
        if not self.taken:
            return [f"{self.symbol} {self.side}: no trade ({self.reason})"]
        return [
            f"{self.symbol} {self.side} x{self.quantity} at {self.entry:.2f}"
            f" (Rs {self.value:,.0f})",
            f"  target {self.target:.2f}, stop {self.stop:.2f}, flat by {self.exit_time:%H:%M}",
            f"  P(target) {self.probability:.0%}; worst loss Rs {self.max_loss:,.0f};"
            f" costs Rs {self.costs:,.0f}; expected gain after costs Rs {self.expected_gain:,.2f}",
        ]


def no_trade(symbol: str, side: str, reason: str, **details: float) -> TradePlan:
    return TradePlan(
        symbol=symbol, side=side, entry=0.0, target=0.0, stop=0.0, quantity=0, max_loss=0.0,
        probability=0.0, expected_gain=0.0, costs=0.0, taken=False, reason=reason,
        details=details,
    )  # fmt: skip


def quantity_for(
    policy: SizingPolicy, entry: float, stop: float, *, lot: int = 1
) -> tuple[int, str | None]:
    """Shares to trade: the risk amount divided by the distance to the stop, capped by the
    exposure limit and by the capital itself. Returns (quantity, why it is zero)."""
    distance = abs(entry - stop)
    if entry <= 0:
        return 0, "entry price is not positive"
    if distance <= 0:
        return 0, "stop is at the entry price"
    by_risk = policy.risk_amount / distance
    by_exposure = policy.capital * policy.max_exposure_per_trade / entry
    quantity = int(math.floor(min(by_risk, by_exposure) / lot) * lot)
    if quantity < policy.min_quantity:
        return 0, "position smaller than one share at this risk"
    return quantity, None


def plan_intraday_trade(
    symbol: str,
    side: str,
    entry: float,
    *,
    target_pct: float,
    stop_pct: float,
    odds: OutcomeOdds,
    policy: SizingPolicy,
    day: DayState | None = None,
    costs: IntradayCostModel = DEFAULT_INTRADAY_COSTS,
    slippage_bps: float = 0.0,
    exit_time: time = FLAT_BY,
    blocked: str | None = None,
    require_positive_ev: bool = True,
) -> TradePlan:
    """Size one intraday trade and decide whether it is worth taking.

    ``target_pct`` and ``stop_pct`` are distances from the entry, in percent. The expected
    gain is the probability-weighted gross result of the three barrier outcomes minus every
    charge and the slippage of both sides.

    With ``require_positive_ev`` (the default) a trade whose expected gain is not positive is
    refused. A rule baseline passes False: it should trade its own signal, so the backtest
    measures the rule itself and not a filtered version of it. Either way the expected gain is
    on the plan.
    """
    if side not in SIDES:
        raise ValueError(f"side must be one of {SIDES}")
    if side == "short" and not policy.allow_short:
        return no_trade(symbol, side, "shorting is switched off")
    if blocked:
        return no_trade(symbol, side, blocked)
    if day is not None and (why := day.blocked(policy)):
        return no_trade(symbol, side, why)
    if entry <= 0:
        return no_trade(symbol, side, "entry price is not positive")

    sign = 1.0 if side == "long" else -1.0
    target = entry * (1 + sign * target_pct / 100)
    stop = entry * (1 - sign * stop_pct / 100)
    quantity, why = quantity_for(policy, entry, stop)
    if not quantity:
        return no_trade(symbol, side, why or "no quantity", entry=entry, stop=stop)

    value = entry * quantity
    win_per_share = abs(target - entry)
    loss_per_share = abs(entry - stop)
    # charges are taken on the larger of the two turnovers, so cost the worst case
    cost = costs.round_trip(value, max(target, stop) * quantity, slippage_bps)
    gross = (
        odds.target * win_per_share * quantity
        - odds.p_stop * loss_per_share * quantity
        + odds.p_time * odds.time_limit_return * sign * value
    )
    expected = gross - cost
    plan = TradePlan(
        symbol=symbol,
        side=side,
        entry=entry,
        target=target,
        stop=stop,
        quantity=quantity,
        max_loss=loss_per_share * quantity + cost,
        probability=odds.target,
        expected_gain=expected,
        costs=cost,
        exit_time=exit_time,
        details={
            "gross_expected": gross,
            "breakeven_pct": costs.breakeven_fraction(value, slippage_bps) * 100,
            "value": value,
        },
    )
    if expected <= 0 and require_positive_ev:
        return no_trade(
            symbol,
            side,
            "expected gain after costs is not positive",
            expected_gain=expected,
            costs=cost,
            quantity=quantity,
        )
    return plan


# --- the other modules (stubs with tests; they grow when those modules are built) ---------


def shares_for_amount(amount: float, price: float) -> int:
    """Long-term stocks: whole shares for an amount, at least one if it is affordable."""
    if price <= 0 or amount < price:
        return 0
    return int(amount // price)


def fund_split(amount: float, funds: list[str], minimums: dict[str, float]) -> dict[str, float]:
    """Mutual funds: split ``amount`` evenly, dropping funds whose minimum cannot be met and
    sharing their part out again. Returns {fund: rupees}; empty when nothing fits."""
    chosen = list(funds)
    while chosen:
        each = amount / len(chosen)
        too_small = [f for f in chosen if each < minimums.get(f, 0.0)]
        if not too_small:
            return {f: round(each, 2) for f in chosen}
        chosen = [f for f in chosen if f not in too_small[:1]]
    return {}


def ipo_lots(amount: float, lot_value: float) -> int:
    """IPO: whole lots only; one lot is the minimum application."""
    if lot_value <= 0 or amount < lot_value:
        return 0
    return int(amount // lot_value)
