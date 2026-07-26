# CLAUDE.md
## Shift-Left Infrastructure Provisioning Portal — working rules

Read this file, then `ARCHITECTURE.md`, then `PLAN.md` at the start of every session before doing anything else.

## About me
I lead infrastructure but I am new to writing software and to git. Explain every step in plain language, avoid unexplained jargon, and **always tell me how to run what you built and exactly what a working result looks like** so I can verify it myself.

## The three documents that govern this project
- **CLAUDE.md** (this file) — how we work together. The standing rules.
- **ARCHITECTURE.md** — the design authority: what we're building, the principles, the feature catalogue (feature IDs), the technology decisions. You must conform to it.
- **PLAN.md** — the build sequence: the increments, in order, each with an acceptance check.

If any two of these conflict, stop and tell me before coding.

## How we work
Build **one increment at a time, in the order PLAN.md gives.** For each:
1. Show me a short plan for *just that increment* and wait for my "go".
2. Build only that increment — nothing from later ones.
3. Tell me, in plain steps, how to run it and what I should see if it worked.
4. Run its automated tests.
5. Commit to git with a clear message naming the increment and the feature IDs it implements.
6. **Stop** and wait for my approval before the next increment.

Do not skip ahead, and do not write application code before I've approved ARCHITECTURE.md and PLAN.md.

## Hard rules (non-negotiable)
- **Separation of authority:** the portal and API *collect and validate*; Jira *holds the approval*; the orchestrator *executes* — and only after re-verifying the approval. Never let one layer take another's job. (ARCHITECTURE.md §4)
- **The client holds no authority:** all validation, sizing, and pricing are re-computed server-side and are authoritative. The browser holds no credentials and no pricing logic.
- **The AI agent recommends; it never decides or executes.** It must never approve a request, hold provisioning credentials, or trigger the orchestrator.
- **Nothing real gets provisioned without me.** Build and demo in mock mode (`USE_MOCK=true`) by default.
- **Everything privileged is audited** to the append-only, tamper-evident log.
- **Secrets never go in code or git.** Use `.env` locally (git-ignored) and a vault in production. If you ever need a real credential, ask me to add it myself — don't put it in a file you commit.
- **Favour simple, mainstream, well-documented technology** over clever choices.
- If something I ask for conflicts with ARCHITECTURE.md, tell me before doing it.

## The stack (from ARCHITECTURE.md §9 — don't change without recording why)
Python 3.12 · FastAPI + Jinja + HTMX portal · FastAPI API · PostgreSQL 16 · OPA for policy · Jira Service Management for approvals · a durable workflow engine driving Terraform/Ansible/GitOps.

## Git (I'm new to this — keep it simple)
Commit after every working increment, one increment per commit, with a message like `feat(1.3): guided request + saved drafts [F-UX-01, F-UX-10]`. Never commit `.env` or any secret. If a commit would include something sensitive, stop and tell me.

## When unsure
Stop and ask me in plain language rather than guessing. A one-sentence question now is cheaper than undoing a wrong assumption later.
