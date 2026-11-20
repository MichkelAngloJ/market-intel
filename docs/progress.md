# Progress

Last updated: 2026-11-21, HEAD b5f2bbe. Updated at the end of every task (see
[CLAUDE.md](../CLAUDE.md)).

## Current

- Phase 0 (setup). Now: 0.9b done, awaiting planner review. Next: 0.10 (Phase 0 wrap-up and
  honest review), then Phase 1. 0.11 (remove the testing-laptop dependency) is planned for
  not before 2026-12-03.
- Before Phase 1: the owner opens an Upstox account (free API).
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
  - Not done yet: the `CLAUDE.md` Databases rule ("admin logins only for alembic, mongo_setup
    and backup/restore") waits for the owner's OK in the implementing session.
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
| 0.9b | done 2026-11-21 (HEAD b5f2bbe) | awaiting planner review | Local Docker databases on the main laptop (migrated, set up, backup with manifest and restore test); commit-email guard (hook + CI history test) |
| 0.10 | next | – | Phase 0 wrap-up and honest review |
| 0.11 | planned (not before 2026-12-03) | – | Remove testing-laptop dependency (hand-back checklist, cloud databases per the 0.10 decision, two-laptop docs) |

## State

- Main laptop (`full`): local Docker databases at migration 0003, MongoDB collections and
  validators set up; `db_check` OK; health overall OK (`mongo_rights_probe` OK, refusal code
  13). Backup with manifest and restore test OK (restored counts match the manifest, SHA-256
  values match); backups in `BACKUP_DIR`, outside the repo. The very first backup there was
  taken before manifests existed; it has no manifest and leaves with the 7-per-kind rotation.
- Testing laptop (`restricted`): unchanged, on the cloud databases (migration 0003, MongoDB
  collections set up).
- Commit-email guard active: the pre-commit hook blocks other emails; the CI test passes on
  the full history.
- CI green at b5f2bbe; compose-smoke last ran (green) at 7a00494.

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
- Main laptop: copy the backups (`BACKUP_DIR`) off the laptop regularly; once ingestion starts
  the local databases are the only copy of the data.
- Before Phase 1 ingestion: consider a health check for backup age.
- Re-run `backup --verify` after the first Phase 1 ingestion: so far the local databases are
  empty apart from `alembic_version`, so the restore test compared mostly zero counts.
