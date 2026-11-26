# Project spec

The owner's brief, kept current. Changes need the owner's approval and a decision entry in the
[decision log](decisions.md).

## Goal

A personal market-intelligence system for Indian markets that predicts, explains and
validates.

- Modules, in order (owner, 2026-11-25): 1 Intraday, 2 Mutual funds, 3 Long-term stocks,
  4 IPOs (it was stocks, IPOs, mutual funds). The next module starts only after the current
  one passes validation.
- Every module takes the owner's capital amount through one shared sizing component (built
  in S5i): risk per trade for intraday, an amount per stock with at least one share, fund
  splits with minimum amounts, the IPO minimum lot.
- Signals and paper trading only; never real orders.
- Objective (owner, 2026-11-22): maximise profit after all costs with capped loss. Nothing
  is guaranteed, and the system says so.

## Roles

- The owner makes every final decision (tools, methods, scope, order).
- The planner (a Claude chat):
  - plans and explains options
  - warns about money loss, data leakage, overfitting, legal/SEBI issues, security and cost
  - writes task prompts and reviews reports
  - never writes project code
- Claude Code implements one task prompt per session.

## Infrastructure

- Portable: env-based config, Docker Compose for local databases, ARM-compatible images;
  changing machines needs no code changes.
- PostgreSQL: prices, features, predictions, paper trades. MongoDB: news, LLM extractions,
  audit records.
- Testing phase: free cloud databases, used only by the testing laptop and deleted in 0.11.
  Local Docker databases run on the main laptop; copies away from it come from copying the
  backup folder to external storage. A move to an always-free ARM cloud VM happens only when
  the owner decides.
- Laptops aren't always on: ingestion must detect gaps and backfill.
- Memory: container limits; no heavy training while ingestion runs.
- GPU: the main laptop's 4 GB GPU for small models; Colab's free GPU for heavier training.
- The repo is public: code and docs only.
- AI access to project data: AI tools query project data only through an MCP connection,
  free options only (an existing free MCP server or one built in this repo), with a
  read-only login. The concrete server is chosen in a later task.

## Cost

- Zero cost by default.
- LLMs: a local LLM first (Ollama, small models on the 4 GB GPU), then the Gemini free tier,
  then the Groq free tier (decision 2026-11-22). Accuracy is measured before any model is
  trusted.
- Paid fallback only with a hard cap of Rs 1,000/month enforced in code, spend logged, a
  warning at 80%. Rs 2,000 needs the owner's explicit approval, logged first.

## Data sources

- Primary: the Upstox API (free; needs an Upstox account):
  - live websocket feed
  - V3 historical candles (minute/hour from Jan 2022; daily/weekly/monthly from Jan 2000)
  - daily OAuth token login with a clear alert when it expires
  - a rate limiter
- Backup: yfinance (.NS), daily stock data only, when Upstox fails. Plus a macro-only
  exception for series Upstox lacks (USD-INR spot, crude).
- Every row is tagged with its source; one data-source interface for all sources.
- Macro: Nifty 50, sector indices, India VIX, USD-INR, crude, FII/DII flows (a manual CSV
  import for now; 1.6 revisits it).
- News: free RSS (ET Markets, Business Standard, Mint) plus the official NSE/BSE
  announcement, results, board-meeting and insider-trading feeds (1.2). Headline and summary
  only, with source URL, publish time, fetch time and `available_at`. Moneycontrol is
  excluded: it blocks non-browser clients.
- Per-company news: Google News RSS for the watchlist, at a polite rate (1.2b); never
  search-page scraping.
- Official NSE daily files (bulk/block deals, bhavcopy delivery %, ASM/GSM surveillance
  lists): downloaded once a day with an honest identity, stopping with an alert if blocked
  (1.6b).
- Fundamentals: NSE/BSE filings, dated by filing date.
- Later:
  - IPO: subscription by category, issue details, RHP PDFs
  - mutual funds: AMFI NAV, factsheet PDFs
