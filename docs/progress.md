# Progress

Last updated: 2026-11-25, HEAD 55bedc8. Updated at the end of every task (see
[CLAUDE.md](../CLAUDE.md)).

## Current

- Sprint (owner-confirmed 2026-11-24): an end-to-end thin slice by 27 Nov (demo 28 Nov):
  S1 Upstox login + daily candles; S2 features + labels + leakage test; S3 baselines +
  per-sector LightGBM (pooled fallback), walk-forward after costs, SHAP, MLflow; S4
  predictions + audit + outcomes + paper trading (with a historical simulation); S5 FastAPI +
  Streamlit (Today's signals, Stock detail, Paper trading, Audit, Data health); S6 scheduler
  + end-to-end CI test + docs + tag e2e-v0. Deferred after 28 Nov: 1.5 intraday, 1.6 macro
  extras, 1.6b NSE files, 1.7 live feed + depth, 1.9 MCP, Phase 2 AI extraction
  (Ollama/FinBERT/OCR/page capture), LSTM, online learning, manipulation flags, IPO, mutual
  funds, agent.
- Owner decision 2026-11-25: new priority order: 1 intraday, 2 mutual funds, 3 long-term
  stocks, 4 IPO. Revised sprint: S4i intraday data + features + labels; S5i intraday
  strategies + ML meta-model + backtest + capital-based sizing + replay demo; S6 mutual funds
  (AMFI NAV, ranking, "how sure" history stats, split calculator with minimums, keep/exit
  flags); S7 dashboard + scheduler + end-to-end test + daily stock predictions/audit/paper
  trading. Every module takes the owner's capital amount (a shared sizing component, S5i).
- Now: S4i (intraday data, features, triple-barrier labels, leakage test) done, awaiting
  planner review. Next: S5i (intraday strategies + ML meta-model + backtest + capital-based
  sizing + replay demo). 0.11 (remove the testing-laptop dependency) stays planned for not
  before 2026-12-03.
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
- S3 review (planner, 2026-11-25): passed as engineering. Verified: DB 0008; 18 candidate (v2)
  + 18 retired (v1) models, none active; 0 old test lines changed; no model or MLflow files
  tracked. Result: the models do NOT beat the baselines after costs (top-20 11.2%/yr vs
  equal-weight 17.8%; holdout worse); 6 of 17 sector models beat their own sector baselines.
  Notes: (a) the SHAP top reasons lean on history_days and month, likely spurious: drop both
  from stock-model inputs when long-term stocks return (priority 3); (b) zeroing event/extreme
  windows is a stopgap: proper corporate actions (NSE official files in 1.6b, yfinance split
  history as a cross-check) and recomputed labels come later; (c) daily-stock paper trading
  happens in S7, labelled "candidate - does not beat baselines".
- S2 review (planner, 2026-11-24): passed. Verified: DB 0007; 5 random 21-day labels
  recomputed from raw candles all match; the leakage test catches all 6 leaky fakes and
  passes on 500 real samples; 0 old test lines changed. Answers: (1) include "always up"
  explicitly; the real bar: the model's top picks must beat an equal-weight hold of the whole
  universe after costs; (2) stocks with fewer than 252 history days are excluded from
  training; they may get predictions later, flagged "thin data".
- S1 review (planner, 2026-11-24): passed. Verified: DB 0006; 1,389,440 candles 2015-01-01 to
  2026-11-23 (upstox 1,388,415, yfinance 1,025, all raw); 0 OHLC violations; 632 instruments;
  nifty500 = 497 with sectors; 1840 unfillable gaps; 156 price events in 92 stocks; no tokens
  tracked; 0 old test lines changed. Answers: (1) remove the 4 BHARATBOND indices from the
  indices universe; keep the bond indices' valid rows, but no model or feature uses bond
  indices; (2) a special session is a day on which fewer than 50% of the nifty500 members with
  history have a candle (Muhurat, Budget Saturday): excluded from the gap and label calendar,
  and its gap rows deleted; (3) turn yfinance's curl-cffi off as a second guard if supported,
  with a test that yfinance uses our own session. All three applied in S2 step 0.
- 1.2b review (planner, 2026-11-22): passed. Verified: DB at 0005, 2755 `bse_code`s, news
  207/20/84, only the approved enum line edited. Answers: (1) keep the strict clean-name rule
  (Phase 2 checks low matches); (2) re-match old exchange items by BSE code (S1 part E).
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
| 1.2b | done 2026-11-22 (HEAD 7d7e9ac) | passed (planner, 2026-11-22) | Per-company news from Google News RSS (watchlist + recent filers; polite rate; block pause until the next IST day; match confidence); BSE scrip codes on instruments (migration 0005) and BSE matching by code; exchange items keyed by their own identity (migrated once); source-health rule f |
| S1 | done 2026-11-24 (HEAD 3e1262f) | passed (planner, 2026-11-24) | Upstox browser login (token in Windows Credential Manager), status and health check; V3 daily candles with an honest-session yfinance backup; loader with precedence, gaps, resume and split/bonus flags; migration 0006 (universe members, price events); nifty500 universe; exchange rematch |
| S2 | done 2026-11-24 (HEAD e91fb33) | passed (planner, 2026-11-24) | Timing convention; features v1 (32, news not for training) and separate forward labels (21/63/126 days, exclusion reasons) with migration 0007; leakage test (truncation, late news, label names, shifted data) in CI and on real data; special sessions out of the gap and label calendar; BHARATBOND out of indices; yfinance curl-cffi off |
| S3 | done 2026-11-25 (HEAD c472d0a) | passed as engineering (planner, 2026-11-25) | Baselines + per-sector LightGBM (pooled fallback), walk-forward after costs, SHAP, MLflow, model registry (migration 0008); the models do NOT beat the baselines |
| S4i | done 2026-11-25 (HEAD 55bedc8) | awaiting planner review | 1-minute candles for the nifty50 universe, the Nifty 50, India VIX and 12 sector indices from 2022 (resumable, one month per request, thin days); the intraday timing convention; features v1 (41) and long/short triple-barrier labels as monthly Parquet with build records (migration 0009); the intraday leakage test (5 checks, 4 leaky fakes) in CI and on real data |
| 1.3 | planned (now part of S1) | – | Upstox login and token |
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
- S4i (2026-11-25): local databases at 0009. Nifty 50 import: 50 matched, 0 unmatched.
  1-minute candles 2022-01-01 to 2026-11-24 for 64 instruments (50 stocks, Nifty 50, India
  VIX, 12 sector indices): 28,349,007 rows, 3,472 requests, 63 min, 11,656 bars dropped
  outside 09:15-15:29, 5 invalid OHLC, 253 thin days; an incremental rerun wrote 0 rows.
  Features v1: 4,015,772 rows in 59 monthly Parquet files, 640 MB, 15 min (% NULL by group:
  returns 0.8, vwap 0.0, opening range 2.3, volume 0.8, volatility 1.5, relative strength
  6.3, market 0.0, previous day 0.0, time and flags 0.0, rule signals 0.0). Labels:
  8,031,544 rows, 63.1 MB, 11 min; long target-first 5.5% / stop-first 24.7% / time-limit
  69.8%, average gross return -0.003%; short 5.3% / 23.5% / 71.2%, +0.002%; excluded
  thin_day 2,850, price_event 402, no_entry 55 (per side). check-leakage --sample 500 on
  real data: PASS (242 s). Health overall OK; backup with restore test OK (1 min 48 s, 686.9 MB; restore test 2 min 30 s).
  Three sectors (Telecommunication, Capital Goods, Consumer Services) have no NSE sector
  index, so relative strength vs the sector is NULL for their stocks.
- S3 (2026-11-25): local databases at 0008. Real training (7.5 min): 17 sector models +
  1 pooled (Diversified, Media, Textiles); 11 thin stocks excluded; folds 2018-2026, holdout
  from 2026-04-24. Walk-forward AUC 0.526 (baselines 0.500-0.501). Walk-forward portfolio
  after costs, annual: model 11.2%, equal-weight universe 17.8%, SMA rule 17.4%, momentum
  15.5%, Nifty 50 9.4%: the model does NOT beat the baselines. Holdout: model -32.1%,
  equal-weight -12.6%, SMA +17.9%, momentum +11.0%, Nifty -10.0%: NO. 6 of 17 sector
  models beat their own sector baselines on the walk-forward. The first run (v1) used a
  ratio correction for split/bonus windows that produced impossible returns; its 18 rows are
  `retired`; the corrected v2 rows are the 18 `candidate`s. Unmeasurable candidate
  stock-periods: 100 of 37,811 (0.3%). MLflow experiment `market-intel-21d`: 38 runs.
- S2 (2026-11-24): local databases at 0007. Step 0: 2 special sessions (2015-02-28,
  2016-10-30); 618 gap rows on them deleted (a second run: 0); the `indices` universe is 135
  (BHARATBOND out). Features v1 for 497 stocks: 1,150,515 rows in 176 s (11 stocks under 252
  days). Labels: 3,451,545 rows in 227 s; usable 21d 1,123,841 (53.0% up), 63d 1,078,648
  (56.6% up), 126d 1,020,422 (59.3% up). `check-leakage --sample 500` on real data: PASS
  (206 s). Health overall OK (13 OK, 1 SKIP).
- S1 (2026-11-24): local databases at 0006. Sector import (real): matched 497, unmatched 4;
  universe `nifty500` = 497 members. Upstox login done by the owner; token valid until
  03:30 IST. Index backfill from 2015: 135 indices loaded, 4 BHARATBOND indices not served
  by Upstox (UDAPI1021), 238,916 rows, 274 Upstox requests, 59 s. Nifty 500 backfill: 498
  instruments, 1,150,524 rows (Upstox 1,149,499; yfinance 1,025 from 368 gap retries), 995
  Upstox requests, 778 s. Incremental rerun: 1 request, 0 rows. Data 2015-01-01 to
  2026-11-23: `candles_daily` 1,389,440 rows; 1840 unfillable gaps (1091 in bond indices;
  617 of 749 stock gaps on two special weekend sessions); 156 suspected split/bonus events
  in 92 stocks. Rematch: 0 changed (17 matched, 3 unmatched). Health overall OK (13 OK, 1
  SKIP; `upstox_token` OK; `sources` 14 sources OK). Backup and `backup --verify` OK.
- 1.2b (2026-11-22): local databases at migration 0005; `mongo_setup` applied (second run:
  nothing to change). `migrate-exchange-keys`: 20 updated, 0 conflicts; a second run 20
  unchanged. 2755 of 2963 active NSE stocks have a `bse_code`. First company-fetch: 11
  targets (watchlist 0, filers 11), 11 requests, 84 new items (high 27, low 57), not
  blocked; run `success`, `rows_written` 84. `news_items`: 207 news, 20 exchange, 84
  company news. Health overall OK (11 OK, 3 SKIP; `sources` OK, 12 sources). Backup and
  `backup --verify` OK (`news_items` 311).
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
- CI (run 45494974004) and compose-smoke (run 45494973966) green at c472d0a, the last S3
  code commit (0008 up, down and up; no drift; 49 integration tests; models train and report
  on synthetic data). The final commit's CI result is in the S3 report.

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
  (0005) and `uv run python -m market_intel.mongo_setup` (the 1.2 and 1.2b news_items
  validator and indexes), then `news migrate-exchange-keys` if its database holds exchange
  items, before running the instruments refresh or any news fetch there.
- Watchlist empty: company-fetch covers only recent filers until the owner adds stocks.
- Daily: the owner runs `upstox login` each morning (token ends 03:30 IST); candles after
  18:00 IST. Today's candle wasn't published by Upstox at 22:00 IST on 24 Nov (last stored
  day 23 Nov); the next incremental run picks it up.
- S4: the models don't beat the baselines after costs; nothing may become `active`. Paper
  trading may run on the candidates to collect outcomes, clearly labelled as such.
- S5i: with a 0.5% target and a 0.25% stop over 15 bars, about 70% of intraday decision
  points reach neither barrier and the target is hit only ~5% of the time; both sides average
  about zero gross return. Volatility-scaled barriers (and a shorter time limit) are worth
  testing before any strategy is built on these labels.
- Intraday price events: a split/bonus day is detected from that day's close, so it cannot be
  a feature at a decision point during the day. The event day and the next trading day are
  excluded from the labels instead, and only the previous day's event is a feature.
- Windows file locks: replacing the intraday progress file failed once with 'Access is
  denied' (a scanner or the indexer holding it) and stopped a backfill after 309 requests; it
  now retries. The other state files (alerts, daily candles, news, source health, the Upstox
  token) replace the same way and have not been changed.
- 1-minute candles make the Postgres backup much bigger and slower; `backup --verify` now
  takes 1 min 48 s, 686.9 MB; restore test 2 min 30 s.
- Price events: the split/bonus detector records only falling ratios and some events are
  not real splits (e.g. several 0.5 'events' within weeks); consolidations go undetected.
  An adjusted-price series (or a corporate-actions source) would make labels and returns
  more reliable.
- Dependencies: python-dateutil is 2.9.0.post0 (mlflow's graphene pins <3; owner-approved
  2026-11-25).
- yfinance by default impersonates a browser (curl_cffi); the project always passes an
  honest `requests` session. Yahoo served it on 24 Nov; if Yahoo starts refusing, the
  backup stops after 5 failures per run and source health reports it.
- Google News is best effort: a block pauses it until the next IST day; repeated blocks
  over several days go to the planner. 57 of the first 84 company items were low
  confidence; Phase 2 checks them.
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
