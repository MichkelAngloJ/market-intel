# Phase 0 review — Setup

Written 2026-11-21 at the end of task 0.10, against the spec's
[Definition of done](../spec.md#definition-of-done-per-phase). Phase 0 was built in 14 tasks
(0.1 to 0.10 plus the main laptop setup, see [progress](../progress.md#tasks)), each reviewed
or verified by the planner before the next one started.

## Definition of done

| Item | State |
|---|---|
| Code and tests pass | Yes. Locally on the main laptop: 376 passed, 24 skipped (the integration tests, which need the Docker databases). In CI: 375 passed, 25 skipped (one Windows-only test is skipped on Linux); compose-smoke runs the 24 integration tests against real Docker databases: 24 passed, and health shows no FAIL. Last code commit: `7c7f966`, CI run `44801440413` and compose-smoke run `44801440149` (the repo's Actions tab), both green. Pre-commit: all hooks Passed or Skipped |
| README and docs updated | Yes: [README](../../README.md), [architecture](../architecture.md), [runbook](../runbook.md), [spec](../spec.md), [workflow](../workflow.md), [progress](../progress.md). A docs test checks links, headings and length, and keeps hosts, emails and device names out |
| decisions.md updated | Yes: every task appended its entry; old entries are never edited (the 2026-11-18 redactions are marked) |
| Honest review | This page |

## What works

- **Configuration:** typed settings from `.env`, three profiles (`restricted`, `full`,
  `server`) and settings groups that are only required where they are used. Rules R1–R3 (no
  broker keys on `restricted`, the LLM spend cap, the warning level) and "backups outside the
  repo" are enforced when settings load, not by convention.
- **Logging and alerts:** JSON logs with masking of secrets; email alerts with a throttle
  and a dry-run mode, so tests and checks never send a real email by accident.
- **Databases with limited logins:** Postgres (schema v1, three Alembic migrations) and
  MongoDB (validated collections, an append-only audit collection). App code uses limited app
  logins; the owner and setup logins are only for migrations, `mongo_setup` and backups. The
  app login's MongoDB rights are tested by behaviour, both in compose-smoke and in every
  health run.
- **Health check:** 13 checks with OK / WARN / FAIL / SKIP, one throttled alert on FAIL, a
  JSON output for the future Data Health page. On the main laptop it is overall OK (9 OK,
  4 SKIP). Since 0.10 it also warns when the newest complete backup is older than
  `BACKUP_MAX_AGE_DAYS` (default 7).
- **Local databases and backups:** the main laptop runs Postgres 18 and MongoDB 8.0 in Docker
  (ports on 127.0.0.1, 2 GB memory cap per container). `market_intel.backup` writes dumps
  plus a manifest with counts and SHA-256 values, keeps 7 of each kind, and `--verify`
  restores into throwaway databases and compares the counts.
- **Public-repo safety:** detect-secrets, nbstripout, `.gitignore` for data folders, a repo
  safety test, a disclaimer, device-neutral names, and a commit-email guard (a pre-commit
  hook plus a CI test over the whole history).
- **CI:** lint, format, secret scan and tests on every push; compose-smoke runs the
  integration tests and health against real Docker databases when database code changes.
- **Way of working:** one task per session, a planner review after each task, and the repo
  as the shared memory between two laptops (CLAUDE.md, spec, workflow, progress). Reviews
  caught real problems (stale status lines, a missing manifest, login rules that needed to
  be written down).

## What doesn't (yet)

- **The restore test ran on empty databases.** The local databases hold only
  `alembic_version`, so `backup --verify` compared mostly zero counts. It proves the
  mechanics, not that a full database restores correctly. Re-run it after the first Phase 1
  ingestion (carry-forward).
- **Windows log-rotation lock.** The log file rotates at midnight; on Windows a second
  process holding the same file open blocks the rename, so the rotation fails. Phase 0 runs one process at a time, so it
  hasn't bitten, but Phase 1 will run several. Fix before then: one log file per component,
  and alerts sent in the background (carry-forward).
- **MongoDB app-role rows for new collections on Docker.** The init script creates the app
  role only on an empty volume. A new collection also needs its role row added in the
  running local database, and the runbook's checklist doesn't say how yet. Settle it with
  the first new collection (carry-forward).
- **A real restore after data loss isn't scripted.** Only the restore test is.
- **No data yet.** `data_freshness` and `upstox_token` are SKIP; nothing in Phase 0 touches
  market data, so the checks around it are untested against real runs.
- **Copies away from the laptop are manual.** Backups land next to the repo on the same
  laptop; copying them to external storage is a routine step, not automated.

## Risks

- **Single copy of the data.** Once ingestion starts and the cloud databases are deleted
  (0.11), the main laptop's Docker volumes are the only live copy. `docker compose down -v`,
  a disk failure or a lost laptop would lose everything since the last external copy.
  Health's `backup_age` warns about old backups, but not about missing external copies.
- **Laptop resources.** The main laptop has 15 GB RAM, Docker holds up to its 6 GB WSL cap
  even when idle, and the GPU has 4 GB. Ingestion, databases and training will compete; heavy
  training goes to Colab.
- **Upstox dependency.** Phase 1 depends on an Upstox account and its daily token login. The
  account isn't open yet; yfinance is the backup for daily data only.
- **Public repo.** One wrong commit can leak data or personal details. The hooks and tests
  catch the known patterns (secrets, emails, hosts, device names, notebook outputs), not
  everything; reviews still matter.
- **AI access to data.** Later phases let AI tools query project data. Without a controlled
  path (decision 2026-11-21: MCP only, read-only login, local only) that would be a new way
  to leak or change data.
- **Process overhead.** Every task goes through prompt, implementation, report and review.
  That caught real problems, but Phase 0 took 14 tasks for setup alone; Phase 1 needs larger,
  still reviewable tasks.

## What to do differently

- **Test with real data earlier.** Several Phase 0 guarantees (backups, freshness, rights)
  were proven on empty databases. In Phase 1, re-run the restore test and the health check as
  soon as the first real data lands.
- **Name things neutrally from the start.** Device-based profile names had to be removed
  after the repo went public (0.8b), and the old names stay in the git history.
- **Write login rules down when the code needs them,** not after: the admin-login rule in
  CLAUDE.md was updated only during the 0.9b review.
- **Keep the status lines in README and progress in the same commit as the task's last
  change,** so a stale "Next:" line can't slip through again (0.9a).
- **Make the backup routine part of the daily routine,** with the external copy on a fixed
  day, before the cloud databases go away.
