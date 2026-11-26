# Decision Log

## 2026-11-12 - Workspace and sync setup
- The project folder is a standalone git repo (branch `main`), separate from any other repo on the machine.
- Code syncs through a personal GitHub remote. Only `drive_sync/` syncs to Google Drive (it's git-ignored), so Drive never touches `.git`.
- `.env` is synced by neither; copy it by hand when moving to the main laptop. [redacted 2026-11-18: device names removed for privacy]

## 2026-11-12 - Machines and phases
- Decision: The testing laptop is for testing and validation until end of Nov 2026. The main laptop becomes the main machine from Dec 2026. Go live only after validation. [redacted 2026-11-18: device names removed for privacy]
- Concern (planner): Not my own device; personal project files and credentials on it.
- Reason (owner): Only machine available until December.

## 2026-11-12 - No real Upstox keys on the restricted profile
- Decision: Real broker credentials are used only on the main laptop. [redacted 2026-11-18: device names removed for privacy]
- Concern (planner): Files on a device I don't fully control may be readable by others.
- Reason (owner): Accepted planner default.

## 2026-11-12 - Google Drive not used (updates "Workspace and sync setup")
- Decision: drive_sync/ stays a local, git-ignored folder. Git is the backup for code. [redacted 2026-11-18: device names removed for privacy]
- Concern (planner): No off-machine backup for exports on the testing laptop.
- Reason (owner): Google Drive can't be installed on the testing laptop; market data can be re-downloaded.

## 2026-11-12 - Databases during testing: free cloud
- Decision: MongoDB Atlas free tier plus a free Postgres host (chosen in task 0.4) on the testing laptop. Docker Compose on the main laptop from December. [redacted 2026-11-18: device names removed for privacy]
- Concern (planner): Small free limits (~0.5 GB each); data and connection strings held by third parties; differs from the final Docker setup. SQLyog only works with MySQL, not Postgres.
- Reason (owner): The testing laptop can't run Docker. Cloud data can be reached from any laptop and removed later.

## 2026-11-12 - Python 3.12 managed by uv
- Decision: Python 3.12 installed per-user by uv. System Python 3.14 untouched. uv lock file for identical installs on every machine.
- Concern (planner): 3.14 may lack builds for some ML libraries. Colab runs 3.12 (checked 2026-11-12).
- Reason (owner): Accepted.

## 2026-11-12 - GitHub via GitHub CLI
- Decision: GitHub CLI (per-user zip) lets Claude Code create the private repo "market-intel" and push. Local folder name stays "Stocks".
- Concern (planner): Claude Code can push without my review. Mitigated by the pre-commit secret scan, a PRIVATE check before the first push, and logins done only by me.
- Reason (owner): Owner wants Claude Code to create the repo and commit.

## 2026-11-12 - Code location
- Decision: A local folder that isn't OneDrive-synced. Per-repo personal git identity.
- Concern (planner): Other work lives on the same machine; keep identities, credentials and content separate.
- Reason (owner): Owner's choice.

## 2026-11-12 - Quality and secret safety
- Decision: ruff + pytest + GitHub Actions CI. .gitignore, .env.example with empty values, detect-secrets in pre-commit and CI.
- Concern (planner): A committed password stays in git history; it must be changed, not just deleted.
- Reason (owner): Accepted.

## 2026-11-12 - Market data sources
- Decision: Upstox API (free; needs an Upstox account, to be opened before Phase 1). yfinance as daily backup for stocks, plus a macro-only exception (USD-INR spot, crude); source tagged on every row. Groww API rejected (Rs 499/month). FII/DII flows via weekly manual CSV.
- Concern (planner): No official free FII/DII API, and NSE blocks automated access.
- Reason (owner): Accepted defaults; zero-cost rule.

## 2026-11-12 - News sources
- Decision: RSS from ET Markets, Moneycontrol, Business Standard and Mint, plus NSE/BSE corporate announcements. Headline and summary only; no full-article scraping.
- Concern (planner): Respect site terms.
- Reason (owner): Owner wants every source the AI needs for decisions.

## 2026-11-12 - Stock universe
- Decision: Daily data for all actively traded NSE stocks; minute data only for my watchlist (up to ~50); on-demand status for any stock. Mutual fund status comes in Phase 7.
- Concern (planner): Small caps mean thin liquidity, pump-and-dump risk and weaker predictions. The system screens and explains; it is not investment advice.
- Reason (owner): Owner wants to find growing companies and check any stock.

## 2026-11-12 - Alerts
- Decision: Email alerts (Gmail app password).
- Concern (planner): None.
- Reason (owner): Telegram notifications are off on my phone.

## 2026-11-12 - LLM providers and budget
- Decision: Gemini free tier first, Groq free tier second, paid fallback chosen in Phase 2. Hard cap Rs 1,000/month, warning at 80%. Rs 2,000 only with my explicit approval when truly needed.
- Concern (planner): The Gemini free tier was cut in 2026 (Flash only, low daily limits). Google AI Pro and Claude subscriptions do not include API access.
- Reason (owner): Zero cost by default.

## 2026-11-12 - Fundamentals, paper trading, pace
- Decision: Fundamentals from NSE/BSE filings, dated by filing date (Rs 0). Paper-trading parameters to be proposed in Phase 4. No deadlines; fast-changing facts are re-checked at each task. The laptop isn't on at market open, so the live feed misses the start of the session; history comes from candle backfill.
- Concern (planner): Fundamentals dated by quarter-end would leak future information.
- Reason (owner): Accepted defaults.

## 2026-11-12 - Config: pydantic-settings, one .env per machine
- Decision: pydantic-settings. APP_PROFILE = restricted | full | cloud. Defaults live in code; each machine's .env sets only what differs. Settings are grouped and each group is checked when that part starts. Secrets are SecretStr. A command prints settings with secrets masked. [redacted 2026-11-18: device names removed for privacy]
- Concern (planner): Per-machine settings files would be easier to read, but aren't worth maintaining while machines differ little.
- Reason (owner): Accepted the planner's recommendation.

## 2026-11-12 - Decisions enforced in code
- Decision: The restricted profile refuses to start if Upstox keys are present. LLM cap defaults to Rs 1,000; above that needs LLM_CAP_RAISE_APPROVED=true; Rs 2,000 is the absolute maximum; warning at 80%. [redacted 2026-11-18: device names removed for privacy]
- Concern (planner): An env flag is easy to flip. The real approval is my explicit OK, logged here BEFORE the flag is set.
- Reason (owner): Turn decisions into checks so they can't be skipped by mistake.

## 2026-11-12 - CI runner pinned to ubuntu-24.04
- Decision: CI runs on ubuntu-24.04 instead of ubuntu-latest.
- Concern (planner): GitHub will eventually retire this image; bump it deliberately when that happens.
- Reason (owner): ubuntu-latest is moving to Ubuntu 26; OS changes should be our choice.

## 2026-11-12 - Changes made during task 0.1 (logged after review)
- Decision: CI actions pinned to exact current versions (actions/checkout@v7, astral-sh/setup-uv@v10.3.3, pre-commit-hooks v6.0.0). detect-secrets skips uv.lock only.
- Concern (planner): checkout@v7 is a moving tag. Acceptable: CI has read-only access and no secrets.
- Reason (owner): setup-uv no longer publishes major tags; package hashes in uv.lock look like secrets and would block every dependency update.

## 2026-11-12 - Machine-specific paths in the decision log
- Decision: Older entries kept the local folder path. Updated 2026-11-13: paths removed before the repo went public; the "no machine-specific paths" rule now covers every committed file.
- Concern (planner): None.
- Reason (owner): The repo is public.

## 2026-11-12 - Alerts: dedicated Gmail through an app password
- Decision: A separate Gmail used only for sending alerts (2-Step Verification + app password in .env). Alerts arrive in my main inbox. Gmail SMTP on port 587 with STARTTLS. [redacted 2026-11-18: device names removed for privacy]
- Concern (planner): An app password gives full access to that Gmail and sits on the testing laptop; the risk is limited by using a dedicated empty account. Rejected: Gmail OAuth send-only (personal apps must re-authorise every 7 days) and Brevo/Resend (need an owned domain for reliable delivery).
- Reason (owner): Free, works now, keeps my main mailbox safe.

## 2026-11-12 - Logging: structlog, JSON files, secret masking
- Decision: structlog with the standard logging module. JSON lines in logs/, a new file at UTC midnight, 30 days kept. Readable console in IST; all stored times in UTC. Masking of secret-looking field names (key, secret, password, token, uri, url...), URL credentials, and any loaded secret value. tzdata added; DISPLAY_TIMEZONE validated.
- Concern (planner): Masking every field named *url* may hide useful links (e.g. RSS feed URLs) in logs; revisit with an allowlist in Phase 1 if it gets in the way. Audit records stay in MongoDB, not in these logs.
- Reason (owner): Accepted the planner's recommendation.

## 2026-11-12 - Alert behaviour
- Decision: Levels info / warning / critical; only warning and critical send email. The same alert emails at most once per 6 hours (state kept in data/ so it survives restarts). Dry-run mode. Alert text masked like logs. Sending failures are logged and never crash the caller.
- Concern (planner): The throttle can hide a repeating problem for up to 6 hours; suppressed alerts are still logged.
- Reason (owner): Avoid alert floods.

## 2026-11-12 - Emails masked in diagnostic output
- Decision: The config command and alert logs show emails as m***@domain.
- Concern (planner): None.
- Reason (owner): Output gets pasted into chats and screenshots.

## 2026-11-13 - Public repo under PolyForm Strict 1.0.0
- Decision: The repo is public and source-available under the PolyForm Strict License 1.0.0 (noncommercial use only; no changes or redistribution). It replaces the private repo with one fresh commit, so earlier history (with machine details) is never public. The old private repo is kept as market-intel-private.
- Concern (planner): Public code can be read and copied regardless of the license; strategy details, decisions and future model logic are visible. Secrets stay in .env and are blocked by detect-secrets. Not an OSI open-source license.
- Reason (owner): Owner wants the project public while limiting reuse.

## 2026-11-13 - Testing-phase Postgres: Neon free plan
- Decision: Neon free plan, Postgres 18, AWS Singapore, Neon's default database "neondb", direct connection with TLS. The main laptop's local database will be named market_intel; the connection URL carries the name, so code doesn't care. [redacted 2026-11-18: device names removed for privacy]
- Concern (planner): Singapore adds latency; compute sleeps after 5 minutes (first query ~1 s slower); the free plan accepts connections from any IP, so protection is the generated password plus TLS; 1 GB fits testing only. An unused pooled connection string is also kept in .env, which is one more copy of the password.
- Reason (owner): Twice Supabase's storage, no weekly pause (Supabase) or power-off (Aiven), TimescaleDB available for task 0.5. Default database name and pooled string kept for simplicity. Limits checked 2026-11-13.

## 2026-11-13 - Testing-phase MongoDB: Atlas M0 in Mumbai
- Decision: Atlas M0 in a separate project "market-intel", region Mumbai (ap-south-1), MongoDB 8.0. The Atlas-created database user (names can't be changed) restricted to readWrite on market_intel only. Network access limited to my current IP.
- Concern (planner): About 512 MB and 100 operations/second. If my IP changes, access must be updated; opening it to all IPs needs a long random password and a new decision entry.
- Reason (owner): Existing account; free; Mumbai is closest.

## 2026-11-13 - Database drivers and connection check
- Decision: psycopg 3 and PyMongo (Motor retired). db_check reports host, version, latency, client-side TLS, storage vs free limit (warning at 80%) and a write test, and sends a critical alert on failure.
- Concern (planner): Neon's server-side TLS view (pg_stat_ssl) reads false because its proxy handles encryption, so TLS is checked on the client side.
- Reason (owner): Accepted the planner's recommendation.

## 2026-11-13 - Docker Compose for the main laptop, tested in CI
- Decision: docker/compose.yaml with postgres 18 and mongo 8.0 (same as the cloud), ports on 127.0.0.1 only, memory limits, health checks, multi-arch images. Credentials in the git-ignored docker/compose.env. Least-privilege app user. A CI job starts the stack and runs db_check whenever database files change. [redacted 2026-11-18: device names removed for privacy]
- Concern (planner): Uses CI minutes on a private repo, so the job runs only on relevant changes. The TimescaleDB image decision is left to task 0.5.
- Reason (owner): Verify the main laptop setup now, without Docker on the testing laptop.

## 2026-11-13 - Planner review of the public repo
- Decision: The repo stays public (owner's decision). The concerns in the entry "Public repo under PolyForm Strict 1.0.0" were written without planner review; the planner's concerns are recorded here.
- Concern (planner): (1) Publishing current stock-specific predictions or signals could count as unregistered investment advice or research under SEBI rules, even with a disclaimer. (2) Bots scan public repos within minutes, so any leaked key must be changed immediately (GitHub secret scanning and push protection are on). (3) Security details in the docs are public, so they are kept general from now on.
- Reason (owner): Share code and documentation as a learning project; others run it with their own data.

## 2026-11-13 - SEBI and data-use rules for this project
- Decision: The public repo holds code and documentation only. No market data, predictions, signals, model files, notebook outputs or paper-trading results are ever committed (enforced by .gitignore, nbstripout and a safety test). The dashboard runs only on my own laptop with no other users. Predicted-vs-actual results are shown only in person (e.g. interviews), and nothing public claims accuracy or returns. Disclaimer in the README (and the dashboard later). Building and testing models for my own use needs no SEBI permission.
- Concern (planner): SEBI looks at substance, not labels: a disclaimer does not turn a recommendation into education. Unregistered education may only use price data at least 30 days old (SEBI circular, May 2026; confirm on sebi.gov.in). Broker and exchange data can't be redistributed. Automated real orders later would need the broker's algo approval under SEBI's retail algo framework. Not legal advice.
- Reason (owner): Learning project and personal use only.

## 2026-11-13 - Correction: CI minutes
- Decision: The 0.4 entry's concern "Uses CI minutes on a private repo" no longer applies; the repo is public, so standard GitHub-hosted runners are free.
- Concern (planner): If the repo goes private again, the concern returns.
- Reason (owner): Keep the log accurate.

## 2026-11-13 - Database encryption rule
- Decision: db_check refuses, before connecting, any unencrypted connection to a non-local database host (exit 1 + critical alert). MongoDB TLS is read from the connection string first. The Docker test also runs when uv.lock, pyproject.toml, config.py or migrations change.
- Concern (planner): None.
- Reason (owner): Accepted the 0.4 review recommendations.

## 2026-11-18 - Migrations: Alembic + SQLAlchemy Core
- Decision: Alembic with SQLAlchemy 2 Core table definitions. Migrations live in migrations/ and run by hand with "alembic upgrade head" using the owner login (POSTGRES_ADMIN_URL), never automatically. CI proves every migration can be applied, undone and re-applied, and that code and migrations match (alembic check). [redacted 2026-11-18: device names removed for privacy]
- Concern (planner): Downgrades delete tables and data, so they run only in CI, never on Neon or main laptop data.
- Reason (owner): Accepted the planner's recommendation.

## 2026-11-18 - Plain Postgres, no TimescaleDB for now
- Decision: Plain Postgres 18 with composite primary keys and BRIN time indexes. Tables are designed so they could become TimescaleDB hypertables later. [redacted 2026-11-18: device names removed for privacy]
- Concern (planner): Neon offers only TimescaleDB's Apache-2 features (no compression), so using it would make Neon and the main laptop behave differently. Revisit only if volumes demand it. This revises the 0.4 reason for picking Neon, which mentioned TimescaleDB.
- Reason (owner): Accepted.

## 2026-11-18 - Two Postgres logins
- Decision: The owner login (POSTGRES_ADMIN_URL) is used only for migrations. mi_app (POSTGRES_URL) is used for everything else, with SELECT/INSERT/UPDATE/DELETE on tables only: no CREATE, DROP, TRUNCATE or ALTER. On Neon, mi_app was created through the SQL Editor, because roles created in the Neon console get admin rights (neon_superuser). On Docker, an init script creates it.
- Concern (planner): Two passwords to manage; app code must never use the owner URL.
- Reason (owner): Least privilege, as with MongoDB.

## 2026-11-18 - Schema v1
- Decision: instruments, candles_daily, candles_intraday (1-minute only), ingestion_runs, data_gaps, llm_spend, plus a monthly IST spend view. UTC timestamps; trading days as IST dates; prices numeric(14,4); every candle row records its source and whether its prices are raw or adjusted. Upstox rows replace yfinance rows for the same day, never the reverse.
- Concern (planner): Splits and bonuses change past prices; adjustment handling is designed in Phase 1. Predictions, paper trades and features come in later migrations.
- Reason (owner): Only what Phase 1 needs.

## 2026-11-18 - Encryption check in every connection
- Decision: The refusal of unencrypted connections to non-local hosts moved into the connection functions, so every caller is covered. Single-word hostnames count as local only on the full and cloud profiles. [redacted 2026-11-18: device names removed for privacy]
- Concern (planner): None.
- Reason (owner): Accepted the 0.5a review recommendations.

## 2026-11-18 - MongoDB collections v1 and validation
- Decision: news_items, llm_extractions and prediction_audit (plus _healthcheck). Pydantic models define the documents, and MongoDB $jsonSchema validators (strict, error) enforce required fields and types in the database too. Every news item and extracted event keeps the time it became public. An audit record is refused if its feature cutoff or any news it used is later than the prediction time (an early leakage guard).
- Concern (planner): Validators check shape, not meaning. The full leakage test comes in Phase 2.
- Reason (owner): Accepted the planner's recommendation.

## 2026-11-18 - Audit records are append-only
- Decision: A prediction audit is written once; only its outcome can be added later, once (outcome_status pending -> filled). The app login has no remove right on prediction_audit, so the database refuses deletes. The code offers no other way to change audit records.
- Concern (planner): The app login still has update on prediction_audit (needed for the outcome), so "only the outcome changes" is enforced in code, not by the database.
- Reason (owner): Keep the record honest.

## 2026-11-18 - Two MongoDB logins
- Decision: The setup login (readWrite + dbAdmin on market_intel) is used only by mongo_setup. The app login uses the custom role mi_app_rw, with rights granted per named collection: news_items, llm_extractions and _healthcheck get find, insert, update, remove, listIndexes and collStats (_healthcheck without update); prediction_audit gets the same without remove. listCollections, dbStats and killAnyCursor are database-wide. The app login cannot create, drop or change collections, indexes or validators. The Docker init script creates the same setup.
- Concern (planner): Atlas user names can't be changed, so they differ from the plan. Each new collection now needs an Atlas role change, recorded in this log.
- Reason (owner): Least privilege, as in Postgres.

## 2026-11-18 - Postgres fixes from the 0.5b review
- Decision: Migration 0003 adds a trigger that keeps instruments.updated_at current and grants mi_app the TEMP right explicitly. The Alembic message now reads "Connecting to host".
- Concern (planner): None.
- Reason (owner): Accepted the 0.5b review recommendations.

## 2026-11-18 - MongoDB: insert also creates collections
- Decision: Rights are granted per named collection instead of database-wide. Code reaches collections only through a helper that accepts names from the fixed list, and db_check warns about unexpected collections.
- Concern (planner): The planner's first role design assumed that leaving out createCollection prevents new collections. That was wrong: MongoDB's insert right also creates a missing collection. The compose-smoke tests caught it by trying it, and permissions are now checked by behaviour, not only by their list. If mongo_setup hasn't run, the app could still create one of the four named collections; mongo_setup then adds its validator and indexes.
- Reason (owner): Keep the guarantee that every collection goes through mongo_setup, enforced by the database, with code as a second check.

## 2026-11-18 - Daily health check
- Decision: One command (market_intel.health) runs every check (config, databases, migration level, MongoDB setup drift, MongoDB rights tested by behaviour, unexpected collections, disk, data freshness, Upstox token, LLM spend) and prints OK/WARN/FAIL/SKIP with reasons, or JSON for the Data Health page. Any FAIL exits 1 and sends one throttled critical alert. Parts not yet built show SKIP. It uses only the app logins.
- Concern (planner): The rights probe uses only deletes that match nothing and one insert that should be refused. If the role were ever wrong, that insert could create a stray collection; health then reports it for manual removal.
- Reason (owner): One thing to run each morning, reused later by the dashboard.

## 2026-11-18 - Compute check and Colab template
- Decision: compute_check reports CPU, RAM, disk and any NVIDIA GPU (via nvidia-smi; no PyTorch yet) with a recommendation. The Colab template checks the GPU, clones the public repo, installs it and runs compute_check. Secrets come only from Colab's Secrets panel. How training data reaches Colab is decided in Phase 3.
- Concern (planner): The project pins Python 3.12, so re-check Colab's Python version before Phase 3. Atlas only accepts known IP addresses, so Colab can't reach MongoDB.
- Reason (owner): Accepted.

## 2026-11-18 - Documentation structure
- Decision: The README is a short entry point (what it is, the disclaimer, quick setup, daily commands, links). docs/architecture.md explains how the system works (Mermaid diagrams, where data lives, the security model, schema, MongoDB rules, the health check). docs/runbook.md explains how to operate it (the daily and two-laptop routines, fixing health-check problems, setting up a laptop, changing secrets, adding a collection). Every task updates the affected docs, and every task report lists the doc changes or says why none were needed. A docs test checks links, README length, Mermaid blocks and required sections.
- Concern (planner): The tests check structure, not truth, so the docs can still drift from the code; every review checks the content too. Security details stay general because the repo is public.
- Reason (owner): One place to understand the system (also for interviews), and a README that stays readable.

## 2026-11-18 - Device-neutral names
- Decision: Profiles are named by what a machine may do: restricted (testing; cloud databases; no broker keys, rule R1), full (main work; Docker databases; broker keys allowed), server (later hosting). Device names and machine details were removed from code, docs and older decision entries (each edited entry is marked as redacted), and a test blocks device names from coming back.
- Concern (planner): The old wording remains in the public git history; rewriting history with a force-push isn't worth the risk for laptop names. Naming profiles after devices leaked them once the repo went public; the planner should have flagged it then.
- Reason (owner): Third parties don't need to know which machines I use.

## 2026-11-20 - Shared tracking in the repo
- Decision: Each laptop has its own planner chat and Claude Code with separate memory, so the repo is the shared memory: CLAUDE.md (standing rules, loaded by Claude Code automatically), docs/spec.md (the project brief, kept current), docs/workflow.md (how owner, planner and Claude Code work), docs/progress.md (task status, next task, carry-forward items). Every session starts with git pull and reading progress.md; every task ends by updating it.
- Concern (planner): Everything here is public, so personal details, laptop specifics and account information stay out; the planner's start-up prompts, which hold them, are kept outside the repo. If progress.md isn't updated, sessions drift, so every review checks it.
- Reason (owner): Work alternates between two laptops; GitHub is the only common place.

## 2026-11-20 - Main laptop set up
- Decision: The main laptop runs the full profile. Tools were installed or updated with winget (Docker Desktop's old version had a critical container-escape flaw, CVE-2025-9074), WSL memory is capped at 6 GB, the repo sits on a second drive, and the Git identity is the GitHub no-reply address. The health check is overall OK on both laptops.
- Concern (planner): On a 15 GB laptop Docker holds its memory even when idle, so quit it when local databases aren't needed. Before the switch, this laptop's global Git identity was a non-personal address; a commit-email guard follows in 0.9b.
- Reason (owner): Alternate between both laptops through git.

## 2026-11-20 - Local Docker databases on the main laptop; commit-email guard
- Decision: The main laptop's full profile uses fresh local Docker databases from now (migrated to head, mongo_setup applied); the testing laptop stays on the free cloud databases until it is handed back, and 0.10 decides whether the cloud databases are deleted or kept as an off-site backup. Local data is backed up with market_intel.backup (pg_dump + mongodump) to a folder outside the repo, with a tested restore. Backups use the Postgres superuser inside the container and the Mongo setup user; the restore test uses the admin logins only on throwaway databases. Commits must use the GitHub no-reply address: a pre-commit hook blocks other emails and a CI test checks every commit. Capability numbers (RAM, GPU memory, memory caps) may appear in public docs; device names and models, hosts, accounts and employers may not.
- Concern (planner): Once ingestion starts, the local databases are the only copy of the data: backups must be copied off the laptop regularly, and `docker compose down -v` deletes everything. The free cloud tiers are too small for Phase 1 data. GitHub's push block only covers addresses registered on the account, so the repo-side guard is still needed.
- Reason (owner): Work moves to the main laptop in December, and my email stays private.

## 2026-11-21 - Phase 0 wrap-up: cloud databases, backup age, AI access through MCP
- Decision: Phase 0 is complete and tagged phase-0. The free cloud databases are used only by the testing laptop and are deleted in 0.11 (not before 2026-12-03); copies away from the main laptop come from copying the backup folder to external storage. Health warns when the newest complete backup is older than BACKUP_MAX_AGE_DAYS (default 7). The owner opens the Upstox account before Phase 1. AI tools query project data only through an MCP connection, free options only (an existing free MCP server or one built in this repo), with a read-only login; the concrete server is chosen in a later task after checking current options.
- Concern (planner): Once the cloud databases are deleted, the only copies away from the main laptop are the external backup copies, so copying them is essential. A database MCP connection gives an AI tool direct access to the data: it must be read-only, local only, and never use the admin logins.
- Reason (owner): Keep everything free and simple, and let AI query my data in one controlled way.

## 2026-11-21 - Phase 1 plan and methods
- Decision: Phase 1 is built as tasks 1.0 to 1.10 (groundwork; instruments and watchlist; news RSS; Upstox login and token; daily candles with gap detection and backfill; intraday candles; macro; live websocket feed; scheduler; read-only MCP access; wrap-up). Tasks 1.0 to 1.2 run while the Upstox account is being approved. The Upstox token comes from a manual daily browser login with one command and is kept only on the laptop; no stored passwords or TOTP secrets. A Python scheduler in the repo, started by Windows at logon, runs the jobs and catches up after downtime. The Upstox Basic plan is enough: the APIs are free and the normal websocket limits cover the watchlist. Each program logs to its own file and alerts are sent in the background.
- Concern (planner): Upstox limits (2000 requests per 30 minutes; 1-minute history only from Jan 2022, at most one month per request) make the first backfill take hours, so it must resume safely. With a manual daily login there is no token on days the owner doesn't log in; gap detection and backfill must cover those days. Broker and exchange data must never be committed or redistributed.
- Reason (owner): Start building while the account is approved, keep the daily login simple and safe, and keep the code portable to a server later.

## 2026-11-20 - Cloud MongoDB accepts any address (logged 2026-11-22)
- Decision: The cloud MongoDB network access list is "allow from anywhere". Protection is the generated passwords, TLS on every connection (the code refuses unencrypted ones) and the limited app login. It replaces the 0.4 rule "network access = current IP only".
- Concern (planner): A leaked connection string would work from any network, so passwords stay only in .env and are changed at once if exposed. The cloud databases are deleted in 0.11, which ends this exposure.
- Reason (owner): The home ISP shares IPv4 addresses (CGNAT), so database traffic left through a different address than the browser and single-address entries kept breaking.

## 2026-11-22 - Instruments and watchlist
- Decision: Instruments come from Upstox's public daily instrument file (NSE.json.gz, no login): NSE stocks of series EQ and BE plus all NSE indices, refreshed by one command inside an ingestion run. Rows are never deleted: a missing instrument becomes inactive, symbol changes are kept in instrument_symbol_history, and a symbol can be reused only by an active instrument. A safety valve refuses a refresh whose file is much smaller than the active list. Sectors come from a Nifty 500 list CSV that the owner downloads by hand and imports with a command. The watchlist (max 50) lives only in the database.
- Concern (planner): BE stocks are often under exchange surveillance and riskier; they are flagged, not hidden. Absence from the file isn't proof of delisting. Stocks outside the Nifty 500 have no sector yet. Choosing watchlist stocks is the owner's decision; the system doesn't recommend.
- Reason (owner): One official, free source with the same keys as the candle API, and private picks stay out of the public repo.

## 2026-11-22 - Strict URL masking in logs
- Decision: Logs hide the whole user part of any URL (user name and password), the same as stored ingestion errors.
- Concern (planner): None.
- Reason (owner): Accepted the 1.0 review answer; user names must not appear in output.

## 2026-11-22 - Data collection: legitimate channels only
- Decision: Data comes only from RSS feeds, official files, APIs and pages the owner captures. The code uses an honest identity and never imitates a browser or works around blocks, CAPTCHAs or logins. Per-company news uses Google News RSS for the watchlist (1.2b), not search-page scraping. Official NSE files (bulk/block deals, bhavcopy delivery %, surveillance lists) are downloaded once a day (1.6b), stopping with an alert if blocked; 1.6 revisits the manual FII/DII import. Moneycontrol is excluded because it blocks non-browser clients.
- Concern (planner): Using a blocked source rarely doesn't change its terms. Publishers' RSS terms usually allow personal, non-commercial use with a link back; ours is stored locally and never republished. Official files can change format or start blocking, so source-health alerts are required.
- Reason (owner): Wider coverage of every factor that moves prices, collected safely.

## 2026-11-22 - News and exchange feeds
- Decision: 11 RSS feeds (6 news, 5 official NSE/BSE) are fetched politely (at most once per 10 minutes per feed, conditional requests, size limits) into news_items: headline, summary and link only. Each item stores published_at, fetched_at and available_at (the later of the two), and Phase 2 features use available_at. Exchange items are linked to instruments only on a clear match. A run is success, partial or failed. A reusable source-health monitor emails when a source fails three times in a row, returns nothing, returns mostly malformed items, drops far below its usual volume, or loses its publish times.
- Concern (planner): Feeds change without notice; health alerts catch it, but fixing a parser remains a manual task.
- Reason (owner): Every broken source must be reported by email so the code can be fixed.

## 2026-11-22 - Phase 2 additions: local LLM and page capture
- Decision: A local LLM (Ollama, small models on the 4 GB GPU) is the first choice for extraction, then the Gemini/Groq free tiers; the paid cap is unchanged, and accuracy is measured before trusting any model. Filing PDFs are read with OCR when needed. Page capture for crucial decisions has two features: (1) automatic browser capture to PDF only for pages that allow automated access (robots and terms respected), stopping at any block; (2) evidence requests, where the system lists exact URLs (checked automatically where allowed, and marked valid or invalid by the owner on screen), the owner saves the pages as PDF, and the system takes them from an upload/file picker or from the Downloads folder (only new PDFs saved after an open request, and only after the owner confirms the match). The capture time counts as available_at.
- Concern (planner): Small local models are less accurate than large ones; captured pages stay on the laptop for personal use and are never committed or shared.
- Reason (owner): Free, private, and a full picture before crucial decisions.

## 2026-11-22 - Intraday design
- Decision: Intraday uses a fast numeric path: a small model (e.g. LightGBM) on 1-minute features (price, volume, order-book imbalance, market and sector moves, VIX, time of day, the latest background news score) with millisecond inference and no LLM in the live decision. Labels use the triple-barrier method (target, stop-loss or time limit, whichever comes first). Every signal is a full trade plan (entry, target, stop-loss, exit time, quantity, maximum loss, probability, expected gain after all costs) and is "no trade" unless the expected gain after costs is positive. Fixed risk per trade, a daily loss limit and a maximum number of trades. 15 minutes is the main horizon; 5 minutes is an experiment. The objective is maximum profit after costs with capped loss; nothing is guaranteed. Order-book depth is recorded from 1.7 because no history exists.
- Concern (planner): SEBI's study found 71% of individual intraday traders in the cash segment lost money in FY2023, and costs add heavily to losses; very short horizons are dominated by noise and charges. Everything is paper-traded first, and promotion needs the owner's approval.
- Reason (owner): Intraday needs fast decisions on a laptop, and clear "invest this, aim for this, risk at most this" plans.

## 2026-11-22 - Factor map, manipulation flags and expert ideas
- Decision: docs/factors.md lists the factors that move prices, their sources and when each is built; contributions are measured per sector and stock with SHAP, not assumed. Phase 5 adds manipulation risk flags (pump and dump, wash trading, operator-led rises, spoofing) learned from exchange surveillance lists and SEBI enforcement orders, used only to avoid or down-weight stocks. Expert trading ideas are hypotheses, tested walk-forward after costs with a final untouched test period.
- Concern (planner): Testing many ideas makes some look good by luck; joining or riding a suspected pump can breach SEBI's rules on fraudulent and unfair trade practices.
- Reason (owner): Use every useful signal, learn from experienced traders' ideas, and protect against fake demand and supply.

## 2026-11-22 - Per-company news through Google News RSS
- Decision: company-fetch reads Google News RSS search feeds for every watchlist stock plus up to 30 recent filers (companies with matched exchange items in the last 24 hours), one request per 10 seconds, each company at most once an hour. Any block (403/429/503, an HTML page, or a consent or CAPTCHA redirect) stops all Google requests until the next IST day and sends one alert. Items are stored as company_news with the target symbol; a shared article gains extra symbols; match_confidence is low for one-word names or when the name isn't in the title or summary.
- Concern (planner): Google can block or change this feed at any time; it stays a best-effort source. Short names produce false matches, so Phase 2 checks low-confidence items. Articles aren't opened, so only headlines and summaries are kept.
- Reason (owner): Track each company's news, including companies that are filing today, not only the general market feeds.

## 2026-11-22 - Exchange item identity and BSE codes
- Decision: Exchange items are keyed by their own identity (the feed guid, or link + title + time), so filings that share one PDF are kept as separate events; existing items were migrated once. Instruments carry the BSE scrip code from Upstox's public BSE file (matched by ISIN), so BSE filings link to stocks exactly. A source with no items for 14 days raises a warning.
- Concern (planner): Some exchange feeds have no stable guid, so title + time stands in; a corrected filing may then appear as a second item.
- Reason (owner): Insider and promoter filings are separate events that matter as features.

## 2026-11-24 - End-to-end sprint by 27 November
- Decision: Build a thin version of every layer by 27 Nov in six tasks (S1 login and daily candles; S2 features, labels and leakage test; S3 baselines and per-sector LightGBM with walk-forward after costs, SHAP and MLflow; S4 predictions, audit, outcomes and paper trading; S5 FastAPI and Streamlit; S6 scheduler, end-to-end CI test, docs and tag). Intraday, extra macro, NSE files, the live feed, MCP, Phase 2 AI extraction, LSTM, online learning, manipulation flags, IPO, mutual funds and the agent come after 28 Nov.
- Concern (planner): Speed raises the risk of leakage and over-optimistic results; the demo shows an honest pipeline (model vs baselines after costs), not proof of profit.
- Reason (owner): A complete end-to-end system is needed for 28 November.

## 2026-11-24 - Upstox login and token
- Decision: A one-command browser login with a local callback (and a paste-the-address fallback) gets the daily access token, kept only in Windows Credential Manager on the main laptop; non-secret expiry metadata lives in a state file. An expired token raises a clear error, one alert, and a health warning. The approve-on-phone flow isn't used because it needs a public webhook.
- Concern (planner): No login on a given day means no Upstox data for that day; the yfinance backup and gap backfill cover it.
- Reason (owner): A simple, safe daily login.

## 2026-11-24 - Daily candles, backup and universe
- Decision: Daily candles come from the Upstox V3 historical API with yfinance as a stock-only backup (each row tagged; Upstox replaces yfinance for the same day, never the reverse). Gaps are tracked against an index-derived trading calendar and backfilled resumably. The sprint universe is all NSE indices plus the Nifty 500 from 2015. Suspected split/bonus jumps are recorded, not corrected, and S2 excludes them from labels.
- Concern (planner): Using today's Nifty 500 for history adds survivorship bias (past losers are missing), so results look better than reality; this must be stated in model cards. Raw prices aren't adjusted for corporate actions.
- Reason (owner): The fastest reliable data base for an end-to-end demo.

## 2026-11-24 - Timing convention, features v1 and labels
- Decision: Features for day t use only candles up to t's close; predictions are made at 08:30 IST on the next trading day and enter at its open; news counts only if available before that time. Features v1 (returns, volatility, trend, RSI, volume, 52-week position, gap, strength vs Nifty and sector, market and VIX, calendar, history and price-event flags) live in features_daily; labels (forward return from the next open to the close h trading days later, h = 21, 63, 126, plus up/down) live in a separate labels_daily table and are excluded around split/bonus events, gaps and special sessions. News features are stored but not used for training until enough history exists.
- Concern (planner): Survivorship bias from today's Nifty 500 and unadjusted prices remain; both must be stated in model cards.
- Reason (owner): Leakage-safe inputs for honest models.

## 2026-11-24 - Leakage test fails the build
- Decision: An automated leakage test (truncating future data, adding late news, a label-name check, and a deliberately leaky fake feature that must be caught) runs in CI and as a command on real data; any failure stops the build.
- Concern (planner): Tests can only catch the leakage patterns they check; every new feature must keep using the shared build function.
- Reason (owner): The spec requires a leakage test that fails the build.

## 2026-11-24 - Model evaluation design
- Decision: 21-day LightGBM classifier and regressor per sector (pooled fallback for small sectors), trained walk-forward with yearly folds, purge and a 21-day embargo, and a final 6-month holdout used once. Baselines: always-up, momentum, SMA rule, equal-weight universe and Nifty 50. The honest test is an after-cost portfolio of the top 20 picks every 21 trading days; "beats_baseline" requires beating the equal-weight universe and the best other baseline. Costs follow current official Indian charges plus liquidity-based slippage.
- Concern (planner): Survivorship bias and unadjusted prices inflate results; a single holdout of 6 months is short. No model becomes active without paper-trading results and the owner's approval.
- Reason (owner): Spec rules: walk-forward only, baselines, results after costs.

## 2026-11-24 - SHAP reasons and MLflow tracking
- Decision: SHAP TreeExplainer gives global importance per model and the top 5 plain-language reasons per prediction from tested templates. MLflow runs locally (sqlite + artifacts under the data folder, UI on 127.0.0.1 only). Models are versioned in model_versions with status candidate/active/retired and a model card each.
- Concern (planner): SHAP explains the model, not the market; reasons are "why the model thinks so", not proof.
- Reason (owner): Explain every prediction and version every model, as the spec requires.

## 2026-11-25 - New priority order and capital input
- Decision: Priority is now 1 intraday, 2 mutual funds, 3 long-term stocks, 4 IPO (replacing stocks -> IPO -> mutual funds). The sprint continues as S4i intraday data, S5i intraday strategies and backtest with capital-based sizing, S6 mutual funds, S7 dashboard, scheduler and end-to-end test. Every module takes the owner's capital amount through one shared sizing component (risk per trade for intraday, amount per stock with at least one share, fund splits with minimum amounts, IPO minimum lot).
- Concern (planner): Intraday is the hardest module to make profitable (SEBI: 71% of individual intraday traders in the cash segment lost money in FY2023); the system must be free to say "no trade". The long-term stock models currently don't beat their baselines after costs.
- Reason (owner): Intraday and mutual funds matter most to me now.

## 2026-11-25 - Intraday data, features and labels
- Decision: Nifty 50 stocks plus the Nifty 50, India VIX and matching sector indices, as 1-minute Upstox candles from Jan 2022, regular session only. Decisions every 5 minutes (09:30–15:00), features from closed bars only, entry at the next bar's open, everything closed by 15:20. Triple-barrier labels for long and short sides (default target 0.5%, stop 0.25%, 15 bars; stop first if both hit in one bar). Intraday features and labels are stored as Parquet under the data folder with a build record in Postgres; candles stay in Postgres.
- Concern (planner): Parquet for features departs from the spec's "features in Postgres" to keep a laptop fast; builds are recorded and reproducible. Fixed-percentage barriers ignore each stock's volatility; S5i may add volatility-scaled barriers.
- Reason (owner): Fast intraday research on a laptop.

## 2026-11-25 - Intraday strategies, backtest and sizing
- Decision: Rule strategies (opening-range breakout, VWAP trend and reversion, gap, volume burst, relative strength, mood and no-trade filters) are baselines for a LightGBM meta-model on triple-barrier labels (fixed v1 and volatility-scaled v2), evaluated with a monthly walk-forward and a 3-month holdout in an event-driven backtest after current intraday costs. Signals are trade plans sized from the owner's capital (1% risk per trade, 2% daily loss limit, max 5 trades by default); trades happen only when the expected value after costs is positive. A replay command demonstrates a past day.
- Concern (planner): Most intraday strategies lose after costs; "no trading" is a valid outcome. No live orders.
- Reason (owner): Intraday is priority 1.

## 2026-11-25 - Intraday longer hold, confidence calibration and percentage reporting
- Decision: Add longer-hold intraday labels (v3: ATR-scaled barriers with 30- and 60-bar limits
  and a hold-to-close variant; v1 and v2 kept as baselines), a confidence-calibration/precision
  report (probability deciles and the top 10/5/1% most-confident signals with their realised win
  rate and net profit after costs, per side; the holdout marked "one trade, not evidence"), and
  plain-percentage reporting that ranks every strategy and the model against "no trading" (0%)
  and shows, in the replay, the day's charges and gross move as percentages. No new instruments,
  no new data, no live orders, nothing promoted.
- Concern (planner): Nifty 50 large caps rarely move enough intraday to beat the ~0.1%
  round-trip charge, so a longer hold may still lose, and the calibration test may confirm the
  strongest "yes" are not profitable. These are measurement tools to be reused on a wider, more
  volatile universe (the next task). No live orders.
- Reason (owner): Test whether holding longer helps and whether a few very-confident signals are
  strong, and show "trade vs no-trade" as clear percentages.

## 2026-11-26 - Wider volatile intraday universe and a calibrated model
- Decision: Build a new intraday universe 'movers100' of about 100 NSE stocks chosen from the
  nifty500 by liquidity (median daily turnover, above a floor so ~5 bps slippage is realistic)
  and intraday volatility (median daily range), always including the owner's named picks
  (Hindustan Copper, Mazagon Dock, Aegis Logistics, Petronet LNG, HAL, Federal Bank, IDFC First
  Bank). Download their 1-minute candles from Jan 2022, build features (v1) and labels (v1, v2,
  v3), train the meta-model with its probabilities calibrated (isotonic on the walk-forward
  out-of-fold predictions) before the EV gate, and backtest every rule strategy and the model
  after costs, reporting each against no_trading in percentages plus the best per-trade gross
  edge at zero slippage against the ~8.3 bps cost floor. Jio Platforms (JPL, listed 28 Oct 2026)
  and Gold BeES are excluded. No live orders; nothing promoted.
- Concern (planner): More volatile stocks move more but also have wider spreads (more slippage),
  so the net after costs is uncertain. Picking the most volatile names in-sample can flatter the
  result; the monthly walk-forward and the untouched holdout guard against it, and no_trading
  stays the benchmark. On Nifty 50 the gross edge was ~1/15 of the fee; if it stays that small
  on volatile stocks, nothing will work here and intraday cash equity is finished.
- Reason (owner): Individual volatile stocks move far more than the Nifty 50/100 index, so they
  have a better chance of beating intraday costs; the system must still be free to say "no trade".
