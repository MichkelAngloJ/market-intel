# Runbook

How to operate market-intel day to day. For how it works see the
[architecture guide](architecture.md); for why, see the [decision log](decisions.md).

All commands run from `<repo folder>` with `uv run` in front.

## Daily routine

1. `git pull`
2. Read [docs/progress.md](progress.md): the current task and the carry-forward items.
3. **On the `full` profile (main laptop):** start Docker Desktop, then the local databases
   (`docker compose -f docker/compose.yaml --env-file docker/compose.env up -d --wait`; see
   [Local Docker databases](#local-docker-databases)).
4. `uv run python -m market_intel.health` and fix anything that isn't OK or SKIP (see
   [Reading the health output](#reading-the-health-output)).
5. **Data (until the scheduler, S6, does it):** each morning
   `uv run python -m market_intel.upstox login` (see
   [Upstox login and daily candles](#upstox-login-and-daily-candles)); after 18:00 IST
   `uv run python -m market_intel.candles daily`, then `features build` and `features labels`
   (see [Features and labels](#features-and-labels)); once a day after ~06:00 IST
   `uv run python -m market_intel.instruments refresh` (see
   [Instruments and watchlist](#instruments-and-watchlist)); news whenever you like,
   `uv run python -m market_intel.news fetch` (see
   [News feeds and source health](#news-feeds-and-source-health)). Your real runs send real
   alerts; `ALERT_DRY_RUN=true` is only for Claude Code's tests and demos.
6. Work.
7. `uv run pytest` and `uv run pre-commit run --all-files`.
8. `git push`, then check that CI is green.
9. **On the `full` profile, after a work day:** back up
   (`uv run python -m market_intel.backup`; see [Local Docker databases](#local-docker-databases)).
10. **On the `full` profile:** when you're done with the databases, stop them and quit Docker
    Desktop (see [Local Docker databases](#local-docker-databases)).
11. **Weekly:** copy `BACKUP_DIR` to external storage (an external drive or your own cloud
    storage). Once the cloud databases are deleted (0.11), these copies are the only ones away
    from the main laptop.

**Alternating laptops (testing laptop and main laptop):** the repo is the only thing the two
laptops share; each has its own planner chat and Claude Code, with separate memories (see the
[workflow](workflow.md#shared-memory-is-the-repo)).

- Never work on both at once.
- Always `git pull` first, then read [docs/progress.md](progress.md), work, run the tests and
  pre-commit, and `git push` last, so the other laptop starts from the latest state.
- Update [docs/progress.md](progress.md) at the end of every task, before the last push.
- `.env` is not synced: each laptop has its own.

Useful extras:

| Task | Command |
|---|---|
| Show the settings (secrets show only "set" / "not set") | `uv run python -m market_intel.config` |
| Check both databases in detail | `uv run python -m market_intel.db_check` |
| What this machine can compute | `uv run python -m market_intel.compute_check` |
| Send a test alert email (ignores the throttle) | `uv run python -m market_intel.alerts --test` |
| Build the test alert without sending it | `uv run python -m market_intel.alerts --test --dry-run` |
| Lint and format checks | `uv run ruff check .` and `uv run ruff format --check .` |
| Last 20 lines of one component's log (PowerShell) | `Get-Content logs/health.jsonl -Tail 20` |
| Only errors (PowerShell) | `Get-Content logs/health.jsonl \| ConvertFrom-Json \| Where-Object level -eq "error"` |

Each program has its own log file; see [Logs and ingestion runs](#logs-and-ingestion-runs).

## Reading the health output

OK and SKIP need nothing. WARN needs attention soon. FAIL sends one alert email (throttled) and
needs a fix now.

| Check and status | Likely cause | Fix |
|---|---|---|
| `config` FAIL | `POSTGRES_URL` or `MONGODB_URI` missing in `.env` | Add them (see [Setting up a new Windows laptop](#setting-up-a-new-windows-laptop)) |
| `config` WARN | Alert settings missing | Add the `ALERT_*` settings, or accept that no emails are sent |
| `config_upstox` / `config_llm` WARN | Only some of the group's settings are set | Set all of them, or none |
| `databases` FAIL, both unreachable on the `full` profile | Docker Desktop or the local databases aren't running | Start them (see [Local Docker databases](#local-docker-databases)) and run health again |
| `databases` FAIL, MongoDB unreachable | Not a network list: the cloud MongoDB accepts any address (decision 2026-11-20); a connection failure means a password, TLS or service problem | Check the password in `.env`, the TLS options in the connection string and the provider's status page |
| `databases` FAIL, Postgres unreachable | The database was asleep and the first connect timed out; or a password changed | Run health again; if it still fails, check the password in `.env` |
| `databases` FAIL, no TLS | A connection string without TLS to a remote host | Add `sslmode=require` (Postgres) or use `mongodb+srv://` / `tls=true` (MongoDB) |
| `databases` WARN, storage | The free tier is 80% full | Clean up or plan a bigger tier |
| `pg_migrations` WARN, behind | New migrations in the code | `uv run alembic upgrade head` with the owner login, after CI is green |
| `pg_migrations` FAIL, unknown version | The database is newer than this laptop's code | `git pull` |
| `mongo_setup_drift` WARN | A collection, validator or index differs from `schemas.py` | `uv run python -m market_intel.mongo_setup` |
| `mongo_rights_probe` FAIL | The app role's rights differ from the design | Check the role `mi_app_rw` in the provider console: one row per collection, no remove on `prediction_audit`, nothing database-wide except listCollections, dbStats and killAnyCursor |
| `mongo_rights_probe` FAIL, created a collection | The app login could create collections | Fix the role, then drop the named stray collection in the provider console (the app login can't drop) |
| `unexpected_collections` WARN | A collection that isn't in `schemas.py` | Find where it came from; drop it in the provider console if it isn't needed |
| `laptop` WARN / FAIL | Under 10 GB / 2 GB free disk | Free up space; check the size of `logs/` and `data/` |
| `backup_age` WARN | The newest complete backup (with a manifest) is older than `BACKUP_MAX_AGE_DAYS` (default 7), or there is none | Run the backup: `uv run python -m market_intel.backup` (databases running), then copy `BACKUP_DIR` to external storage |
| `data_freshness` WARN | The last ingestion run failed | Read its error in `ingestion_runs` and the logs (from Phase 1) |
| `upstox_token` WARN | No Upstox login today, the token expired, or under 1 hour left | `uv run python -m market_intel.upstox login` |
| `sources` WARN | A feed (or later another source) has an active source-health problem | Run `uv run python -m market_intel.news status`, then see [News feeds and source health](#news-feeds-and-source-health) |
| `llm_spend` WARN / FAIL | Spend this IST month is at `LLM_WARN_PCT` / at the cap | Stop paid LLM calls; raising the cap needs a decision-log entry (rule R2) |

If several Postgres checks fail together (`databases`, `pg_migrations`, `data_freshness`,
`llm_spend`), it's usually one connection problem: health shares one connection per database.

## Setting up a new Windows laptop

1. Install Git, and [uv](https://docs.astral.sh/uv/getting-started/installation/) per user.
   Optional: the GitHub CLI (`gh`), logged in with `gh auth login`.
2. Clone the repo into `<repo folder>` and open a terminal there.
3. `uv sync` (installs Python 3.12 and the locked dependencies).
4. `uv run pre-commit install` (lint, format, secret scan and notebook stripping on every
   commit).
5. Create `.env`: copy `.env.example` to `.env`, then copy the values from the other laptop
   **securely**, with a USB drive or a password manager. **Never by email, chat or git.** Set
   `APP_PROFILE`: `restricted` on the testing laptop, `full` on the main laptop (see
   [Laptops and profiles](architecture.md#laptops-and-profiles)). Leave settings empty when
   a default is fine.
6. No network step for MongoDB: the cloud MongoDB accepts any address (decision
   2026-11-20); a connection failure means a password, TLS or service problem.
7. `uv run python -m market_intel.health`: everything should be OK or SKIP.

### Main laptop setup

The main laptop runs the `full` profile and Docker. In order:

1. **Install the tools with winget** (in a terminal):
   `winget install --id Git.Git`, `winget install --id astral-sh.uv`,
   `winget install --id GitHub.cli`, `winget install --id Microsoft.VisualStudioCode` and
   `winget install --id Docker.DockerDesktop`. Use `winget upgrade --id <id>` for tools that
   are already installed (old Docker Desktop versions have known security flaws). Docker's
   installer asks for admin approval: watch for the prompt, it may open behind other windows.
2. **Cap WSL's memory** (Docker Desktop runs in WSL): create `.wslconfig` in your user folder
   (`%UserProfile%`) with these two lines, then run `wsl --shutdown` so it takes effect:

   ```ini
   [wsl2]
   memory=6GB
   ```

3. **Clone** the repo into `<repo folder>` (a drive with enough free space) and open a
   terminal there.
4. **Set the Git identity for this repo only:** `git config user.name "<your name>"` and
   `git config user.email "<your GitHub no-reply address>"` (GitHub: Settings > Emails).
   Never a personal address: commits are public.
5. `uv sync`, then `uv run pre-commit install`.
6. **Copy `.env` securely** from the other laptop (USB drive or password manager; never by
   email, chat or git) and set `APP_PROFILE=full`.
7. **No MongoDB network step:** the cloud MongoDB accepts any address (decision
   2026-11-20); a connection failure means a password, TLS or service problem.
8. **Health check:** `ALERT_DRY_RUN=true uv run python -m market_intel.health` (in Git Bash;
   in PowerShell set `$env:ALERT_DRY_RUN = "true"` first). Everything should be OK or SKIP.
9. **Quit Docker Desktop when idle:** it holds its memory (up to the WSL cap) even when no
   container runs. Start it only when you need the local databases.
10. **Local databases:** create `docker/compose.env`, start the databases, migrate them and
    take a first backup, as in [Local Docker databases](#local-docker-databases).

## Local Docker databases

On the `full` profile (main laptop) Postgres 18 and MongoDB 8.0 run in Docker, defined in
`docker/compose.yaml`. Their passwords are in `docker/compose.env` (git-ignored; names in
`docker/compose.env.example`), and `.env` points the four database URLs at `127.0.0.1`. Start
Docker Desktop first; run everything from `<repo folder>`.

| Task | Command |
|---|---|
| Start, and wait until both are healthy | `docker compose -f docker/compose.yaml --env-file docker/compose.env up -d --wait` |
| Status (both should be `healthy`, ports on `127.0.0.1` only) | `docker compose -f docker/compose.yaml --env-file docker/compose.env ps` |
| Stop (keeps the data) | `docker compose -f docker/compose.yaml --env-file docker/compose.env down` |
| Back up | `uv run python -m market_intel.backup` |
| Restore test of the newest backup (run it AFTER a fresh backup) | `uv run python -m market_intel.backup --verify` |

Never run `docker compose config` without `--quiet`, `docker inspect` or `docker compose exec
... env`: they print the passwords.

**First start.** The init scripts in `docker/postgres-init/` and `docker/mongo-init/` create the
logins (Postgres `mi_app`; the MongoDB app and setup users with the role `mi_app_rw`) from
`docker/compose.env`. They run **only when the data volume is empty**, so editing
`docker/compose.env` later changes no password: change it in the database too (`ALTER ROLE`
in Postgres, `db.changeUserPassword()` in MongoDB), then in `.env`. After the first start:

1. `uv run alembic upgrade head` (owner login, only after CI is green).
2. `uv run python -m market_intel.mongo_setup` (setup login).
3. `ALERT_DRY_RUN=true uv run python -m market_intel.health`: everything OK or SKIP.
4. `uv run python -m market_intel.backup`, then `uv run python -m market_intel.backup --verify`.

**Where the data lives.** A container is disposable; the data lives in the named volumes
`market-intel_pgdata` and `market-intel_mongodata`.

- `docker compose ... down` removes the containers and keeps the volumes: the data survives
  the next `up`.
- **Warning: `docker compose ... down -v` deletes the volumes and with them ALL local data.**
  Never run it on the main laptop unless you mean to wipe the databases, and only after a
  verified backup has been copied off the laptop.

**Backups.**

- `uv run python -m market_intel.backup` (the databases must be running; `full` profile only;
  on other profiles it stops with a message) writes to `BACKUP_DIR`:
  - one Postgres dump (`market_intel-pg-<UTC time>.dump`, as the Postgres superuser inside
    the container),
  - one MongoDB archive (`market_intel-mongo-<UTC time>.archive.gz`, as the MongoDB setup
    user),
  - a manifest (`market_intel-manifest-<UTC time>.json`): the UTC time, the row count of every
    table and the document count of every collection taken right before each dump, the
    `pg_dump` and `mongodump` versions, and each dump file's size and SHA-256. It is written
    last; dumps without a manifest are an incomplete backup.

  The default `BACKUP_DIR` is a `market-intel-backups` folder next to the repo folder; a
  folder inside the repo is refused. It keeps the newest 7 of each kind and deletes older
  ones.
- **Take backups while nothing is writing** to the databases, so the counts in the manifest
  match the dumps exactly. There is no ingestion yet; once there is, back up between runs.
- **`--verify` does not take a backup.** It only restore-tests the newest one already in
  `BACKUP_DIR`, so run it right after `backup`; otherwise it checks an older file and says
  nothing about today's data. (The S4i check first passed against a backup taken before the
  1-minute candles existed.)
- `uv run python -m market_intel.backup --verify` checks the newest backup:
  - each dump file's size and SHA-256 against the manifest;
  - it restores both dumps into throwaway databases (`market_intel_restore_test` in both) and
    compares the counts with the manifest: they must match exactly;
  - it prints today's live counts (app logins) for information only; they may differ once
    data has changed since the backup;
  - it drops the throwaway databases again, also when something fails.

  The restore test uses the Postgres superuser and the MongoDB root user, and only on the
  throwaway databases.
- **MongoDB logins and roles are not in the backup:** they live in the `admin` database, and
  only `market_intel` is dumped. A restore onto empty volumes relies on the init scripts and
  `docker/compose.env` to recreate them.
- **Copy the backups off the laptop regularly** (an external drive or your own cloud
  storage): once ingestion starts, the local databases are the only copy of the data. Never
  copy backups into the repo or into git.
- A real restore after data loss isn't scripted yet: plan it with the planner first.

**Quit Docker when idle.** Stop the databases (`down`) and quit Docker Desktop when you don't
need them: Docker Desktop holds its memory (up to the WSL cap) even with no container running.
Containers that were still running when Docker Desktop quit start again with it
(`restart: unless-stopped`).

## Secrets

| Secret | Lives in | How to change it |
|---|---|---|
| Postgres owner password (in `POSTGRES_ADMIN_URL`) | `.env` | `ALTER ROLE <owner role> PASSWORD '...'` in the provider's SQL editor |
| Postgres `mi_app` password (in `POSTGRES_URL`) | `.env` | `ALTER ROLE mi_app PASSWORD '...'` in the SQL editor |
| MongoDB setup and app passwords (in `MONGODB_ADMIN_URI`, `MONGODB_URI`) | `.env` | The provider console's "Edit password" for that database user |
| Gmail app password (`ALERT_EMAIL_APP_PASSWORD`) | `.env` | Delete it in the Google account and create a new one |
| Docker database passwords | `docker/compose.env` (main laptop) | Change it in the database too (see [Local Docker databases](#local-docker-databases)), then in `.env` |
| GitHub CLI login | The CLI's own credential store | `gh auth refresh`, or `gh auth logout` and log in again |
| Colab secrets | Colab's Secrets panel | Edit them in the panel |

After changing a secret, update `.env` on **every** laptop and run health.

**If a secret leaks** (pasted, committed, shown on screen): change it **immediately**, then
update every laptop's `.env`. Bots scan public repos within minutes. If it was committed,
changing the secret matters more than rewriting history.

## Migrations

- Apply only **after CI and compose-smoke are green**, with the owner login:
  `uv run alembic upgrade head`, then `uv run alembic current` (should say `(head)`).
- **Never downgrade real data**: a downgrade drops tables. Downgrades run only in CI.
- New migration: change `src/market_intel/db/schema.py`, then
  `uv run alembic revision --autogenerate -m "..."` against an empty database (or render the
  operations as in 0001), review it by hand, and let CI's `alembic check` confirm there's no
  drift.

## Adding a MongoDB collection

1. Add the model to `src/market_intel/mongo/models.py`, and its validator and indexes to the
   collection list in `src/market_intel/mongo/schemas.py`.
2. Run `uv run python -m market_intel.mongo_setup` (setup login) **before** any code writes to
   it.
3. Add a row for the new collection to the app role `mi_app_rw` in the provider console.
4. Add the same row to the Docker init script `docker/mongo-init/01-app-user.js`.
5. Tests: unit tests for the model and validator, and integration tests for the rights.
6. Docs: [architecture](architecture.md) (collections and rights tables), this runbook if
   needed, and a decision-log entry.

The same order applies to a **new field** on an existing collection: validators reject unknown
fields, so `mongo_setup` must update the validator before code writes the field.

## Logs and ingestion runs

**Where each log is.** Every program writes only to its own file in `logs/` (`LOG_DIR`), one
JSON object per line, times in UTC:

| Program | Log file |
|---|---|
| `market_intel.health` | `logs/health.jsonl` |
| `market_intel.db_check` | `logs/db_check.jsonl` |
| `market_intel.backup` | `logs/backup.jsonl` |
| `market_intel.mongo_setup` | `logs/mongo_setup.jsonl` |
| `market_intel.alerts --test` | `logs/alerts.jsonl` |
| `market_intel.instruments` (refresh, sector import) | `logs/instruments.jsonl` |
| `market_intel.watchlist` | `logs/watchlist.jsonl` (counts only, never symbols) |
| `market_intel.news` (fetch, status) | `logs/news.jsonl` |
| `market_intel.upstox` (login, status) | `logs/upstox.jsonl` (never the token) |
| `market_intel.candles` | `logs/candles.jsonl` |
| `market_intel.features` (build, labels, check-leakage) | `logs/features.jsonl` |
| `market_intel.models` (train, report, ui) | `logs/models.jsonl` |
| Code that names no component | `logs/market_intel.jsonl` |
| Later Phase 1 jobs | `logs/<component>.jsonl`, named in each job's section |

Each file rotates at UTC midnight on its own (older days get a date suffix; 30 are kept), so
two programs running at once never block each other. Alerts that a program queued but
couldn't send before exiting are logged there as not sent.

**What a failed ingestion run looks like.** Every data-loading job writes one row to the
Postgres table `ingestion_runs`: `status` is `running` while it works, then `success` (with
`rows_written`) or `failed`. A failed row keeps the rows counted so far, `finished_at` and an
`error` like `ConnectionError: ... https://***@api.example/...`: the error type and message,
with user names, passwords and secrets masked, cut to 2000 characters. Health's
`data_freshness` check turns WARN when the latest run failed.

To read the latest runs, run this SQL with any client using the **app login**
(`POSTGRES_URL`):

```sql
SELECT id, job, source, status, rows_written, started_at, finished_at, error
FROM ingestion_runs ORDER BY started_at DESC LIMIT 20;
```

On the `full` profile the container's own login works without a password:
`docker compose -f docker/compose.yaml --env-file docker/compose.env exec postgres sh -c
'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"'`, then the query above.

Then open that job's log (see the table above; `instruments_refresh` writes to
`logs/instruments.jsonl`) around `started_at`: its
`ingestion_run_failed` line has the same masked error, and the lines before it show what the
job was doing. A row stuck at `running` with no `finished_at` means the process was killed
before it could record the end (power off, a crash of Python itself); the next run of the job
starts a new row.

## Instruments and watchlist

How it works: [architecture](architecture.md#instruments-and-watchlist). **Alert rule:**
`ALERT_DRY_RUN=true` is only for Claude Code's tests and demos; the owner's real runs use
real alerts, so a refused refresh reaches your inbox.

| Task | Command |
|---|---|
| Daily refresh (after ~06:00 IST, when Upstox refreshes its file) | `uv run python -m market_intel.instruments refresh` |
| See what a refresh would change, save nothing | `uv run python -m market_intel.instruments refresh --dry-run` |
| Refresh from a local file instead of downloading | `uv run python -m market_intel.instruments refresh --file PATH` |
| Monthly sector import, dry run first | `uv run python -m market_intel.instruments import-sectors PATH --dry-run` |
| Sector import | `uv run python -m market_intel.instruments import-sectors PATH` |
| Add a stock to the watchlist | `uv run python -m market_intel.watchlist add SYMBOL [--note TEXT]` |
| Remove a stock | `uv run python -m market_intel.watchlist remove SYMBOL` |
| Show the watchlist | `uv run python -m market_intel.watchlist list` |

**Daily refresh.** It downloads Upstox's public NSE file (about 2.3 MB) only when it has
changed; otherwise it prints "not modified ... nothing to do". The summary shows inserted,
updated, symbol changes, deactivated, reactivated, skipped and malformed objects, and the
counts of EQ, BE and indices. A second run on the same file makes 0 changes. Downloaded files
stay in `data/raw/upstox/instruments/` (git-ignored, newest 7 kept).

**"Instrument refresh refused: file too small".** The safety valve: the file keeps fewer than
`INSTRUMENTS_MIN_KEEP_RATIO` (default 0.8) x the active instruments, so nothing was changed and
the run is marked failed (health's `data_freshness` turns WARN). Usually Upstox published a
broken or partial file: wait and run the refresh again later. If NSE really removed that many
instruments, lower `INSTRUMENTS_MIN_KEEP_RATIO` in `.env` for one run, then empty it again.

**Monthly sector import.** Sectors come from the Nifty 500 list, downloaded **by hand in a
browser** (never scripted):

1. On the NSE Indices website, open the Nifty 500 index page and download its constituent list
   CSV (file name `ind_nifty500list.csv`).
2. Keep it outside the repo, or in `data/` (git-ignored). Never commit it.
3. Dry run: `uv run python -m market_intel.instruments import-sectors PATH --dry-run`. Check the
   matched and unmatched counts.
4. Import: the same command without `--dry-run`.

The headers must be `Company Name, Industry, Symbol, Series, ISIN Code`. If NSE changes the
format the import stops with "unexpected headers" and changes nothing; ask for a code update.
Empty values never overwrite a sector, and stocks outside the Nifty 500 keep no sector.

**Watchlist.** At most 50 active NSE stocks of series EQ or BE; indices are refused. Adding a BE
stock prints a risk note (trade-to-trade, often under exchange surveillance). The list lives
only in the database: never copy it into the repo, the docs or a planner prompt that ends up in
the repo. Choosing stocks is the owner's decision.

## Upstox login and daily candles

How it works: [architecture](architecture.md#upstox-login-and-daily-candles).

| Task | Command |
|---|---|
| **Daily login** (each morning; the token ends at 03:30 IST) | `uv run python -m market_intel.upstox login` |
| Is the token still valid? | `uv run python -m market_intel.upstox status` |
| Daily candles, everything (incremental) | `uv run python -m market_intel.candles daily` |
| History from 2015 for one universe | `uv run python -m market_intel.candles daily --universe nifty500 --from 2015-01-01` |
| One stock or index | `uv run python -m market_intel.candles daily --symbol SYMBOL` |
| See what would load, save nothing | `uv run python -m market_intel.candles daily --symbol SYMBOL --dry-run` |
| Re-match stored exchange items (after new BSE codes) | `uv run python -m market_intel.news rematch-exchange --dry-run`, then without `--dry-run` |

**The daily login.** Run `upstox login` in your own terminal. A browser opens Upstox's login
page; log in there (your password and TOTP stay with Upstox). When the browser shows "Login
received", the terminal prints how long the token is valid. If no browser opens or nothing
happens within 3 minutes, copy the full address from the browser bar (it starts with your
redirect address and contains `?code=`) and paste it when asked. The token is kept only in
Windows Credential Manager (entry `market-intel-upstox`); it never appears on screen or in logs.

**If the token expired** (alert "Upstox token expired", health `upstox_token` WARN, or "no valid
Upstox token" in a command): run `upstox login` again, then rerun the command. Without a token
the candle loader still loads stocks from yfinance (if Yahoo allows it) and skips indices;
those days are asked from Upstox again after the next login, and Upstox rows replace yfinance
rows.

**Reading a candle run.** The summary shows instruments done / already done (resumed) / failed,
rows written, requests per source and the duration, rows by source, gaps (open / filled /
unfillable) and price events. A `partial` run names the failed instruments in
`ingestion_runs.error`. An interrupted backfill continues where it stopped: just run the same
command again.

**Gaps.** `open`: days still missing, retried on the next run (usually filled after the next
login). `unfillable`: every source failed for those days (often a trading suspension); nothing
to do. Special weekend sessions (Diwali Muhurat trading, a Budget-day Saturday) are not gaps
since S2: they are left out of the gap calendar. To list the gaps, with the app login:

```sql
SELECT i.symbol, g.gap_start, g.gap_end, g.status FROM data_gaps g
JOIN instruments i ON i.id = g.instrument_id WHERE g.interval = '1d' AND g.status <> 'filled'
ORDER BY g.gap_start DESC LIMIT 50;
```

**Price events.** `price_events` lists suspected split/bonus days (a sudden 1/2, 1/3, ... price
jump while the market was still). Prices stay as they were; S2 leaves these days out of labels.

## Models

How it works: [architecture](architecture.md#models-evaluation-costs-and-the-registry).

| Task | Command |
|---|---|
| Train (walk-forward, holdout once, final retrain; ~20-40 min) | `uv run python -m market_intel.models train --horizon 21` |
| Show the last report again | `uv run python -m market_intel.models report` |
| Open the MLflow UI (this laptop only) | `uv run python -m market_intel.models ui`, then http://127.0.0.1:5000 |
| The same with plain MLflow | `uv run mlflow ui --backend-store-uri sqlite:///<repo folder>/data/mlflow/mlflow.db --host 127.0.0.1` |

Never start the UI with `--host 0.0.0.0`: it would expose the runs to the network.

**Reading the report.** The walk-forward portfolio table is the honest test: the model's top 20
every 21 trading days, after costs, next to the equal-weight universe, always-up (the same as the
equal-weight universe), momentum, the SMA rule and Nifty 50. "beats the baselines: YES" needs
the model to beat the equal-weight universe AND the best other baseline. The holdout (the last 6
months) is evaluated once and shown separately; never retrain or change settings because of it.
Every training run registers new `candidate` model versions; none becomes `active` without
paper-trading results and the owner's approval. Model cards: `data/models/<name>-v<version>.md`.

## Features and labels

How it works: [architecture](architecture.md#features-labels-and-the-leakage-test).

| Task | Command |
|---|---|
| Features for new days (after the candles) | `uv run python -m market_intel.features build` |
| Rebuild features from a day | `uv run python -m market_intel.features build --from YYYY-MM-DD` |
| Recompute the labels | `uv run python -m market_intel.features labels` |
| Leakage test on the real data | `uv run python -m market_intel.features check-leakage --sample 500` |
| Special sessions and their gap rows | `uv run python -m market_intel.candles special-sessions --dry-run`, then without `--dry-run` |

**Order each day:** candles, then `features build`, then `features labels`. The build summary
shows the rows written and the % of NULL per feature (long windows such as SMA200 and the
52-week features are NULL for a stock's first year). The labels summary shows, per horizon, the
usable rows, % up and the rows excluded by reason (`beyond_data` for the most recent days is
normal: their future isn't known yet).

**If `check-leakage` fails:** don't train or use models. It names the check, the instrument and
day, and the features that changed; the cause is a feature that looks ahead. Fix the feature in
`build_features` (a code task) and run the test again.

## Intraday data, features and labels

How it works: [architecture](architecture.md#intraday-data-features-and-labels).

| Task | Command |
|---|---|
| The nifty50 universe (once, and when the index changes) | `uv run python -m market_intel.instruments import-sectors --file data/ind_nifty50list.csv --universe nifty50` |
| 1-minute candles (new days; needs today's Upstox login) | `uv run python -m market_intel.candles intraday` |
| Plan only, nothing saved | `uv run python -m market_intel.candles intraday --dry-run` |
| One instrument from a date | `uv run python -m market_intel.candles intraday --symbol RELIANCE --from 2024-01-01` |
| Intraday features (new months) | `uv run python -m market_intel.intraday features build` |
| Rebuild features from a month | `uv run python -m market_intel.intraday features build --from YYYY-MM` |
| Triple-barrier labels | `uv run python -m market_intel.intraday labels build` |
| Leakage test on the real data | `uv run python -m market_intel.intraday check-leakage --sample 500` |

**Order:** candles, then `features build`, then `labels build`. The first backfill from 2022
takes about an hour; it can be stopped and run again (complete months are skipped). India
VIX's symbol is `INDIA VIX` for `--symbol`.

**Reading the summaries.** The candles summary shows the rows written, requests, bars dropped
outside 09:15-15:29 and thin days (fewer than 300 bars; often half days or data gaps). The
features summary shows the % NULL per group (the volume group needs 10 previous days, the
30-minute windows aren't ready at 09:30, and relative strength vs the sector is NULL for
stocks without a sector index). The labels summary shows, per side, how often the target or
the stop came first or the time limit ended the trade, and the average gross return (before
costs).

**Disk usage.** 1-minute candles live in Postgres (about 29 million rows from 2022 for ~64
instruments; they make the backups bigger and slower). Features and labels are Parquet under
`data/features/intraday` and `data/labels/intraday` (sizes in the build summaries); deleting
them is safe: a `--from` rebuild recreates them.

**If `check-leakage` or a build's leakage gate fails:** nothing is written. Don't use the
features; the output names the check, the instrument, the time and the features that changed.
Fix the feature in `market_intel.intraday` (a code task) and run the test again.

## Intraday strategies, backtest and replay

How it works: [architecture](architecture.md#intraday-strategies-backtest-and-sizing).

| Task | Command |
|---|---|
| Volatility-scaled labels (after the v1 labels) | `uv run python -m market_intel.intraday labels build --version v2` |
| Rule strategies against the baselines, after costs | `uv run python -m market_intel.intraday backtest --capital 100000` |
| The same on the fixed v1 barriers | `uv run python -m market_intel.intraday backtest --labels v1` |
| Train the meta-model (walk-forward, then the holdout once) | `uv run python -m market_intel.intraday train --capital 100000` |
| The backtests stored so far | `uv run python -m market_intel.intraday report` |
| Replay one past day, trade by trade | `uv run python -m market_intel.intraday replay --date YYYY-MM-DD --capital 100000` |
| Longer-hold labels (30 / 60 bars, hold to close) | `uv run python -m market_intel.intraday labels build --version v3` |
| The leakage test on the longer-hold labels | `uv run python -m market_intel.intraday check-leakage --labels v3 --sample 500` |
| Is the model's confidence meaningful, and do the strongest signals pay? | `uv run python -m market_intel.intraday precision --capital 100000` |

**Order:** candles, `features build`, `labels build`, `labels build --version v2`, then
`backtest` and `train`. `replay` needs the features and labels of the day; with no `--date` it
takes the last built day.

**Reading the report.** `report` ranks every run worst-first in plain percentages, with
a "vs none" column: the gap to doing nothing, which returns 0%. A negative number is
money that no trading would have kept. The replay prints the day's charges and the gross
price move as percentages too, so "the fee was bigger than the move" is visible without
arithmetic.

**Reading `precision`.** The calibration table shows what the model said against what
happened: if decile 10 says 43% and delivers 34%, the model ranks correctly but its
numbers are overconfident, and the expected-value gate will trust them too much. The
top 10/5/1% rows answer the only question that matters for a "few strong signals"
strategy: whether the most confident trades are net positive after the round trip. They
are not, on this data. It retrains the walk-forward, so it takes about twenty minutes.

**Reading the backtest.** The table ranks every rule strategy and the model against
`no_trading`, after all charges and the assumed slippage. Under it is a grid showing the best
strategy's profit at 0, 2, 5 and 10 bps of slippage per side, because that assumption decides
most of the answer: charges alone take about 0.13% of a position per round trip, so at 10 bps
a side a trade must move 0.33% just to break even. `RUINED` marks a run that lost three
quarters of the account and was stopped. **"No trading" winning is a result, not a bug**, and
it is the usual one: SEBI found that 71% of individual intraday traders in the cash segment
lost money in FY2023.

**Reading the training output.** Walk-forward AUC near 0.5 means the model found nothing.
A warning about folds that "predicted a single value" means those months had too few training
rows for LightGBM to split at all (raise `--window-months` or lower `--min-train-rows`), and
their AUC says nothing about the market. The holdout is scored once; never retrain because of
it, and never promote a model: everything stays `candidate`.

**Capital and risk.** Every command takes `--capital`. The defaults risk 1% per trade, stop
for the day after losing 2%, and take at most 5 trades a day. A tight stop cannot use the whole
risk budget, because the position would exceed the capital: with a 0.25% stop the exposure cap
binds and the trade risks about 0.25% of capital. Intraday (MIS) leverage is not modelled, and
no command ever places an order.

## News feeds and source health

How it works: [architecture](architecture.md#news-and-exchange-feeds) and
[source health](architecture.md#source-health).

| Task | Command |
|---|---|
| Fetch every enabled feed (each at most once per 10 minutes) | `uv run python -m market_intel.news fetch` |
| One feed only | `uv run python -m market_intel.news fetch --feed NAME` |
| Ignore the 10-minute interval (never ignores `Retry-After`) | `uv run python -m market_intel.news fetch --force` |
| Fetch and parse, save nothing | `uv run python -m market_intel.news fetch --dry-run` |
| Per-feed status and problems | `uv run python -m market_intel.news status` |
| Per-company news for the watchlist and recent filers (Google News RSS) | `uv run python -m market_intel.news company-fetch` |
| One company only | `uv run python -m market_intel.news company-fetch --symbol SYM` |
| Re-key stored exchange items (once, after 1.2b; dry run first) | `uv run python -m market_intel.news migrate-exchange-keys --dry-run` |

**Reading a fetch.** The first line gives the run status (`success`, `partial` or `failed`)
and the totals; then one line per feed: new and duplicate items (a duplicate keeps the first
stored copy), malformed entries and, for exchange feeds, how many items matched a stock
(matched, ambiguous, unmatched); or `not_modified`, `skipped (interval)`,
`skipped (retry_after)` or `failed: <reason>`. A `partial` run names the failed feeds in
`ingestion_runs.error`; a `failed` run (every attempted feed failed) exits with code 1.

**A source-health alert** ("Source problem: rss:<feed> (<rule>)") means a feed broke or
changed. The email names the rule, the masked last error and the next step.

| Rule | Likely cause | Fix |
|---|---|---|
| `failures` (3 in a row) | The site is down, moved the feed, or now refuses us (403/429) | Open the feed URL in a browser. If it moved, update `url` in `config/news_feeds.toml`. If it refuses automated readers, set `enabled = false`: never change the user agent or work around a block |
| `empty` | The URL now returns a web page or an error page instead of a feed | As above; the publisher may have retired the feed |
| `malformed` | Items lost their link or title | Check the feed in a browser; the parser may need an update (a code task) |
| `volume_drop` | The feed returns far fewer items than usual | Often temporary; if it lasts, check the feed URL and the publisher's RSS page |
| `no_publish_time` | The date format changed | The parser needs the new format (a code task); until then items use the fetch time |
| `silent` (14 days) | The feed returns no items at all for two weeks | Open it in a browser; if it's dead, set `enabled = false` |

A rule stays an active problem (health `sources` WARN, `news status` shows it) until a later
fetch passes it. Alerts repeat at most once per `ALERT_THROTTLE_HOURS` per feed and rule.
After a fix, run `uv run python -m market_intel.news fetch --feed NAME --force` and check
`news status`.

**Per-company news (Google News RSS).** `company-fetch` covers every watchlist stock plus
up to 30 companies that filed with the exchanges in the last 24 hours. **Adding watchlist
stocks improves coverage** (`uv run python -m market_intel.watchlist add SYMBOL`). It asks
Google at most once every 10 seconds and each company at most once an hour. The summary
shows targets (watchlist / filers), requests, new / shared / duplicate items,
high / low confidence and whether Google blocked us. Low confidence means a one-word
name or a name not found in the headline: treat those items with care.

**"google_news blocked" alert.** Google refused automated access (403/429/503, a web
page instead of a feed, or a consent/CAPTCHA redirect). The code already stopped and
pauses all Google requests until the next IST day. **Wait; don't retry, don't use
`--force`** (it can't override the pause anyway) and never change the user agent. If
blocks repeat over several days, tell the planner: the source may need to be dropped.

**BSE codes.** `instruments refresh` also maps BSE scrip codes from Upstox's BSE file;
its summary ends with "BSE codes: N instruments have a bse_code". "BSE codes: FAILED"
means only that part failed (the run is `partial`); run the refresh again later.

The feed list is in the repo; nothing fetched (headlines, state files) is ever committed. The
publishers' RSS terms usually allow personal, non-commercial use with a link back; ours stays
local and is never republished.

## Common problems

- **"uv not found"** after installing uv: fully restart VS Code (or the laptop) so it picks up
  the new PATH, or use `.venv\Scripts\python.exe` directly.
- **The first query after a while is slow:** the cloud database is waking up from sleep.
  Connections retry once after 3 s.
- **VS Code asks to enable `python.terminal.useEnvFile`:** do **not** enable it. It would load
  `.env` into every terminal; the project reads `.env` itself.
- **An unexpected real alert email:** check whether the same alert was throttled before
  (`data/state/alert_throttle.json`) and what ran at that time (`logs/alerts.jsonl` and the
  log of the program that alerted). Tests, demos and CI must
  use `ALERT_DRY_RUN=true`.
- **Colab stops at the Python version check:** pick runtime version 2026.07 (Python 3.12)
  under *Runtime > Change runtime type > Runtime version*; if it's no longer offered, tell the
  planner.
- **A commit is blocked by detect-secrets:** don't add a pragma by reflex. Move fake test
  values into a variable or reword examples; never commit a real secret.
- **A commit is blocked by nbstripout:** it stripped notebook outputs; `git add` the notebook
  again and commit.
- **A commit is blocked by commit-email-guard:** the author or committer email isn't your
  GitHub no-reply address. Set it for this repo (`git config user.email "<your GitHub
  no-reply address>"`, GitHub: Settings > Emails) and commit again. Never bypass the hook.

## Changing the project

- Every change updates the docs it affects (README, [architecture](architecture.md), this
  runbook), and every task report lists the doc changes or says why none were needed.
- Every decision gets an entry in the [decision log](decisions.md), appended at the end; old
  entries are never edited.
- Small conventional commits; hooks always run (no `--no-verify`); no force-push.
