# Progress

Last updated: 2026-11-22, HEAD 8317a95. Updated at the end of every task (see
[CLAUDE.md](../CLAUDE.md)).

## Current

- Phase 1 (stock data pipeline). Now: 1.2b (per-company news, BSE codes, exchange item
  identity, 14-day silence warning) in progress. 0.11 (remove the testing-laptop dependency) stays planned
  for not before 2026-12-03.
- Phase 1 plan (owner-confirmed 2026-11-21, updated 2026-11-22; one session per task): 1.0
  groundwork; 1.1 instruments and watchlist; 1.2 news feeds; 1.2b per-company news (Google
  News RSS for the watchlist, polite rate); 1.3 Upstox login and token; 1.4 daily candles
  with gaps and backfill; 1.5 intraday candles; 1.6 macro; 1.6b official NSE daily files
  (bulk/block deals, bhavcopy delivery %, ASM/GSM lists; once a day, honest identity, stop
  and alert if blocked); 1.7 live feed (also records order-book depth snapshots for the
  watchlist); 1.8 scheduler; 1.9 read-only MCP access; 1.10 Phase 1 wrap-up. Tasks 1.0 to
  1.2b run while the owner's Upstox account is being approved.
- Phase 2 adds (owner, 2026-11-22): a local LLM via Ollama (first choice); filing PDFs with
  OCR; page capture features 1 and 2; the expert-feature catalogue
  ([factor map](factors.md)). The planned 1.4 item "partial run status" was done in 1.2
  (owner-approved).
- Owner decisions for Phase 1 (2026-11-21):
  - The daily Upstox token comes from a manual browser login (one command); the token is kept
    only on this laptop; passwords and TOTP secrets are never stored.
  - A Python scheduler in the repo, started by Windows at logon, catches up after downtime.
  - The Upstox Basic plan is enough.
  - The owner's Upstox mobile app is signed in, so the approve-on-phone token option can be
    evaluated in or after 1.3.
- Upstox facts checked by the planner (official docs, 2026-11-21): APIs free; standard limits
  per API per user 50 per second, 500 per minute, 2000 per 30 minutes; normal websocket
  limits 2 connections, full mode 2000 instruments; minute and hour candles from Jan 2022 (1
  to 15 minute intervals: at most 1 month per request); daily from Jan 2000 (1 decade per
  request).
- 1.2 review (planner, 2026-11-22): passed. Verified in the databases: timing fields
  consistent (0 items with `available_at` before `published_at` or `fetched_at`); all 17
  exchange matches correct (the 3 unmatched look like SME or suspended companies); only the
  approved test line added and the standing rule is in CLAUDE.md; decisions untouched; no
  tracked data; no browser imitation. Answers: (1) the narrower "empty" rule b is OK; add rule
  f (an enabled source with 0 items in every content fetch for 14 days -> WARN); (2) repeated
  links in exchange feeds are different events: store them separately (1.2b part A); (3)
  exchange times without a zone are IST: confirmed.
- 1.1 review (planner, 2026-11-22): passed. Verified as mi_app: counts by series, ISINs
  present, tick sizes 0.01 to 5.00, watchlist 0 rows, run history (one real refresh, one
  refused safety-valve run, two no-op runs), health OK; only the approved 4 masking strings
  changed in old tests; decisions untouched; no data files tracked. Open questions: (1) the
  owner checks the Nifty 500 CSV headers when downloading it (the import has `--dry-run`);
  (2) the dry-run alert rule is only for Claude Code's tests and demos; the owner's real runs
  use real alerts (runbook fixed in 1.2).
- 1.0 review (planner, 2026-11-22): passed. Verified CI, tests, health, unchanged old tests
  and decisions, privacy; read the alerts, file_lock, rate_limit and ingestion code. Open
  questions: (1) log masking hides URL user names too: done in 1.1 part A; (2) a "partial" run
  status: in 1.4.
- 0.10 review (planner, 2026-11-21): passed. Verified: tag `phase-0` on b661d98, local and
  remote; CI green at b661d98; compose-smoke green at 7c7f966; health overall OK with
  `backup_age` OK; the review is honest and specific; privacy clean; old decision entries
  untouched; no-reply emails. Its open question is closed: no clickable CI link in the review
  (it would expose the account name); the run IDs are enough.
- Phase 0 (setup) is complete; tag `phase-0`. Review:
  [docs/reviews/phase-0.md](reviews/phase-0.md).
- 0.9b review (planner, 2026-11-21): passed. All commits pushed; CI green at 9e19a1e;
  compose-smoke green at 7a00494; the CLAUDE.md admin-login rule updated (owner-approved);
  health overall OK on the full profile against local Docker; backups outside the repo, with
  manifests; the email guard blocked a test email; every commit uses the no-reply address.
  Accepted notes: the 0.9b decision entry was amended within its own task, before review;
  mongosh reads its password from the container environment; the first backup has no
  manifest and rotation will remove it; the restore test compared mostly zero counts (its
  carry-forward stays).