- **Legitimate channels only** (decision 2026-11-22): RSS feeds, official files, APIs and
  pages the owner captures. The code uses an honest identity and never imitates a browser
  or works around blocks, CAPTCHAs or logins. Respect site terms; no aggressive scraping;
  never redistribute broker or exchange data. Every source is watched by source-health
  alerts.
- Universe: daily data for all actively traded NSE stocks; minute data for a watchlist (~50);
  on-demand status for any stock.

## Phases (build + learn)

- **0 Setup** (Learn: Docker)
- **1 Stock data pipeline:** Upstox daily + intraday candles, live feed, macro, news, gap
  detection and backfill, scheduler, data health checks (Learn: websockets, scheduling)
- **2 Events and features:** technical + fundamental features; LLM news-to-JSON event
  extraction (a local LLM via Ollama first); FinBERT sentiment; filing-PDF extraction with
  OCR when needed; page capture (below); the expert-feature catalogue
  ([factor map](factors.md)) (Learn: structured LLM extraction, FinBERT)
- **3 Stock models:** long-term + intraday; XGBoost baseline vs PyTorch LSTM; SHAP; MLflow
  (Learn: PyTorch, SHAP, MLflow)
- **4 Paper trading page** (Learn: backtesting metrics)
- **5 Online learning and anomaly detection** (unusual price/volume, possible pumps, data
  errors): Isolation Forest, then a PyTorch autoencoder; manipulation risk flags (pump and
  dump, wash trading, operator-led rises, spoofing) learned from exchange surveillance lists
  and SEBI enforcement orders, used only to avoid or down-weight stocks (Learn: river,
  drift detection, anomaly detection)
