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
