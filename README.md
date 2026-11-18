# market-intel — Personal Market Intelligence (India)

> Personal learning project. Not investment advice. The author is not a
> SEBI-registered investment adviser or research analyst. Nothing here is a
> recommendation to buy or sell any security; do not trade based on it. This
> repository contains code and documentation only: no market data, predictions or
> signals.

Personal system that predicts, explains (SHAP + audit trail) and validates (paper trading) signals for Indian markets: stocks first, then IPOs, then mutual funds.

**Signals only, never real orders. Not investment advice.**

## Where things live

| What | Where |
|---|---|
| Code, docs, tests | Public GitHub repo `market-intel` (source-available, see License) |
| `.env` (secrets) | Nowhere. Copy it by hand to a new machine. Never commit it. |
| `drive_sync/` | Local, git-ignored folder for exports and backups |

## Setup

1. Install [uv](https://docs.astral.sh/uv/getting-started/installation/) (per-user, no admin needed).
2. Install Python and dependencies (uv reads `.python-version` and `uv.lock`):
   ```
   uv sync
   ```
3. Install the git hooks (lint, format, secret scan on every commit):
   ```
   uv run pre-commit install
   ```
4. Copy `.env.example` to `.env` and fill in values locally.

## Using this code

Run it with your own data and your own API keys. The repo ships no data: no market data, predictions, signals, model files or notebook outputs. `.gitignore`, nbstripout and a safety test keep them out. Results stay on the machine that produced them.

## Configuration

One `.env` per machine, never committed. Defaults live in code; `.env` only sets what differs.

1. Copy `.env.example` to `.env`.
2. Set `APP_PROFILE` to `thinkpad`, `hp` or `cloud`. Leave the rest empty until a task needs it.
3. Check the result (secrets show only "set" / "not set"):
   ```
   uv run python -m market_intel.config
   ```

Rules checked at startup:
- **R1:** the `thinkpad` profile refuses to start if Upstox keys are set.
- **R2:** `LLM_MONTHLY_CAP_INR` defaults to 1000; above 1000 needs `LLM_CAP_RAISE_APPROVED=true` (after an approval logged in the decision log); above 2000 is always rejected.

Real environment variables override `.env`. Set `MARKET_INTEL_ENV_FILE` to use a different file (e.g. in Docker).

## Logging and alerts

**Logs** go to `logs/market_intel.jsonl` (folder set by `LOG_DIR`, git-ignored): one JSON object per line, timestamps in UTC. A new file starts at UTC midnight; 30 days are kept. The console shows the same events in readable form, in `DISPLAY_TIMEZONE`. Level comes from `LOG_LEVEL`.

Secrets are masked in both outputs: values of fields whose names look secret (`key`, `secret`, `password`, `token`, `uri`, `url`, ...), passwords embedded in connection URLs (the part between `user:` and `@`), and any secret value loaded from `.env`.

Read the logs, e.g. in PowerShell:
```
Get-Content logs/market_intel.jsonl -Tail 20
Get-Content logs/market_intel.jsonl | ConvertFrom-Json | Where-Object level -eq "error"
```

**Alerts** (`market_intel.alerts.send_alert`): `info` is logged only; `warning` and `critical` are also emailed to `ALERT_EMAIL_TO`. Recipients appear masked (`m***@domain`) in logs and output.

- **Test alert:** `uv run python -m market_intel.alerts --test` sends one warning email (ignores the throttle). Add `--dry-run` to build it without sending.
- **Throttle:** the same alert (level + title, or an explicit `key`) is emailed at most once per `ALERT_THROTTLE_HOURS` (default 6, `0` = off). State is kept in `data/state/alert_throttle.json`. Suppressed alerts are still logged.
- **Dry-run:** set `ALERT_DRY_RUN=true` to build emails without sending, e.g. while developing.
- A failed send is retried once after 5 s, then logged as an error. It never crashes the caller.

## Databases

**Testing phase (ThinkPad):** free cloud databases, Postgres on Neon (Postgres 18) and MongoDB on Atlas M0 (MongoDB 8.0). Put `POSTGRES_URL` and `MONGODB_URI` in `.env`.

Check both connections:
```
uv run python -m market_intel.db_check
```
For each database it shows reachable, host (never user, password or URL), server version, latency (median of 3 pings), TLS (checked client-side), storage vs the free limit, and a write test that leaves nothing behind. Exit code 0 when all is well, 1 otherwise. A failure sends a critical alert; storage at 80% or more sends a warning.

**Encryption rule:** db_check refuses to connect, before sending anything, to a database that isn't on this machine unless the connection string forces TLS. For Postgres that means `sslmode=require`, `verify-ca` or `verify-full`. For MongoDB it means `mongodb+srv://`, or `mongodb://` with `tls=true`. A refusal exits 1 and sends a critical alert. Local hosts (`localhost`, `127.0.0.1`, `::1`, or a Docker service name without a dot) may connect without TLS.

Free-tier limits (checked 2026-11-13): Neon 1 GB storage, compute sleeps after 5 minutes idle; Atlas M0 512 MB storage, about 100 operations/second. Set `PG_STORAGE_LIMIT_MB` / `MONGO_STORAGE_LIMIT_MB` if they change.

**HP (from Dec 2026):** the same versions in Docker. Copy `docker/compose.env.example` to `docker/compose.env`, fill in long random values, then:
```
docker compose -f docker/compose.yaml --env-file docker/compose.env up -d
```
Ports listen on 127.0.0.1 only. The Mongo app user gets readWrite on `market_intel` only. CI starts this stack and runs `db_check` against it whenever database files change.

## Lint and tests

```
uv run ruff check .
uv run ruff format --check .
uv run pytest
uv run pre-commit run --all-files
```

CI (GitHub Actions) runs `uv sync --locked`, pre-commit and pytest on every push and pull request to `main`. pre-commit includes nbstripout, which strips outputs from notebooks before they are committed.

## Machine plan

- **ThinkPad (until end of Nov 2026):** testing and validation only, with free cloud databases. No real broker keys.
- **HP Victus (from Dec 2026):** main machine, with Docker Compose for the databases.
- **Go live** only after validation.

See [docs/decisions.md](docs/decisions.md) for the decision log.

## License

Source-available, not open source: [PolyForm Strict License 1.0.0](LICENSE.md). You may read and run it for noncommercial purposes; changing or redistributing it is not allowed. Copyright (c) 2026 Michkel Anglo.
