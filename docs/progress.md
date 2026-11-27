# Progress

Last updated: 2026-11-27, HEAD 02d1fe9. Updated at the end of every task (see
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
- Now: S6d (default the lumpsum ranking to a window that contains a real fall) in progress.
  S6a, S6b and S6c reviews all PASSED (planner, 2026-11-27). 0.11 (remove the testing-laptop
  dependency) stays planned for not before 2026-12-03.
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
- S6c review (planner, 2026-11-27): passed. Verified at HEAD 71c0ccd: CI green at the tip,
  compose-smoke green at 587c43c (the two commits after it are docs only); privacy clean; no
  old test weakened (38 new tests). Three bugs were found and fixed inside the task: every
  hybrid fund read "untested" because the hybrid index never fell 20%, the reference was blind
  before 2013 because Direct plans did not exist, and return_per_fall exploded on a 0.5%
  drawdown. Finding to act on, which becomes S6d: the automatic window (2021-2026) holds no
  fall, so the default ranks who climbed fastest in a bull run, not who holds up in a decline.
- S6b review (planner, 2026-11-27): passed. Verified at HEAD f0afce5: CI green at the tip,
  compose-smoke green at 17f4d2d (the two commits after it are docs only); no NAV data or
  results tracked; no old test weakened (65 new tests). Three Direct/Regular duplication bugs
  were found and fixed inside the task. Finding to act on, which becomes S6c: the ranking still
  judges each fund on its OWN history, so a 2020-vintage fund that never saw a crash can top
  the table on a flattering worst case.
- S6a review (planner, 2026-11-27): passed. Verified: CI and compose-smoke green at d35852c;
  migration 0011 present; no NAV data, scheme lists or results tracked; no old test weakened
  (46 new tests). Four bugs were found and fixed inside the task: three from this project's
  autocommit connections (a loader that committed for its caller, a temp table dropped the
  instant it was created, a server-side cursor outside a transaction) and a resume keyed on
  chunk boundaries that re-downloaded a finished backfill when the end date moved by a day.
  Minimum-history rule set for S6b: rank only funds with at least 5 years, flag 3-5 years as
  short history, and call anything under 3 years too new to judge.
- S5i review (planner, 2026-11-25): passed. Verified: CI and compose-smoke green at the tip;
  migration 0010 present; decisions appended; no data, model or log files tracked. The removed
  test in tests/test_intraday_candles.py (the old per-writer replace retry) was superseded by
  the shared `file_lock.safe_replace` and tests/test_safe_replace.py: an authorised refactor,
  not test-weakening. Finding to act on: nothing beat `no_trading`; the rules lose even at zero
  slippage, and the model loses less only because its EV gate trades about 4x less often.
- S4i review (planner, 2026-11-25): passed. Answers: (1) a price event on the decision day
  is not a feature: accepted; (2) the v1 barriers are too wide (~70% time-outs): S5i tests
  volatility-scaled barriers and shorter limits, and keeps v1 as a baseline; (3) NULL sector
  relative strength for Telecommunication, Capital Goods and Consumer Services: accepted;
  (4) the file-lock retry becomes ONE shared safe-replace helper used by every state writer;
  (5) the failed run 47 and the row difference: accepted; (6) the runbook must say "back up
  first, then verify".
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
| S4i | done 2026-11-25 (HEAD 55bedc8) | passed (planner, 2026-11-25) | 1-minute candles for the nifty50 universe, the Nifty 50, India VIX and 12 sector indices from 2022 (resumable, one month per request, thin days); the intraday timing convention; features v1 (41) and long/short triple-barrier labels as monthly Parquet with build records (migration 0009); the intraday leakage test (5 checks, 4 leaky fakes) in CI and on real data |
| S5i | done 2026-11-25 (HEAD ad735f5) | passed (planner, 2026-11-25) | Labels v2 (ATR-scaled barriers, 3x3 grid) with its own leakage check; 8 rule strategies with mood and no-trade filters; intraday charges (official rates) and the shared capital-sizing component; event-driven backtest after costs with migration 0010 (backtest_runs, backtest_trades, model kind 'intraday'); LightGBM meta-model per side (monthly walk-forward, holdout scored once); backtest / train / report / replay commands. NOTHING beats `no_trading` |
| S5i-b | done 2026-11-26 (HEAD 23dde7a) | passed (planner, 2026-11-26) | Labels v3 (ATR barriers held 30 / 60 bars or to the close) with the ATR invariant checked separately; `intraday precision` (probability deciles and the top 10/5/1% most-confident signals with their net result after costs); plain-percentage reporting against `no_trading`. BOTH questions answered NO: holding longer does not beat the fee, and no confidence band is profitable |
| S5i-c | done 2026-11-26 (HEAD 6874630) | awaiting planner review | `movers100` (107 liquid, volatile NSE stocks incl. the owner's 7 picks) chosen from the daily candles; 47.8M 1-minute rows; features and labels v2; isotonic calibration fitted from EARLIER months only; a probability cache. Rules still have NO gross edge; the model's EV gate pays over the walk-forward but decays to negative by 2026 |
| S6a | done 2026-11-27 (HEAD d35852c) | awaiting planner review | Mutual funds, the data and evidence half. Official AMFI NAV (NAVAll.txt and the 90-day history report, both verified live and parsed by their own header because their column orders differ); migration 0011 (mf_schemes, mf_nav, mf_nav_chunks); a resumable, polite backfill; and a per-fund stats engine (rolling returns, CAGR, volatility, drawdown, fall-and-recovery distributions, category percentiles) with a disclaimer and a survivorship note on every output |
| S6b | done 2026-11-27 (HEAD 17f4d2d) | awaiting planner review | Mutual funds, the decision half, offline and storing nothing. `mf lumpsum` (a ranking on three historical measures at once, inside a measured risk band), `mf sip` (a split across fund houses with each scheme's minimum, and the best/median/worst of that exact plan over every rolling window, with a plain stop rule) and `mf keep-or-exit` (what this fund and its whole category did after falls of the same depth, against the owner's horizon). The minimum-history rule - rank at 5 years, flag 3 to 5, refuse under 3 - is shared by all three and printed on every output. No migration |
| S6c | done 2026-11-27 (HEAD 02d1fe9) | awaiting planner review | The lumpsum ranking made comparable. The headline rank is computed over ONE window every ranked fund covers (`--common-years` demands a longer one; stale funds are dropped first so a dead fund cannot drag its end back). Each fund carries a survived-a-fall or untested-in-a-downturn flag, measured against a broad reference built from the sixty longest-lived funds held, which finds the 2011 and 2020 declines. Every spread is reported over non-overlapping windows as well as overlapping ones. No migration, nothing stored |
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
- S6c (2026-11-27): local databases unchanged, still at 0011. S6c computes on demand and
  stores nothing. Of 1,128 Direct-plan funds, 467 have the five years the ranking needs and 17
  are stale. The automatic shared window is 2021-11-15 to 2026-11-25 (5.0 years) and contains
  NO decline of 20% or more; `--common-years 10` gives 2016-11-25 to 2026-11-25, 302 funds,
  with the 2020 crash inside it. A whole-universe `mf lumpsum` takes about 70 seconds.
- S6b (2026-11-27): local databases unchanged, still at 0011. S6b computes on demand and
  stores nothing, so there is no migration and no new table. The universe used: 2,255 funds,
  of which 1,128 are Direct plans; 938 have the five years the ranking needs. A whole-universe
  `mf lumpsum` takes about 36 seconds, a 10-year `mf sip` about 40, and `mf keep-or-exit` with
  the category comparison about 50.
- S6a (2026-11-27): local databases at 0011 (migration applied after CI and compose-smoke
  were green at 8aab9e6). AMFI scheme master: 14,245 schemes on 2026-11-26, of which the
  universe (open-ended equity/hybrid/index on the GROWTH option) is 2,255 - equity 1,133,
  index 733, hybrid 389, across 52 AMCs. IDCW schemes are excluded because their NAV falls
  when they pay out. NAV backfill: 72 chunks of 90 days, 3,488,997 rows for all 2,255 funds,
  2011-01-03 to 2026-11-26 (15.9 years), about 4.0 GB of raw files under DATA_DIR, roughly
  75 seconds a chunk because AMFI's server takes that long to build one. A full rerun plans
  0 of 65 chunks and writes 0 rows. History held per fund: 245 funds have 15+ years, 362
  have 10-15, 331 have 5-10, 476 have 3-5 and 841 under 3 (new schemes).
  The stats engine on real data, by the real command: 4 named funds in 0.3 s; the whole
  equity category (1,133 funds) in 34 s through a server-side cursor, one fund at a time.
  What it shows, as evidence and nothing more: HDFC Flexi Cap over 15.9 years has a worst
  1-year window of -35.8% and a worst 10-year window of +9.2%; Parag Parikh Flexi Cap fell
  31.3% in Feb-Mar 2020 and regained that peak in 145 days, while SBI Small Cap's 40.3% fall
  from Jan 2018 took 1,044 days to regain and Nippon India Small Cap's 48.6% fall took 1,064.
  The 1-year spread is where the uncertainty lives; the 10-year spread is narrow for every
  fund looked at. NOTHING here is a prediction or a recommendation, and every output carries
  the survivorship note and the "not investment advice" disclaimer.
  Health overall WARN: 12 OK, 1 WARN (the Upstox token expired overnight; no Upstox call is
  part of this task), 0 FAIL, 1 SKIP; pg_migrations at 0011 (head). Postgres is at 73.6% of
  its space. Backup 1.2 GB (20 tables, 85,626,863 rows) with restore test OK.
- S5i-c (2026-11-26): local databases at 0010 (no migration). Universe `movers100`: 107
  stocks (100 by volatility above a Rs 25 crore median-turnover floor, from 317 of 497
  nifty500 members that cleared it, plus the owner's 7 named picks), median daily range
  3.14% against the nifty500's 2.81%. Backfill: 120 instruments, 0 failed, 47,752,568 rows,
  6,268 requests, 98 min (Upstox allows 1,600 requests per 30 min, so most of that is
  waiting); 1,066 new thin days. Features v1: 8,610,550 rows, 1,257 MB, 31 min (relative
  strength is 20.4% NULL because many mid-caps have no NSE sector index). Labels v2:
  17,221,100 rows, 1,044 MB, 89 min; long k15_b10 target first 26.9% / stop 63.8%, average
  gross -0.0083%; short 31.7% / 57.8%, +0.0097% - roughly 2.4x the nifty50 magnitudes in
  BOTH directions. check-leakage on this universe: PASS (300 samples, 465 s).
  THE DECISIVE NUMBERS. Rule strategies: the best gross edge at ZERO slippage is about
  -0.06 bps a trade (net -8.36 bps, charges 8.3 bps) against the 8.3 bps floor, so no rule
  has any gross edge at all; all eight ruined the account and `no_trading` won. The
  volatility did remove the negative drift seen on nifty50 (gross there was about -4.2 bps)
  but created nothing to pay the fee with. Meta-model: walk-forward AUC 0.572 (nifty50 was
  0.532), holdout 0.532 long / 0.544 short; its backtest took 2,306 trades at +9.18 bps a
  trade, +579.4%, Sharpe 2.44. That is NOT a validated edge: 82% of the profit is 2024
  alone and the edge runs +5.15 (2022), +10.25 (2023), +13.27 (2024), +4.75 (2025),
  -9.29 bps (2026), with the untouched holdout at -159 bps on 3 trades. The mechanism is
  identifiable: ranked by probability alone NO confidence band pays (the most confident 1%
  earns +2.30 bps gross long and +4.33 short against an 18.26 bps round trip), but the
  expected-value gate multiplies probability by barrier distance and so selects moments
  where the ATR-scaled target is wide - its trades average a 110 bps target against the
  rules' 49 bps. Wide-barrier moments were plentiful in 2024 and are not now. Nothing
  promoted. Health OK; backup 1.2 GB (82,123,542 rows), restore test OK.
- S5i-b (2026-11-26): local databases at 0010 (no migration). Labels v3: 8,031,544 rows in
  59 Parquet files, 549 MB, 48 min; check-leakage --labels v3 on real data PASS (500 samples,
  418 s).
  (a) Does holding longer help? NO. With an ATR-scaled barrier one side is almost always hit
  within 30 bars, so the time-limit outcome falls to about 0% and the 30-bar, 60-bar and
  hold-to-close results are nearly identical. Long k=1.5: target first rises 27.4% (15 bars)
  to 31.3% (to the close) while the average gross return stays at -0.0037% / -0.0038%; the
  extra wins are the former time-limit exits turning into stops. The v3 backtest matches the
  v2 one to a hundredth of a basis point (best edge -12.51 vs -12.55 bps at zero slippage,
  -22.95 vs -23.12 at 5 bps); all eight strategies still ruined the account.
  (b) Are the most-confident signals profitable? NO. Walk-forward, long side: the actual
  target-first rate rises monotonically across deciles (28.2% to 34.2%), so the ranking is
  real, but it is worth about 1.2 bps a trade against an 18.26 bps round trip. The model is
  badly overconfident where it matters: decile 10 says 42.6% and delivers 34.2%, and the top
  1% says 57.5% and delivers 33.6%. The top 1% is no better than the top 10% (gross +0.19 vs
  +0.25 bps for longs, +0.66 vs +0.90 for shorts), so the extreme tail is noise, not
  concentrated edge. Every confidence band loses money after costs, on the walk-forward and
  on the holdout (187,600 scored rows, so the holdout IS evidence here: the `one trade` of
  S5i was the EV-gated backtest, while `precision` scores every row). Replay of 2026-11-23:
  5 trades on Rs 496,750 of traded value, price moved 0.252% of it, charges took 0.183% of
  it, net -0.711% of capital. Health overall OK; backup 687.4 MB, restore test OK.
- S5i (2026-11-25): local databases at 0010. Labels v2: 8,031,544 rows in 59 Parquet files,
  571 MB, 29 min; the barrier mix is now balanced (long k15_b10: target first 27.4%, stop
  first 64.4%, time limit 8.1%, average gross -0.0037%; short 31.2% / 59.9% / 8.8%,
  +0.0040%), against v1's 5.5 / 24.7 / 69.8. Excluded: no_atr 2,581, thin_day 2,850,
  price_event 402, no_entry 55. Backtest (capital Rs 100,000, 5 bps slippage a side, 1,210
  days): all 8 rule strategies lose 23-25 bps per trade after costs and every one of them
  ruined the account (-75%); `no_trading` wins. At ZERO slippage the best rule still loses
  12.55 bps per trade while charges alone are about 8.3 bps, so the signals' own gross edge
  is roughly -4 bps. Meta-model (110 monthly folds, 19.5 min): walk-forward AUC 0.532 both
  sides, holdout 0.513 (long) and 0.529 (short); its backtest took 167 trades (32% hit,
  -18.79 bps, -29.1%) - better than the rules only because the expected-value gate trades 4x
  less often, and still negative. The holdout produced ONE trade, which is no evidence
  either way. Both sides registered as `candidate`; nothing promoted. Health overall OK;
  backup 687.3 MB in 1 min 56 s, restore test OK in 2 min 24 s.
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
- The size of the move IS the binding constraint, and it is now measured. A 1.5x more
  volatile universe doubled the gross move in both directions but left the rules with no
  edge, because the fee does not shrink with volatility. The only thing that cleared the fee
  was selecting moments where the ATR-scaled barrier is wide relative to the charge, and
  that supply of moments is regime-dependent (abundant in 2024, scarce by 2026). If intraday
  is revisited, that selection - not the signal type and not the holding time - is the thing
  to study, and it needs an instrument whose cost per unit of movement is far lower
  (index options or futures) rather than a more volatile cash stock.
- A whole-period calibration is look-ahead. Fitting one isotonic mapping on every
  walk-forward prediction and applying it back to those rows uses a month's own outcomes to
  calibrate it; the walk-forward error collapsed to 0.000 while the holdout reached only
  0.007. Each month is now calibrated from strictly earlier months. It changed the headline
  by only 0.05 bps here, but the next model it is used on may not be so forgiving.
- The default shared window contains no market decline. It is the longest stretch EVERY
  eligible fund covers, so the most recently launched eligible fund sets it, and on the data
  held that is about five years of a market that only rose. The comparison is fair but it is
  a comparison on calm; `--common-years 10` is what buys a window with 2020 in it, at the
  cost of dropping 160 funds. Anything built on the ranking should say which window it used.
- The market reference has to span EVERY plan. SEBI created Direct plans only in January
  2013, so a Direct-only reference starts in 2013 and cannot see the 2011 fall at all. It is
  also built from the funds that still exist, so it carries the same survivorship bias as
  everything else here and is a yardstick for WHEN the market fell, never a benchmark.
- A category's own index is useless as a decline yardstick for the quieter categories: the
  hybrid index never fell 20% even in 2020, so judging hybrid funds against it marked every
  single one "untested", including the ones that sat through the crash. One broad equity
  reference is used for all categories instead.
- Non-overlapping windows are not simply the honest version of overlapping ones. They use
  each stretch of market once, but there are few of them and a fall landing across a boundary
  can be split between two windows and show up in neither - one real fund shows a worst year
  of -27.9% overlapping and -5.5% independent. Both are printed; the narrower is not safer.
- An AMFI scheme code is a plan AND an option, so every fund is in the universe twice,
  Direct and Regular, with nearly the same NAV. `mf lumpsum` and `mf sip` therefore rank ONE
  plan at a time and default to Direct, and `mf keep-or-exit` pools its category evidence over
  the held fund's own plan. Anything else built on this universe has to make the same choice
  or it will count the same fund twice: pooling both plans turned 304 equity funds into 612
  and moved the median wait after a 28% fall from 312 days to 453.
- Five years of history is not the SAME five years. 2020 was the bottom of a crash, so a fund
  whose NAV begins in mid-2020 has a flattering worst-case and can top a ranking without ever
  having been through a fall. The minimum-history rule cannot catch this; `mf lumpsum` prints
  the span of start dates and each fund's own history line instead. A like-for-like ranking
  over a COMMON period is the honest fix and is not built.
- Rolling windows overlap almost completely: SIP starts a month apart share all but one
  instalment, so 46 windows are not 46 independent tries. Every report that shows a window
  spread has to say so, or the spread reads as more certain than it is.
- The minimum SIP is NOT in AMFI's files. It is an input with a 500 default and a printed
  warning; if the owner ever wants it to be right per scheme, it has to come from somewhere
  else (the scheme information documents, or by hand).
- AMFI serves at most 90 days of NAV history per request and takes 60-110 seconds to build
  each ~70 MB chunk, so a 15-year backfill is about 65 requests and a little over two hours.
  It is resumable in two places (the raw file and the `mf_nav_chunks` row), so an interrupted
  run costs at most one chunk. Downloading in parallel would work and the primary key already
  makes duplicates impossible, but AMFI publishes no rate limit and has no official
  alternative, so a block would cost the whole source; sequential is the deliberate choice.
- AMFI's two files order their columns DIFFERENTLY (the daily file puts the ISINs before the
  scheme name, the history report puts them after Plan and Option), and AMFI has retired the
  older format and added the Plan and Option columns. Any parser that counts positions is
  wrong against one of them; both are read by their own header line.
- IDCW schemes are excluded from the mutual-fund universe because their NAV drops when they
  pay out, so a NAV series is not a total return for them. If an IDCW fund is ever wanted, it
  needs a distribution-adjusted series, not the raw NAV.
- Survivorship bias is unavoidable with AMFI's files: they list the schemes that exist today,
  so funds merged away or wound up are missing and every historical statistic is flattered by
  their absence. Every stats output says so; S6b must not quietly drop the note.
- `feature_builds` has no universe column, so it cannot say where an incremental build
  should resume; the files on disk decide. If a third universe arrives, giving that table a
  universe column (a migration) would be cleaner than inferring it from paths.
- Intraday is now measured out on large-cap cash equity: no rule and no model beats
  `no_trading` at any slippage down to zero, at any hold from 5 bars to the session close,
  and in no confidence band including the model's most confident 1%. The gross edge is under
  1 basis point a trade against an 18 basis point round trip. What this does NOT rule out is
  a different instrument set (index options or futures, where the cost per unit of movement
  is far lower) or a wider, more volatile universe - the next task - for which every tool
  built here (labels, strategies, backtest, calibration, percentage reporting) is reusable
  unchanged.
- The model is overconfident, not merely weak: its ranking is sound but its stated
  probabilities are far too high at the top, which the expected-value gate trusts. Any later
  use of the probability should calibrate it (isotonic or Platt) on the walk-forward first.
- `intraday precision` retrains the whole walk-forward (about 20 minutes) because the
  walk-forward probabilities are not stored, only the fitted models. If this analysis is run
  often, cache the probabilities to Parquet beside the labels.
- Shorts beat longs consistently across every barrier variant on 2022-2026 Nifty 50 minute
  data (for example +0.0046% against -0.0038% average gross at k=1.5 held to the close).
  Still far below costs, but a real asymmetry worth a look if intraday is revisited.
- S6 and after: intraday does NOT pay on this evidence. Eight rule strategies and a LightGBM
  meta-model all lose after costs on 2022-2026 Nifty 50 minute data, and the rules lose even
  with zero slippage. Nothing may be promoted or paper-traded as if it worked. What is NOT
  ruled out: a different instrument set (index options or futures, where the cost per unit of
  movement is far lower), a longer intraday horizon, or using the model only as a veto on
  rules rather than a signal of its own.
- Costs decide intraday: an intraday round trip costs about 0.13% of the position in charges
  alone (0.083% at a Rs 100,000 position, where the Rs 20 brokerage cap binds). A Nifty 50
  stock rarely moves that far in 15 minutes, which is most of the reason nothing works.
- Intraday (MIS) leverage is NOT modelled: the exposure cap is one times capital, so a 0.25%
  stop risks about 0.25% of capital rather than the nominal 1%. Real MIS margin would change
  the sizing (and the losses) substantially.
- The IPFT charge is ambiguous on Upstox's own pages (Rs 0.01 per crore in one table,
  Rs 0.10 per lakh in another, and a note that it is now folded into the transaction charge).
  It is treated as included; at the larger figure it would add about 3% to the charges on a
  round trip.
- model_versions.horizon counts trading days for a daily model and one-minute bars for
  kind = 'intraday'. Worth splitting if a third kind of horizon ever appears.
- Intraday price events: a split/bonus day is detected from that day's close, so it cannot be
  a feature at a decision point during the day. The event day and the next trading day are
  excluded from the labels instead, and only the previous day's event is a feature.
- Windows file locks: every state writer (alerts, daily and intraday candles, news, source
  health, the Upstox token, the instruments cache) now swaps its temp file in through
  `file_lock.safe_replace`, which retries while the target is locked (S5i). A locked file
  had stopped an S4i backfill after 309 requests.
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