- Owner decisions (2026-11-21):
  1. The cloud databases are deleted in 0.11, after the testing laptop is handed back. Copies
     away from the main laptop come from copying `BACKUP_DIR` to external storage.
  2. A backup-age health check now (done in 0.10: `backup_age`, `BACKUP_MAX_AGE_DAYS`).
  3. The owner opens the Upstox account during 0.10 (owner-side: no code, no keys).
  4. AI tools query project data only through an MCP connection, free options only (an
     existing free MCP server or one built in this repo), with a read-only login. The
     concrete server is chosen in a later task.
- 0.9a review (planner, 2026-11-20): passed. Its one miss, the README's stale "Next:" line, was
  fixed in 0.9b.
- Planner decisions during 0.9b (owner-approved defaults, 2026-11-21):
  - One implementer: a second session got the prompt by mistake, stopped at A0 and changed
    nothing. The first `up -d --wait` of this task was A1, on empty volumes.
  - `backup` writes a manifest (counts right before each dump, tool versions, each file's
    size and SHA-256). `--verify` compares the restored counts with the manifest and checks
    the SHA-256 values; live counts are shown for information only.
  - Logins: Postgres superuser inside the container and the MongoDB setup user for backups;
    the Postgres superuser and MongoDB root only on the throwaway databases for the restore
    test; MongoDB passwords only via a temporary `--config` file.
  - The `server` profile is refused like `restricted` for now.
  - The guard script stays in `scripts/commit_email_guard.py`.
  - The `CLAUDE.md` Databases rule now says the owner/setup logins are only for alembic,
    `mongo_setup` and backup/restore (`market_intel.backup`); approved by the owner
    (2026-11-21), done in 3961a2b.
