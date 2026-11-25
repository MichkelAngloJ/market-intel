# Factor map

What can move an Indian stock's price, where this project gets each factor, and when it is
built. Decision: [Factor map, manipulation flags and expert ideas](decisions.md) (2026-11-22).

There are **no fixed contribution percentages**: how much each factor matters varies by stock,
sector, horizon and market mood. General findings:

- For **large caps**, the overall market and the sector explain a large share of daily moves
  (often a third to a half or more).
- For **small caps**, company-specific news, flows and operators matter more.
- **Around results**, earnings dominate.
- **In crashes**, the market dominates.

Phase 3 measures the contributions per sector and stock with SHAP instead of assuming them.

Costs are a factor of their own intraday: an intraday round trip costs about 0.13% of the
position in charges alone (S5i), so a signal has to be worth more than that before it is
worth acting on at all.

## Factors

Status (2026-11-25): **built**: the sector map (1.1), news and exchange feeds (1.2),
per-company news (1.2b), daily candles for all NSE indices and the Nifty 500 from 2015
(S1), and features v1 built from them (S2: price and volume behaviour, overall market,
sector relative strength, market mood from India VIX, calendar, liquidity, and news
counts stored but not used for training yet). **S4i** adds the intraday path: 1-minute
candles from January 2022 for the Nifty 50 members, the Nifty 50 index, India VIX and the
matching sector indices, and intraday features v1 and triple-barrier labels built from
them (minute returns, VWAP, opening range, time-of-day volume, realised volatility,
relative strength vs the index and the sector, VIX, the previous day, time of day and
rule signals). Everything else is still to come.

| Factor group | Examples | Matters most for | Free source | Built in |
|---|---|---|---|---|
| Overall market | Nifty, global markets, breadth | intraday, days | Upstox index candles (daily: built in S1; 1-minute: S4i) | S1, S4i, 1.5–1.6 |
| Sector | sector index, peer moves | minutes–weeks | sector map + equal-weight sector return from our candles (built in S2) + NSE sector index minute candles (S4i) | 1.1, S2, S4i |
| Price and volume behaviour | trend, momentum, gaps, 52-week high, volatility | minutes to months | candles (daily: built in S1; 1-minute: S4i) | S1, S2, S4i, 1.5 |
| Delivery % | real buying vs same-day churn | days; pump detection | NSE daily bhavcopy | 1.6b |
| Order book | buy/sell imbalance, large orders | minutes | Upstox live feed (recorded from 1.7; no history exists) | 1.7 |
| Rule patterns | opening-range breakout, VWAP trend and reversion, gaps, volume bursts, relative strength | minutes | our own 1-minute candles (built in S5i as the model's baselines) | S5i |
| Trading costs | brokerage, STT, exchange, SEBI, stamp duty, GST, slippage | all (decides whether a signal is worth taking) | the brokers' published charges (intraday: built in S5i; delivery: S3) | S3, S5i |
| Results | surprise, margins, guidance | days around results; months | NSE/BSE results feeds + filing PDFs | 1.2, Phase 2 |
| Corporate events | order wins, dividends, splits, buybacks, M&A, management change | days | NSE/BSE announcement feeds | 1.2 |
| Promoter and insider activity | promoter buying/selling, pledges | weeks–months | NSE insider-trading feed; shareholding filings | 1.2, Phase 2 |
| Big money | FII/DII flows, bulk/block deals | days–months | NSE bulk/block files; FII/DII | 1.6, 1.6b |
| Fundamentals and valuation | P/E, debt, ROE, cash flow | months–years | filings | Phase 2 |
| Macro and policy | RBI rates, inflation, budget, policy, crude, USD-INR | days–months | macro data + news | 1.6 |
| Global and geopolitics | war, US Fed, commodities | days | news + global indices | 1.2, 1.6 |
| Market mood | India VIX, risk-on/off | all horizons | Upstox (India VIX level and change: built in S2; 1-minute level: S4i) | S2, S4i, 1.6 |
| News and sentiment | tone, surprise, amount of coverage | hours–days | RSS feeds (1.2) + per-company news from Google News RSS for the watchlist and recent filers (1.2b) + FinBERT/LLM | 1.2, 1.2b, Phase 2 |
| Surveillance risk | ASM/GSM lists, circuit limits, series BE | small caps | NSE lists; series BE | 1.6b, Phase 5 |
| Calendar | results season, F&O expiry week, budget day | days | exchange calendar (day of week, month: built in S2; minutes since the open and day of week intraday: S4i) | S2, S4i, Phase 2 |
| Liquidity | traded value, bid-ask spread | all (costs, slippage) | candles (20-day traded value: built in S2), order book | S2, 4 |

Every factor is used with its **available_at** time (when this system could know it), never
the time of the event itself, so features never see the future.

## Manipulation patterns (Phase 5)

| Pattern | What it looks like |
|---|---|
| Pump and dump | A price and volume jump with no news or filing, low delivery %, small free float, then a sharp fall |
| Circular / wash trading | Huge volume, low delivery %, repeated bulk deals by the same entities |
| Operator-led rise | Steady upper circuits on thin volume, a move to series BE, then ASM/GSM |
| Spoofing / layering | Big orders that appear and vanish in the order book (needs live depth from 1.7) |

History labels come from exchange surveillance list entries and SEBI enforcement orders.

**Use:** a risk flag to **avoid** or down-weight a stock. Never to join or ride a suspected
pump: that can breach SEBI's rules on fraudulent and unfair trade practices.

## Expert ideas as features

Ideas from experienced traders are **hypotheses**, for example:

- breakouts with high delivery %
- relative strength against the sector
- volume confirming the price move
- post-results drift
- promoter buying after a fall
- expiry-week behaviour
- "news but no price reaction"

Each is tested walk-forward, after costs, with a final untouched test period, because testing
many ideas makes some look good by luck. Only ideas that beat the baselines survive.
