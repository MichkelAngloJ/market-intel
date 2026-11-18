# Decision Log

## 2026-11-12 - Workspace and sync setup
- The project folder is a standalone git repo (branch `main`), separate from any other repo on the machine.
- Code syncs through a personal GitHub remote. Only `drive_sync/` syncs to Google Drive (it's git-ignored), so Drive never touches `.git`.
- `.env` is synced by neither; copy it by hand when moving to the HP Victus.

## 2026-11-12 - Machines and phases
- Decision: The ThinkPad (no admin, no WSL, no NVIDIA GPU) is for testing and validation until end of Nov 2026. HP Victus becomes the main machine from Dec 2026. Go live only after validation.
- Concern (planner): Not my own device; personal project files and credentials on it.
- Reason (owner): Only machine available until December.

## 2026-11-12 - No real Upstox keys on the ThinkPad
- Decision: Real broker credentials are used only on the HP.
- Concern (planner): Files on a device I don't fully control may be readable by others.
- Reason (owner): Accepted planner default.

## 2026-11-12 - Google Drive not used (updates "Workspace and sync setup")
- Decision: drive_sync/ stays a local, git-ignored folder. Git is the backup for code.
- Concern (planner): No off-machine backup for exports on the ThinkPad.
- Reason (owner): Google Drive can't be installed on the ThinkPad; market data can be re-downloaded.

## 2026-11-12 - Databases during testing: free cloud
- Decision: MongoDB Atlas free tier plus a free Postgres host (chosen in task 0.4) on the ThinkPad. Docker Compose on the HP from December.
- Concern (planner): Small free limits (~0.5 GB each); data and connection strings held by third parties; differs from the final Docker setup. SQLyog only works with MySQL, not Postgres.
- Reason (owner): No admin/WSL, so no Docker. Cloud data can be reached from any laptop and removed later.

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
- Decision: pydantic-settings. APP_PROFILE = thinkpad | hp | cloud. Defaults live in code; each machine's .env sets only what differs. Settings are grouped and each group is checked when that part starts. Secrets are SecretStr. A command prints settings with secrets masked.
- Concern (planner): Per-machine settings files would be easier to read, but aren't worth maintaining while machines differ little.
- Reason (owner): Accepted the planner's recommendation.

## 2026-11-12 - Decisions enforced in code
- Decision: The thinkpad profile refuses to start if Upstox keys are present. LLM cap defaults to Rs 1,000; above that needs LLM_CAP_RAISE_APPROVED=true; Rs 2,000 is the absolute maximum; warning at 80%.
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
- Decision: A separate Gmail used only for sending alerts (2-Step Verification + app password in .env). Alerts arrive in my main inbox. Gmail SMTP on port 587 with STARTTLS.
- Concern (planner): An app password gives full access to that Gmail and sits on the ThinkPad; the risk is limited by using a dedicated empty account. Rejected: Gmail OAuth send-only (personal apps must re-authorise every 7 days) and Brevo/Resend (need an owned domain for reliable delivery).
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
- Decision: Neon free plan, Postgres 18, AWS Singapore, Neon's default database "neondb", direct connection with TLS. The HP's local database will be named market_intel; the connection URL carries the name, so code doesn't care.
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

## 2026-11-13 - Docker Compose for the HP, tested in CI
- Decision: docker/compose.yaml with postgres 18 and mongo 8.0 (same as the cloud), ports on 127.0.0.1 only, memory limits, health checks, multi-arch images. Credentials in the git-ignored docker/compose.env. Least-privilege app user. A CI job starts the stack and runs db_check whenever database files change.
- Concern (planner): Uses CI minutes on a private repo, so the job runs only on relevant changes. The TimescaleDB image decision is left to task 0.5.
- Reason (owner): Verify the HP setup now, without Docker on the ThinkPad.

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
- Decision: Alembic with SQLAlchemy 2 Core table definitions. Migrations live in migrations/ and run by hand with "alembic upgrade head" using the owner login (POSTGRES_ADMIN_URL), never automatically. CI proves every migration can be applied, undone and re-applied, and that code and migrations match (alembic check).
- Concern (planner): Downgrades delete tables and data, so they run only in CI, never on Neon or HP data.
- Reason (owner): Accepted the planner's recommendation.

## 2026-11-18 - Plain Postgres, no TimescaleDB for now
- Decision: Plain Postgres 18 with composite primary keys and BRIN time indexes. Tables are designed so they could become TimescaleDB hypertables later.
- Concern (planner): Neon offers only TimescaleDB's Apache-2 features (no compression), so using it would make Neon and the HP behave differently. Revisit only if volumes demand it. This revises the 0.4 reason for picking Neon, which mentioned TimescaleDB.
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
- Decision: The refusal of unencrypted connections to non-local hosts moved into the connection functions, so every caller is covered. Single-word hostnames count as local only on the hp and cloud profiles.
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
