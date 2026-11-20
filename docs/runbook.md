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
5. Work.
6. `uv run pytest` and `uv run pre-commit run --all-files`.
7. `git push`, then check that CI is green.
8. **On the `full` profile, after a work day:** back up
   (`uv run python -m market_intel.backup`; see [Local Docker databases](#local-docker-databases)).
9. **On the `full` profile:** when you're done with the databases, stop them and quit Docker
   Desktop (see [Local Docker databases](#local-docker-databases)).
10. **Weekly:** copy `BACKUP_DIR` to external storage (an external drive or your own cloud
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
| Last 20 log lines (PowerShell) | `Get-Content logs/market_intel.jsonl -Tail 20` |
| Only errors (PowerShell) | `Get-Content logs/market_intel.jsonl \| ConvertFrom-Json \| Where-Object level -eq "error"` |

## Reading the health output

OK and SKIP need nothing. WARN needs attention soon. FAIL sends one alert email (throttled) and
needs a fix now.

| Check and status | Likely cause | Fix |
|---|---|---|
| `config` FAIL | `POSTGRES_URL` or `MONGODB_URI` missing in `.env` | Add them (see [Setting up a new Windows laptop](#setting-up-a-new-windows-laptop)) |
| `config` WARN | Alert settings missing | Add the `ALERT_*` settings, or accept that no emails are sent |
| `config_upstox` / `config_llm` WARN | Only some of the group's settings are set | Set all of them, or none |
| `databases` FAIL, both unreachable on the `full` profile | Docker Desktop or the local databases aren't running | Start them (see [Local Docker databases](#local-docker-databases)) and run health again |
| `databases` FAIL, MongoDB unreachable | The cloud database's IP access list no longer includes this network (new network or changed IP) | Add the current IP in the provider console |
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
6. Allow this laptop's network in the MongoDB access list (provider console).
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
7. **Allow this laptop's network** in the MongoDB access list (provider console).
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
| Restore test of the newest backup | `uv run python -m market_intel.backup --verify` |

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

## Common problems

- **"uv not found"** after installing uv: fully restart VS Code (or the laptop) so it picks up
  the new PATH, or use `.venv\Scripts\python.exe` directly.
- **The first query after a while is slow:** the cloud database is waking up from sleep.
  Connections retry once after 3 s.
- **VS Code asks to enable `python.terminal.useEnvFile`:** do **not** enable it. It would load
  `.env` into every terminal; the project reads `.env` itself.
- **An unexpected real alert email:** check whether the same alert was throttled before
  (`data/state/alert_throttle.json`) and what ran at that time (logs). Tests, demos and CI must
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
