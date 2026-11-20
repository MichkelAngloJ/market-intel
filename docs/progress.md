# Progress

Last updated: 2026-11-21, HEAD 4eb9574. Updated at the end of every task (see
[CLAUDE.md](../CLAUDE.md)).

## Current

- Phase 0 (setup) is complete: 0.10 done, awaiting planner review; tag `phase-0`. Review:
  [docs/reviews/phase-0.md](reviews/phase-0.md).
- Next: Phase 1 planning by the planner, and the owner's Upstox account (owner-side, no code,
  no keys). 0.11 (remove the testing-laptop dependency) stays planned for not before
  2026-12-03.
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
| 0.10 | done 2026-11-21 (HEAD 4eb9574) | awaiting planner review | Phase 0 wrap-up: `backup_age` health check and `BACKUP_MAX_AGE_DAYS`; Phase 0 review; spec (cloud databases, AI access through MCP); tag `phase-0` |
| 0.11 | planned (not before 2026-12-03) | – | Remove testing-laptop dependency (hand-back checklist, cloud databases per the 0.10 decision, two-laptop docs) |

## State

- Main laptop (`full`): local Docker databases at migration 0003, MongoDB collections and
  validators set up; `db_check` OK; health overall OK (9 OK, 4 SKIP; `mongo_rights_probe` OK,
  refusal code 13; `backup_age` OK after a fresh backup). Backup with manifest and restore test OK (restored counts match the manifest, SHA-256
  values match); backups in `BACKUP_DIR`, outside the repo. The very first backup there was
  taken before manifests existed; it has no manifest and leaves with the 7-per-kind rotation.
- Testing laptop (`restricted`): unchanged, on the cloud databases (migration 0003, MongoDB
  collections set up).
- Commit-email guard active: the pre-commit hook blocks other emails; the CI test passes on
  the full history.
- CI and compose-smoke green at 7c7f966 (all 0.10 code; compose-smoke shows `backup_age`
  WARN there, as expected with no backup folder). The final commit's CI result is in the 0.10
  report.

## Carry-forward items

- Before Phase 1 runs several processes: one log file per component, and alerts sent in the
  background (Windows log-rotation lock).
- Phase 2: decide when the `llm` settings group counts as complete (lean: Gemini OR Groq).
- Phase 3 start: Colab's newest runtime uses Python 3.13 while the project pins 3.12; decide
  whether to allow both.
- MongoDB: new fields need `mongo_setup` first; each new collection needs an app-role row in
  the provider console. Refusal codes: 8000 (cloud) and 13 (Docker).
- MongoDB on Docker: the init script creates the app role only on an empty volume, so a new
  collection also needs its role row added in the running local database. The runbook's
  "Adding a MongoDB collection" checklist doesn't say how yet; settle it with the first new
  collection.
- The MongoDB provider accepts only known networks: add each new network's IP.
- Before the testing laptop is handed back: log out of GitHub CLI, revoke its token, delete
  its `.env`, remove its network from the MongoDB access list.
- Main laptop: quit Docker Desktop when not using local databases (it holds 6 GB).
- Main laptop: back up after work days and copy `BACKUP_DIR` to external storage weekly; once
  ingestion starts and the cloud databases are deleted (0.11), these are the only copies.
- Before AI first queries project data (Phase 2 at the latest): plan the MCP connection
  task (free, read-only login, local only).
- Re-run `backup --verify` after the first Phase 1 ingestion: so far the local databases are
  empty apart from `alembic_version`, so the restore test compared mostly zero counts.
