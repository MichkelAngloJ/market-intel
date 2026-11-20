# Project spec

The owner's brief, kept current. Changes need the owner's approval and a decision entry in the
[decision log](decisions.md).

## Goal

A personal market-intelligence system for Indian markets that predicts, explains and
validates.

- Modules, in order: 1 Stocks, 2 IPOs, 3 Mutual funds. The next module starts only after the
  current one passes validation.
- Signals and paper trading only; never real orders.

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
- Testing phase: free cloud databases. Local Docker databases run on the main laptop. A move
  to an always-free ARM cloud VM happens only when the owner decides.
- Laptops aren't always on: ingestion must detect gaps and backfill.
- Memory: container limits; no heavy training while ingestion runs.
- GPU: the main laptop's 4 GB GPU for small models; Colab's free GPU for heavier training.
- The repo is public: code and docs only.

## Cost

- Zero cost by default.
- LLMs: Gemini free tier first, Groq free tier second.
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
- Macro: Nifty 50, sector indices, India VIX, USD-INR, crude, FII/DII flows (weekly manual
  CSV import; no NSE scraping).
- News: free RSS (ET Markets, Moneycontrol, Business Standard, Mint) plus NSE/BSE corporate
  announcements. Headline and summary only, with source URL and publish time.
- Fundamentals: NSE/BSE filings, dated by filing date.
- Later:
  - IPO: subscription by category, issue details, RHP PDFs
  - mutual funds: AMFI NAV, factsheet PDFs
- Respect site terms; no aggressive scraping; never redistribute broker or exchange data.
- Universe: daily data for all actively traded NSE stocks; minute data for a watchlist (~50);
  on-demand status for any stock.

## Phases (build + learn)

- **0 Setup** (Learn: Docker)
- **1 Stock data pipeline:** Upstox daily + intraday candles, live feed, macro, news, gap
  detection and backfill, scheduler, data health checks (Learn: websockets, scheduling)
- **2 Events and features:** technical + fundamental features; LLM news-to-JSON event
  extraction; FinBERT sentiment (Learn: structured LLM extraction, FinBERT)
- **3 Stock models:** long-term + intraday; XGBoost baseline vs PyTorch LSTM; SHAP; MLflow
  (Learn: PyTorch, SHAP, MLflow)
- **4 Paper trading page** (Learn: backtesting metrics)
- **5 Online learning and anomaly detection** (unusual price/volume, possible pumps, data
  errors): Isolation Forest, then a PyTorch autoencoder (Learn: river, drift detection,
  anomaly detection)
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
