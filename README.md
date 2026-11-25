# market-intel — Personal Market Intelligence (India)

> Personal learning project. Not investment advice. The author is not a
> SEBI-registered investment adviser or research analyst. Nothing here is a
> recommendation to buy or sell any security; do not trade based on it. This
> repository contains code and documentation only: no market data, predictions or
> signals.

A personal system that predicts, explains (SHAP and an audit trail) and validates (paper
trading) signals for Indian markets: stocks first, then IPOs, then mutual funds. **Signals
only, never real orders.**

**Status:** Phase 0 (foundations) is complete (tag `phase-0`): configuration, logging and
alerts, Postgres and MongoDB with limited logins, migrations, validated collections, a daily
health check, local Docker databases with tested backups, and a Colab template. Now: Phase 1,
the data pipeline; the groundwork, the daily NSE instrument list with a watchlist, and the
news and exchange feeds with source-health alerts are in place.

## Docs

- [Architecture](docs/architecture.md): how the system works (diagrams, where data lives,
  security model, schema, health check).
- [Runbook](docs/runbook.md): how to operate it (daily routine, fixing health-check problems,
  setting up a laptop, secrets, migrations).
- [Decision log](docs/decisions.md): every decision, with concerns and reasons.
- [Spec](docs/spec.md): the project brief (goal, data sources, phases, modeling rules).
- [Factor map](docs/factors.md): what moves Indian stock prices, the free source for each
  factor and when it's built; manipulation patterns; expert ideas as tested hypotheses.
- [Workflow](docs/workflow.md): how the owner, the planner and Claude Code work together.
- [Progress](docs/progress.md): task status, next task and carry-forward items.
- [Phase 0 review](docs/reviews/phase-0.md): what works, what doesn't, risks, what to do
  differently.
- [License](LICENSE.md).

[CLAUDE.md](CLAUDE.md) holds the standing rules that Claude Code loads in every session.

## Quick setup

1. Install [uv](https://docs.astral.sh/uv/getting-started/installation/) (per user).
2. Clone this repo and open a terminal in it.
3. Install Python 3.12 and the locked dependencies: `uv sync`
4. Install the git hooks (lint, format, secret scan, notebook stripping):
   `uv run pre-commit install`
5. Copy `.env.example` to `.env` and set `APP_PROFILE` (`restricted`, `full` or `server`; see
   [Laptops and profiles](docs/architecture.md#laptops-and-profiles)). Leave other values
   empty until a task needs them; never commit `.env`.
6. Run the health check: `uv run python -m market_intel.health`

Details, including how to copy secrets safely, are in the
[runbook](docs/runbook.md#setting-up-a-new-windows-laptop).

## Daily commands

| Command | What it does |
|---|---|
| `uv run python -m market_intel.health` | Checks everything; OK / WARN / FAIL / SKIP per check (`--json` for machines) |
| `uv run python -m market_intel.db_check` | Connection, TLS, storage and write test for both databases |
| `uv run python -m market_intel.compute_check` | CPU, RAM, disk and NVIDIA GPU of this machine |
| `uv run pytest` | Runs the tests (also `uv run pre-commit run --all-files`) |
| `uv run alembic upgrade head` | Applies Postgres migrations with the owner login, only after CI is green |
| `uv run python -m market_intel.mongo_setup` | Creates or updates MongoDB collections with the setup login |
| `uv run python -m market_intel.backup` | Backs up the local Docker databases outside the repo (`--verify`: restore test); `full` profile only |
| `uv run python -m market_intel.instruments refresh` | Refreshes NSE stocks (EQ, BE) and indices from Upstox's public file (`--dry-run` saves nothing) |
| `uv run python -m market_intel.news fetch` | Fetches the RSS news and NSE/BSE announcement feeds politely into MongoDB (`status`: per-feed health) |
| `uv run python -m market_intel.news company-fetch` | Per-company news (Google News RSS) for the watchlist and recent filers; politely rate-limited, stops for the day if blocked |
| `uv run python -m market_intel.upstox login` | The daily Upstox login in the browser (token kept in Windows Credential Manager; `status` shows the hours left) |
| `uv run python -m market_intel.candles daily` | Daily candles for all NSE indices and the Nifty 500 (Upstox, yfinance backup for stocks), with gaps and resume |
| `uv run python -m market_intel.features build` | Features v1 for the Nifty 500 (then `labels`; `check-leakage` runs the leakage test) |
| `uv run python -m market_intel.candles intraday` | 1-minute candles for the Nifty 50, its index, India VIX and sector indices from 2022 (Upstox; resumable) |
| `uv run python -m market_intel.intraday features build` | Intraday features v1 as Parquet (then `labels build`; `check-leakage` runs the intraday leakage test) |
| `uv run python -m market_intel.models train --horizon 21` | Walk-forward models vs baselines after costs, SHAP, MLflow, candidate versions (`report` shows the tables) |
| `uv run python -m market_intel.watchlist list` | Shows the watchlist (`add SYMBOL`, `remove SYMBOL`; max 50, stored only in the database) |

The daily routine and what to do when a check fails are in the
[runbook](docs/runbook.md#daily-routine).

## Using this code

Run it with your own data and your own API keys. The repo ships no data: no market data,
predictions, signals, model files or notebook outputs. `.gitignore`, nbstripout and a safety
test keep them out. Results stay on the machine that produced them.

## License

Source-available, not open source: [PolyForm Strict License 1.0.0](LICENSE.md). You may read
and run it for noncommercial purposes; changing or redistributing it is not allowed.
Copyright (c) 2026 Michkel Anglo.
