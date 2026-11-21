# Instructions for Claude Code

This repo is built task by task. A planner (a separate Claude chat) writes each task prompt;
you implement exactly ONE task per session and report back. Workflow:
[docs/workflow.md](docs/workflow.md). Project brief: [docs/spec.md](docs/spec.md). Status:
[docs/progress.md](docs/progress.md).

## Start of every session

1. `git pull`.
2. Read [docs/progress.md](docs/progress.md) (current task, carry-forward items).
3. If a task prompt is given, follow it exactly. If none, summarise the status in a few lines
   and wait. Never start the next task on your own.

## Hard rules

- **Secrets:**
  - Never open, print or copy `.env`, `docker/compose.env`, connection strings, tokens or
    passwords. Output may show hosts, booleans, versions and names only.
  - Secrets live only in `.env` (never committed). Never ask the owner for a secret.
- **Alerts:** every command that can send an alert runs with `ALERT_DRY_RUN=true`, unless the
  task explicitly allows a real email.
- **Public repo:**
  - never commit market data, predictions, signals, model files, notebook outputs, results or
    logs
  - no hosts, IP rules, user names or email addresses in docs
  - device-neutral words only (testing laptop / main laptop; profiles `restricted` / `full` /
    `server`); a test enforces this
- **Git:**
  - small conventional commits; hooks always run (never `--no-verify`)
  - no force-push or history rewrite unless the task says so
  - commits use the owner's GitHub no-reply address (per-repo config)
- **Tests and decisions:** never weaken or edit existing tests to make them pass; never edit
  old decision entries (append new ones). One standing exception (planner, 2026-11-22): when a
  task adds a new health check, adding ONLY its entry to tests that list every check by name
  is allowed (append only, no other change); list each such edit in the report.
- **Databases:**
  - App code uses the app logins (`POSTGRES_URL`, `MONGODB_URI`). The owner/setup logins
    (`POSTGRES_ADMIN_URL`, `MONGODB_ADMIN_URI`) are only for alembic, `mongo_setup` and
    backup/restore (`market_intel.backup`).
  - Migrations run by hand, only after CI is green; never downgrade a real database.
  - MongoDB validators reject unknown fields: run `mongo_setup` BEFORE code writes a new
    field. Adding a collection follows the checklist in
    [docs/runbook.md](docs/runbook.md#adding-a-mongodb-collection).
  - Check permissions by trying the action, not by reading the role.
- **STOP points:** when a task says STOP, stop and wait for the owner.
- **Knowledge drift:** check current official docs for library APIs, versions and limits.

## End of every task

- Update the affected docs (README, [docs/architecture.md](docs/architecture.md),
  [docs/runbook.md](docs/runbook.md)) and append the decision entry from the prompt to
  [docs/decisions.md](docs/decisions.md).
- Update [docs/progress.md](docs/progress.md):
  - the task row: done, date, HEAD, "awaiting planner review"
  - the next task
  - the carry-forward items
- REPORT BACK: files changed; tests run + results; docs changed; pragmas added (or "none");
  real emails sent (or "none"); problems; open questions.

## Commands

All run from the repo folder.

| Command | What it does |
|---|---|
| `uv sync` | Installs Python 3.12 and the locked dependencies |
| `uv run pytest` | Runs the tests |
| `uv run pre-commit run --all-files` | Lint, format, secret scan, notebook stripping |
| `ALERT_DRY_RUN=true uv run python -m market_intel.health` | Daily health check, no real emails |
| `ALERT_DRY_RUN=true uv run python -m market_intel.db_check` | Connection, TLS, storage and write test for both databases |
| `uv run python -m market_intel.compute_check` | CPU, RAM, disk and GPU of this machine |
| `uv run alembic upgrade head` | Applies Postgres migrations (owner login), only after CI is green |
| `uv run python -m market_intel.mongo_setup` | Creates or updates MongoDB collections (setup login) |

## Windows note

If `uv` is not found, fully restart VS Code (or use `.venv\Scripts\python.exe`).
