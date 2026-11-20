# Workflow

How the owner, the planner and Claude Code work together. What is built:
[spec](spec.md). Where it stands: [progress](progress.md).

## Who does what

As in the spec's [Roles](spec.md#roles):

- **Owner:** makes every final decision (tools, methods, scope, order); does all logins and
  edits `.env`.
- **Planner** (a Claude chat): plans and explains options, warns about risks, writes task
  prompts and reviews reports. Never writes project code.
- **Claude Code:** implements one task prompt per session and reports back.

## Shared memory is the repo

- Planner chats and Claude Code sessions on each laptop do NOT share memory.
- Sources of truth:
  - [docs/progress.md](progress.md): status
  - [docs/decisions.md](decisions.md): why
  - [docs/spec.md](spec.md): what
  - [docs/architecture.md](architecture.md) and [docs/runbook.md](runbook.md): how it works
    and how to run it
  - [CLAUDE.md](../CLAUDE.md): Claude Code's rules
- Every session starts with `git pull` and reading [docs/progress.md](progress.md).

## Steps per task

1. **Plan:** the planner breaks the phase into tasks, one Claude Code session each.
2. **Method:** options, trade-offs, a recommendation and risks. The owner confirms. Questions
   and a task prompt never come in the same reply.
3. **Prerequisites:** the planner finishes and VERIFIES every owner-side step first
   (accounts, logins, `.env` values, connectivity).
4. **Task prompt:** in the template below.
5. **Implement:** Claude Code implements it and stops at STOP points.
6. **Review:** the planner checks the report against every acceptance criterion and verifies
   in the repo, CI and databases itself (permissions by behaviour).
7. **Record:** the next task prompt starts by recording the review result in
   [progress.md](progress.md).

## Task prompt template

TASK, CONTEXT, GOAL, METHOD (confirmed by owner), FILES, CONSTRAINTS, ACCEPTANCE CRITERIA,
DO NOT, DOCS, DECISION LOG ENTRY, REPORT BACK.

## Report format

Files changed; tests run + results; docs changed; pragmas added; real emails sent; problems;
open questions.

## Decision entry format

`## date - title`, then three bullets: Decision / Concern (planner) / Reason (owner). Entries
are appended to [decisions.md](decisions.md); old entries are never edited.

## Planner rules

- Few questions per reply (2–3), each explained first with a recommended default.
- Question the owner's choices when there's a better option; warn clearly; never silently
  change a confirmed method.
- Verify fast-changing facts live (prices, free tiers, versions) and cite sources.
- Never ask for, paste or print secrets.
- Every task prompt has a DOCS step and updates [progress.md](progress.md).
- Add a short "Learn" note (concept + one exercise) when a task uses a skill on the learning
  list: PyTorch, anomaly detection, online learning, SHAP, MLflow, LoRA, MCP server,
  websockets, scheduling, FinBERT, Docker.

## Two laptops

- **Testing laptop:** profile `restricted`; cloud databases; no Docker; no broker keys.
- **Main laptop:** profile `full`; Docker; GPU; broker keys allowed.
- Pull first, push last; never work on both at once; each laptop has its own `.env`. Details
  in the [runbook](runbook.md#daily-routine).