- Open questions resolved by the owner (2026-11-20):
  - Phase 4–8 names: resolved in 0.9a.
  - A `mongo_rights_probe` mismatch stays FAIL; an unexpected error stays WARN.
  - `mongo_setup` does not read the app role (health's behaviour check covers it).
  - The old "not my own device" wording stays.
  - Colab's Python 3.13 stays a carry-forward item for the start of Phase 3.
  - Capability numbers (RAM, GPU memory, memory caps) may appear in public docs (decision
    2026-11-20).

## Tasks

| Task | Status | Review | Summary |
|---|---|---|---|
| 0.1 | done | passed | Repo skeleton, uv, pre-commit, detect-secrets, CI |
| 0.2 | done | passed | Config: profiles, groups, rules R1–R3 |
| 0.3 | done | passed | JSON logging with masking; email alerts with throttle and dry-run |
| 0.4 | done | passed | Cloud Postgres + MongoDB, db_check, Docker Compose, compose-smoke CI |
| 0.5a | done | passed | Public-repo safeguards, disclaimer, TLS refusal |
| 0.5b | done | passed | Postgres schema v1, Alembic, limited app login |
| 0.6 | done | passed | MongoDB collections, validators, append-only audits, migration 0003 |
| 0.7 | done | passed | Health check, compute check, Colab template |
| 0.8 | done | passed | Docs restructure (phase titles finished in 0.9a) |
| 0.8b | done | verified (CI green, tests pass on the main laptop) | Device-neutral names |
| main laptop setup | done 2026-11-20 | verified (health OK) | Tools, clone, `.env`, health check |
| 0.9a | done 2026-11-20 (HEAD 1dfb7ce) | passed (planner, 2026-11-20) | Shared tracking files; phase titles; main laptop setup in the runbook |
| 0.9b | done 2026-11-21 (HEAD 3961a2b) | passed (planner, 2026-11-21) | Local Docker databases on the main laptop (migrated, set up, backup with manifest and restore test); commit-email guard (hook + CI history test) |
| 0.10 | done 2026-11-21 (HEAD 4eb9574) | passed (planner, 2026-11-21) | Phase 0 wrap-up: `backup_age` health check and `BACKUP_MAX_AGE_DAYS`; Phase 0 review; spec (cloud databases, AI access through MCP); tag `phase-0` |
| 0.11 | planned (not before 2026-12-03) | – | Remove testing-laptop dependency (hand-back checklist, cloud databases per the 0.10 decision, two-laptop docs) |
| 1.0 | done 2026-11-21 (HEAD 3938cd5) | passed (planner, 2026-11-22) | Groundwork: one log file per component; background alerts with a bounded exit flush and a locked throttle; `market_intel.sources` interface, typed records and a fake source; multi-window rate limiter with Upstox defaults and 429 back-off; `ingestion_run` helper |
| 1.1 | done 2026-11-22 (HEAD c31dfe5) | passed (planner, 2026-11-22) | Instruments from Upstox's public NSE file (EQ, BE, indices) with conditional GET, size caps, safety valve and symbol history; sector import from the Nifty 500 list CSV; watchlist (max 50, database only); migration 0004; strict URL masking in logs; cloud MongoDB access decision logged |
| 1.2 | done 2026-11-22 (HEAD 8317a95) | passed (planner, 2026-11-22) | 11 RSS feeds (6 news, 5 NSE/BSE) into `news_items` with `available_at`, dedupe and clear-match symbols; reusable source-health alerts and the `sources` health check; `partial` run status; factor map; spec, plan and five decisions |
| 1.2b | in progress | – | Per-company news (Google News RSS), BSE codes, exchange item identity, 14-day silence warning |
| 1.3 | planned | – | Upstox login and token |
| 1.4 | planned | – | Daily candles with gap detection and backfill |
| 1.5 | planned | – | Intraday candles |
| 1.6 | planned | – | Macro |
| 1.6b | planned | – | Official NSE daily files (bulk/block deals, bhavcopy delivery %, ASM/GSM lists) |
| 1.7 | planned | – | Live websocket feed (plus order-book depth snapshots for the watchlist) |
| 1.8 | planned | – | Scheduler |
| 1.9 | planned | – | Read-only MCP access |
| 1.10 | planned | – | Phase 1 wrap-up |

## State

- Main laptop (`full`): local Docker databases at migration 0004 (applied 2026-11-22 after CI
  and compose-smoke were green), MongoDB collections and validators set up; `db_check` OK;
  health overall OK (10 OK, 3 SKIP; `data_freshness` OK with the instruments refresh;
  `mongo_rights_probe` OK, refusal code 13; `backup_age` OK). Backup with manifest and restore test OK (restored counts match the manifest, SHA-256
  values match); backups in `BACKUP_DIR`, outside the repo. The very first backup there was
  taken before manifests existed; it has no manifest and leaves with the 7-per-kind rotation.
- Testing laptop (`restricted`): unchanged, on the cloud databases (migration 0003, MongoDB
  collections set up).
- News (1.2): local `news_items` holds 227 items (207 news, 20 exchange; 17 exchange items
  matched a stock, 3 unmatched; every `published_at` from the feed). First real fetch
  2026-11-22: run `success`, `rows_written` 227; the immediate second run skipped all 11
  feeds by interval. MongoDB validator and indexes updated with `mongo_setup` (second run:
  nothing to change). Health overall OK (11 OK, 3 SKIP; `sources` OK). Backup and
  `backup --verify` OK with `news_items` 227.
- Instruments (1.1): first real refresh 2026-11-22 kept 3102 (EQ 2779, BE 184, indices 139),
  0 malformed; a second run was "not modified"; a run from the saved file made 0 changes.
  No sectors yet (the owner's sector import is pending). Watchlist empty (the demo removed
  both rows). Backup and `backup --verify` after the refresh: restored counts match the
  manifest (`instruments` 3102), SHA-256 values match.
- Phase 1 groundwork (1.0) in place: logs in `logs/<component>.jsonl`;
  `queue_alert` sends in the background; `market_intel.sources`, `market_intel.rate_limit` and
  `market_intel.ingestion` exist. Integration tests pass through the app login on the local
  databases and leave no rows (the instrument tests run in a transaction that is rolled back).
- Commit-email guard active: the pre-commit hook blocks other emails; the CI test passes on
  the full history.
- CI (run 44945548776) and compose-smoke (run 44945548733) green at 131a510, the last 1.2
  code commit (37 integration tests); CI green at 8317a95 (run 44945788144). The final
  commit's CI result is in the 1.2 report.

## Carry-forward items

- 1.3: the owner's Upstox mobile app is signed in; evaluate the approve-on-phone token option.
- First Upstox backfill takes hours (2000 requests per 30 minutes); it must resume safely.
- Phase 2: decide when the `llm` settings group counts as complete (lean: Gemini OR Groq).
- Phase 3 start: Colab's newest runtime uses Python 3.13 while the project pins 3.12; decide
  whether to allow both.
- MongoDB: new fields need `mongo_setup` first; each new collection needs an app-role row in
  the provider console. Refusal codes: 8000 (cloud) and 13 (Docker).
- MongoDB on Docker: the init script creates the app role only on an empty volume, so a new
  collection also needs its role row added in the running local database. The runbook's
  "Adding a MongoDB collection" checklist doesn't say how yet; settle it with the first new
  collection.
- The cloud MongoDB accepts any address (decision 2026-11-20): passwords stay only in `.env`
  and are changed at once if exposed; the cloud databases are deleted in 0.11.
- Testing laptop: the next time it is used, after `git pull`: `uv run alembic upgrade head`
  (0004) and `uv run python -m market_intel.mongo_setup` (the 1.2 news_items validator and
  indexes), before running the instruments refresh or the news fetch there.
- 1.7: order-book depth recording starts there (no history exists before it).
- The publishers' RSS terms (ET, Mint, Business Standard, NSE, BSE): the owner may check them
  in a browser; ours is personal, local and never republished.
- Owner: download the Nifty 500 list CSV by hand and run `instruments import-sectors`
  (dry run first); see the runbook (Instruments and watchlist).
- Before the testing laptop is handed back: log out of GitHub CLI, revoke its token, delete
  its `.env`.
- Main laptop: quit Docker Desktop when not using local databases (it holds 6 GB).
- Main laptop: back up after work days and copy `BACKUP_DIR` to external storage weekly; once
  ingestion starts and the cloud databases are deleted (0.11), these are the only copies.
- Before AI first queries project data (Phase 2 at the latest): plan the MCP connection
  task (free, read-only login, local only).