- **6 IPO:** listing-gain + 3/6-month performance model; RAG over RHP PDFs. Never predict
  allotment (it's a lottery).
- **7 Mutual funds:** rank and screen from numeric metrics (rolling returns, Sharpe/Sortino,
  drawdown, expense ratio, consistency) + factsheet text (holdings changes, manager
  commentary). Don't predict NAV.
- **8 Agent layer:** a LangGraph agent ("why did X move today?") with DB tools + a code-level
  MCP server over the DB. Optional: LoRA fine-tune FinBERT on Indian financial news.
- **9 Optional experiment:** reinforcement learning (the planner warns about its limits
  first).

## Features and labels

- Event features from news via LLM extraction (structured JSON):
  - war/conflict (0/1)
  - government policy change (0/1 + direction)
  - RBI rate action
  - budget announcements
  - company investment or order win (Rs amount + sign)
  - results surprise
  - promoter buying/selling
  - sentiment score
- Every event stores when it became PUBLIC. Features use only information available before
  prediction time; a leakage test must fail the build if violated.
- Labels: forward return % (regression) + up/down class. Intraday horizons 15 min / 1 hour /
  end of day; long-term 1 / 3 / 6 months.
- No single model for all companies: per sector or volatility cluster, plus dedicated models
  only for large, liquid stocks with enough history. Warn when data is too thin.

### Page capture (Phase 2)

For crucial decisions (decision 2026-11-22):

1. **Automatic capture:** a browser saves pages to PDF only where automated access is
   allowed (robots and terms respected), and stops at any block.
2. **Evidence requests:** the system lists exact URLs (checked automatically where allowed,
   marked valid or invalid by the owner on screen); the owner saves the pages as PDF; the
   system takes them from an upload/file picker or from the Downloads folder (only new PDFs
   saved after an open request, and only after the owner confirms the match).

The capture time counts as `available_at`. Captured pages stay on the laptop for personal
use; they are never committed or shared.

### Intraday design

Decision 2026-11-22:

- A fast numeric path: a small model (e.g. LightGBM) on 1-minute features (price, volume,
  order-book imbalance, market and sector moves, VIX, time of day, the latest background
  news score), with millisecond inference and no LLM in the live decision.
- Labels use the triple-barrier method: target, stop-loss or time limit, whichever comes
  first.
- Every signal is a full trade plan: entry, target, stop-loss, exit time, quantity, maximum
  loss, probability and expected gain after all costs. It is "no trade" unless the
  expected gain after costs is positive.
- Fixed risk per trade, a daily loss limit and a maximum number of trades.
- 15 minutes is the main horizon; 5 minutes is an experiment.
- Measured result (S5i-c, 2022-2026, 107 liquid but volatile NSE stocks): the rule
  strategies still have no gross edge (about -0.06 bps a trade before slippage against
  an 8.3 bps charge floor). The meta-model, whose expected-value gate selects moments
  where the ATR-scaled target is wide relative to the fee, did pay over the
  walk-forward (+9.18 bps a trade) but its edge decays to negative by 2026 and the
  untouched holdout loses. Regime-dependent, not a current edge; nothing promoted.
- Measured result (S5i and S5i-b, 2022-2026 Nifty 50 minute data): no rule strategy and no
  meta-model beats "no trading" after costs, at any slippage down to zero, at any hold
  from 5 bars to the session close, and in no confidence band including the model's most
  confident 1%. Intraday on large-cap cash equity is not viable at these charges; the
  tools built for it are reusable on a wider, more volatile universe.
- Strategies, backtest and sizing (S5i, decision 2026-11-25): eight rule strategies are the
  baselines for a LightGBM meta-model on the triple-barrier labels (fixed v1 and
  volatility-scaled v2), judged by a monthly walk-forward and a holdout scored once, in an
  event-driven backtest after the brokers' current intraday charges. "No trading" is one of
  the baselines and is allowed to win.
- Data, features and labels (S4i, decision 2026-11-25): Nifty 50 stocks, Nifty 50, India
  VIX and matching sector indices as 1-minute candles from January 2022; decisions every 5
  minutes 09:30-15:00, entry at the next bar's open, everything closed by 15:20; features
  and labels as Parquet with a build record (see [architecture](architecture.md)).
- Order-book depth is recorded from 1.7, because no history exists.
- SEBI's study found that 71% of individual intraday traders in the cash segment lost
  money in FY2023; everything is paper-traded first and promotion needs the owner's
  approval.

## Modeling rules

- Walk-forward / time-based validation only; no random splits.
- Always compare with baselines: buy-and-hold, naive last value, a simple moving-average
  rule.
- Results are reported after brokerage, STT, other charges and slippage.
- A model that doesn't beat its baseline is not promoted, and this is said plainly.
- Version every model. Promotion to active needs validation results, paper-trading results
  and the owner's approval.

## Paper trading

- Dummy trades from signals with realistic fees and slippage; no real orders.
- Per model, sector and horizon: hit rate, precision/recall, profit after costs, max
  drawdown, Sharpe, vs buy-and-hold, calibration. A rolling-window view shows accuracy decay.

## Online learning

- Scheduled rolling-window retraining, plus incremental updates where suitable.
- Drift detection on features and accuracy, with alerts.
- A retrained model replaces the active one only if it beats it on the latest out-of-sample
  window; the comparison is logged.

## Logging and audit

- Every prediction writes an audit record (MongoDB) with:
  - timestamp, instrument, model + version, horizon, data source
  - the feature snapshot and the top SHAP reasons in plain language
  - news events used (URLs + publish times)
  - LLM calls (model, prompt version, cost)
  - signal + confidence
  - the actual outcome and error, filled in later
- Structured JSON logs for ingestion, training, errors, token expiry and rate-limit hits.
- Audit page: open any prediction, see why it was made and whether it was right.

## Safety and regulation

- Signals and paper trading only. No automatic real orders unless the owner decides later;
  before that, the planner explains SEBI's retail algo framework and the risks.
- Public repo:
  - code and docs only; never commit data, predictions, signals, model files, notebook
    outputs or results
  - the dashboard runs only on the owner's laptop
  - no public accuracy or return claims; results are shown only in person
  - disclaimer in the README and the dashboard

## Interface

Streamlit dashboard + FastAPI backend. Pages: Today's signals, Stock detail, Paper trading,
Audit, Data health (gaps, last update, token status, API errors, LLM spend). Later: IPO,
Mutual funds.

## Definition of done (per phase)

- Code and tests pass.
- README and docs updated; decisions.md updated.
- An honest review: what works, what doesn't, risks, what to do differently.
