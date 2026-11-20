# Progress

Last updated: 2026-11-20, HEAD 1dfb7ce. Updated at the end of every task (see
[CLAUDE.md](../CLAUDE.md)).

## Current

- Phase 0 (setup). Now: 0.9a done, awaiting planner review. Next: 0.9b (local Docker
  databases decision + commit-email guard). Then 0.10 (Phase 0 wrap-up and honest review), then Phase 1.
- Before Phase 1: the owner opens an Upstox account (free API).

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
| 0.9a | done 2026-11-20 (HEAD 1dfb7ce) | awaiting planner review | Shared tracking files; phase titles; main laptop setup in the runbook |
| 0.9b | next | – | Local Docker databases decision; commit-email guard |
| 0.10 | planned | – | Phase 0 wrap-up and honest review |

## State

- Postgres at migration 0003; MongoDB collections set up.
- Health OK on both profiles (`restricted`, `full`).
- CI and compose-smoke green.

## Carry-forward items

- Before Phase 1 runs several processes: one log file per component, and alerts sent in the
  background (Windows log-rotation lock).
- Phase 2: decide when the `llm` settings group counts as complete (lean: Gemini OR Groq).
- Phase 3 start: Colab's newest runtime uses Python 3.13 while the project pins 3.12; decide
  whether to allow both.
- MongoDB: new fields need `mongo_setup` first; each new collection needs an app-role row in
  the provider console. Refusal codes: 8000 (cloud) and 13 (Docker).
- The MongoDB provider accepts only known networks: add each new network's IP.
- Before the testing laptop is handed back: log out of GitHub CLI, revoke its token, delete
  its `.env`, remove its network from the MongoDB access list.
- Main laptop: quit Docker Desktop when not using local databases (it holds 6 GB).
