# PLAN.md
## Shift-Left Infrastructure Provisioning Portal — Build Plan

**Owner:** Infrastructure Management Department (IMD)
**Status:** Draft v1 (July 2026)
**Governed by:** ARCHITECTURE.md (the architect's authority). This plan sequences *how* it gets built; it must not contradict it.
**Assumed stack:** FastAPI + Jinja + HTMX portal, FastAPI API, PostgreSQL, OPA, Jira, durable orchestrator (per ARCHITECTURE.md §9). See §5 for the one place the plan changes if the portal is React instead.

---

## 0. How Claude Code must use this plan

1. Build **one increment at a time, in order.** Do not start the next increment until the reviewer has run the current one and approved it.
2. Before each increment: show a short plan for *just that increment* and wait for "go".
3. After each increment: (a) tell the reviewer in plain steps how to run it and exactly what a working result looks like, (b) run the increment's tests, (c) commit to git with a clear message, then **stop**.
4. Every increment lists the ARCHITECTURE.md feature IDs it implements — reference them in the commit message so code, plan, and backlog stay linked.
5. If an increment turns out to need something not in ARCHITECTURE.md, stop and flag it before coding.

---

## 1. How to read this plan (for the reviewer)

Each increment is **one small thing you can run and see**. That is deliberate: you never have to judge a giant pile of code at once. For every increment there is a plain-language "**You'll know it works when…**" — that line is your approval test. If you can see that result, approve and move on. If you can't, say so and we fix it before going further.

Phase 0 and Phase 1 (the baseline spine) are planned in fine detail because you build them first. The enterprise increments E1–E4 are listed at increment-batch level; each will be decomposed into small steps like these *when we reach it*, so we don't over-plan work that's months away.

**Definition of done — applies to every increment:**
- It runs locally with one command and shows the expected result.
- Server-side validation is authoritative; the browser holds no credentials or pricing logic (ARCHITECTURE.md P2).
- It has automated tests for its main behaviour and the obvious ways it could break.
- It's committed to git with a message naming the increment and its feature IDs.

---

## 2. Phase 0 — Walking skeleton (before any features)

The goal here is not features — it's a running, empty shell you can build everything else onto.

**0.1 — Project skeleton**
Create the repo structure (portal, api, agents, db, orchestrator folders), a Python 3.12 environment, and a FastAPI app with a single `/health` endpoint. Add the `USE_MOCK` master switch (ARCHITECTURE.md §10 NFR).
*You'll know it works when:* you open `http://localhost:8081/health` and see `{"ok": true, "mock": true}`.
*Note:* port 8080 is already in use on the reviewer's machine by Docker Desktop's WSL2 backend (unrelated to this project), so this app uses 8081 instead. Not a stack change — just an available port.

**0.2 — Postgres + one-command run**
Add Docker Compose that starts the API and a PostgreSQL 16 container together, creating the database automatically.
*You'll know it works when:* `docker compose up` starts everything, and `/health` still responds.

**0.3 — First HTMX page**
Serve one Jinja page with HTMX wired in, showing a "Portal is running" panel that fetches a live value from the API.
*You'll know it works when:* you open `http://localhost:5173`, see the page, and the panel shows live data from the API (not hard-coded).

---

## 3. Phase 1 — Baseline request-to-approval spine

This is the core product from ARCHITECTURE.md §1 and §6: collect → validate → cost → approve → hand off. Build it end to end in mock mode first (no real Jira/cloud accounts needed), then wire real integrations later, one adapter at a time.

**1.1 — Database schema + seed**
Create the tables from ARCHITECTURE.md §5 and seed lookups, sizing anchors, and rate cards.
*Implements:* reference-data foundation.
*You'll know it works when:* a seed query returns the projects, cost centres, technologies, and rate cards.

**1.2 — Lookups API + form scaffolding**
Expose `/api/lookups` and render the guided-request page with real dropdowns (projects, cost centres, technologies, environments).
*Implements:* guided intake foundation.
*You'll know it works when:* the request form loads and its dropdowns are populated from the database, not placeholder text.

**1.3 — Guided request + saved drafts + server-side validation**
Build the multi-step request form for the four request types, with save-and-resume and authoritative server-side validation that returns readable, field-level errors.
*Implements:* F-UX-01 (saved drafts), F-UX-10 (guided error remediation), four request types.
*You'll know it works when:* you can start a request, save it half-finished, come back to it, and a bad entry is rejected with a message that tells you the rule.

**1.4 — Automatic sizing**
Resolve CPU / memory / storage from the per-technology, per-size anchors.
*Implements:* automatic sizing; F-CAT-07 (versioned anchors) foundation.
*You'll know it works when:* choosing a technology and size shows the resolved CPU/RAM/storage on screen.

**1.5 — Cost estimation + live cost panel**
Compute one-time, monthly, and annual cost (incl. licence lines) server-side, and show a live cost panel that updates as selections change. Mock pricing first, behind the pricing service.
*Implements:* cost estimation, multi-source pricing, cost-before-approval.
*You'll know it works when:* changing the size or stack updates the cost panel live, and the numbers come from the server, not the browser.

**1.6 — Persist request + estimate**
Save the submitted request and its server-computed estimate; show a confirmation with a reference.
*Implements:* request + estimate persistence.
*You'll know it works when:* after submitting, the request and its cost are stored and you can see it saved.

**1.7 — Policy gate (OPA)**
Add the OPA policy gate that runs at request time — allowed regions, SKUs, naming, tagging, residency — returning human-readable pass/fail reasons.
*Implements:* F-GOV-03 (policy-as-code), F-CAT-04/05/06 foundations.
*You'll know it works when:* a request that breaks a policy (e.g. a disallowed region) is blocked with a plain-language reason, and a compliant one passes.

**1.8 — Jira ticket with config + cost + plan preview**
Raise a Jira ticket carrying configuration, cost, and a (mock) plan preview, under the requester. Mock Jira adapter first.
*Implements:* Jira workflow, cost-before-approval, F-ORC-02 (plan preview).
*You'll know it works when:* submitting an approved-shape request returns a (mock) ticket key, and the ticket body shows config + cost together.

**1.9 — Approval webhook + signed orchestrator handoff**
On approval, verify the HMAC-signed webhook, re-verify approval and re-check OPA, then call a mock orchestrator. Nothing real is provisioned yet.
*Implements:* governed execution trigger; ARCHITECTURE.md §4 authority separation.
*You'll know it works when:* approving a request fires the handoff, the orchestrator re-verifies approval before acting, and you see the mock "provisioned" result plus an audit entry.

**End of Phase 1:** you have a working, demonstrable request-to-approval spine in mock mode. This is the point to demo to stakeholders before adding enterprise depth.

---

## 4. Phase 2 — Real integrations (turn off the mocks, one at a time)

With the spine proven, switch `USE_MOCK=false` and enable each adapter individually so a failure is easy to isolate: Active Directory / OIDC, Azure pricing, OCI pricing, Jira, then the real orchestrator contract. Each is its own small increment with its own "you'll know it works when" check. Keep on-prem pricing in the database (no code change).

---

## 4a. Phase UX — Premium portal redesign (React + IBM Carbon) — decided 2026-07-29

Per the reviewer's UX brief (*"UX Portal Design.docx"*) and ARCHITECTURE.md §14.1, the portal front end moves from HTMX+Jinja to a **React (Vite + TypeScript) SPA on IBM Carbon**, served via a **FastAPI Backend-for-Frontend** that keeps Entra OIDC + token handling server-side. **The API, RBAC, OPA, Jira, orchestrator, cost engine, poller and audit are unchanged** — this phase only replaces the presentation layer. Migration was incremental (screen by screen); **cutover completed at UX.5 (2026-07-29)** — React is now the portal on port 5173, with the HTMX portal parked as a demo fallback.

- **UX.1 — Design-system foundation & app shell.** Vite+React+TS app + Carbon; the BFF (serves the SPA, does OIDC, proxies `/api/*`); the enterprise shell (top nav, left nav, dark/light, WCAG 2.2). *You'll know it works when:* you sign in and see the empty Carbon shell calling the real API (e.g. `/api/me` shows your roles), running alongside the old portal.
- **UX.2 — Request form** (reskin today's guided form: request types, subsidiary, technology/size cards, live cost panel, validation, submit).
- **UX.3 — My Requests** (table, status badges, workflow drawer, live auto-update, drill-down).
- **UX.4 — Estate overview** (KPIs + charts, live refresh, drill-down).
- **UX.5 — Cutover** ✅ *(done 2026-07-29)*. The React portal became the front door: the `webapp` (BFF) now answers on host port **5173** — the classic portal's old port — reusing the Entra redirect URI already registered for 5173, so no new registration was needed. The HTMX portal is **parked as a demo fallback** under a docker-compose `fallback` profile (kept, not deleted; a plain `docker compose up` no longer starts it). Live sign-in verified on 5173 (`/login` → Microsoft Entra → `…/5173/auth/callback`). The classic portal's manual approve/apply/destroy buttons were **not** ported — they were mock/demo affordances; the live flow approves in Jira and provisions via the autonomous poller. If manual controls are wanted in the new UI later, that's a separate increment.

Each UX increment keeps every existing control/validation/integration and its backend tests; new UI gets component tests.

### UX brief — full traceability (`UX Portal Design.docx`)

Every section of the brief is mapped here so nothing is dropped (traceability pass, 2026-07-29). The reskin (UX.1–UX.5) delivered the **look-and-feel**; the added **breadth and features** land as backend/catalog increments sequenced *after* the reskin — pointers name the target phase. Legend: **✅ done · ◐ partial · ⬜ planned (not started) · ⛔ deliberate deviation.**

**Platform & design language**
- ⛔ *"Built on Backstage.io"* — **declined**; React + IBM Carbon via a BFF instead (ARCHITECTURE.md §14.1). The brief's design *language* is still followed.
- ✅ Premium enterprise look: sharp edges, blue/white, large type, minimal palette, Carbon icons, **dark/light**, WCAG 2.2, enterprise data tables, no rounded buttons.
- ◐ Per-breakpoint **mobile/tablet** optimisation (Carbon grid is responsive but not yet audited); animation/pixel polish — ongoing.

**Page layout panels** — ✅ top nav · left nav · main form · validation messages. ◐ live cost panel (totals now; breakdown → E3). ⬜ environment-summary panel · approval-summary panel · multi-step wizard/progress · sticky submit → *Portal UI polish*. ⬜ AI Assistant panel → **E4**.

**Header** — ✅ logo · app name · notifications *icon* · user avatar · help *icon*. ⬜ **Search**, working notifications (→E2), profile page, AI assistant (→E4).

**Requester Information (~30 fields, auto from Entra)**
- ✅ email · roles · cost centre · project code (on the form today).
- ⬜ identity-derived (name, employee ID, business unit, department, designation, phone, manager name/email, country, location, time zone, organization, division, company, auth method, privilege level) → **requester profile enrichment via Microsoft Graph (E1)**.
- ✅ owner/metadata: business justification, priority, business criticality, required delivery date, and the four owner roles — **delivered in increment 6.1** (validated, stored, shown in the Jira ticket + costing PDF + request detail). ⬜ request date auto-stamp is a trivial follow-up.

**Request Type (10)** — ✅ Create · Add Component · Decommission; ◐ Increase Capacity (=resize) · Remove Component (=decommission-by-reference). ⬜ Reduce Capacity · Clone · Disaster Recovery · Sandbox · Temporary → **catalog expansion**.

**Target Platform (7)** — ✅ Azure · OCI · On-Premises. ⬜ AWS · Google Cloud · Hybrid · Multi-Cloud → **multi-cloud phase** (one pricing/provisioning adapter each; large).

**Target Environment (7: Dev/Test/SIT/UAT/PreProd/Prod/DR)** — ✅ **delivered in 6.2**: the full tier ladder is a required-on-create field (`environment_tier`), validated + shown in the ticket/PDF/detail.

**Technology Stack (~24, cards w/ icons)** — ✅ **21 in the catalog after 6.2** (added Oracle DB, SQL Server, MongoDB, Kafka, RabbitMQ, Elastic/OpenSearch, Java, .NET, Node.js, Python, Apache, OpenShift, Vault, Keycloak; Oracle/SQL Server carry licences). ⬜ Istio/Service Mesh, Monitoring, Logging, Backup (platform-service add-ons) → later catalog. ✅ **card-with-icon presentation delivered in 6.3** (generic Carbon category icons, not brand logos). *(Catalog entry is cheap; real per-technology provisioning is the deferred heavy work — an apply still creates a placeholder resource.)*

**Environment Size (5, cards w/ specs)** — ✅ Small/Medium/Large **+ XLarge (6.2)**, server-side sizing anchors + auto-pricing. ✅ **spec cards delivered in 6.3** (each size card shows vCPU · RAM · storage). ⬜ Custom (user-typed resources) → catalog. ⬜ per-card HA / recommended-use-case hints → later polish.

**Advanced Options (~20)** — ✅ **delivered in 6.5** (all 20 in an expandable accordion, stored as an `advanced_options` JSON bag, validated). HA / backup retention / monitoring level / support tier **drive real cost**; region, AZ, DB version, encryption, DR, logging level, storage tier, autoscaling, network type, firewall profile, private/public endpoint, DNS, certificates, secrets management, compliance profile are validated capture-only. *(None provision anything real yet — the deferred technology-to-real-resource boundary.)*

**Live Cost Panel** — ✅ one-time/monthly/annual totals, live update, **compute/storage/licence split (6.4)**, **+ backup/monitoring/support lines driven by advanced options (6.5)**. ⬜ network (usage-based, not estimated); ⬜ savings recommendation + optimization tips → **AI Copilot (E4)**.

**AI Assistant (8 capabilities)** — ⬜ all (recommend sizing/cloud, explain costs, detect gaps, predict time, compliance) → **E4**, **recommend-only per hard-rule P3** (never decides, executes, or holds credentials).

**Bottom Actions** — ✅ Save Draft · Submit · (Cancel trivial) · **Generate Cost Sheet — PDF and Excel (6.4)**. ⬜ Validate Request · Preview Environment → small increments.

**Submission Flow** — ✅ validate fields · calculate pricing · **create Jira ticket** · **attach costing PDF + Excel to the ticket (6.4)** · plan-preview/blueprint in the ticket body · display tracking ID. ⬜ assign approvers · send approval notification → **E2**.

**Design-process outputs (Figma mockup, wireframe, component hierarchy, palette, typography, icons, journey, a11y, responsive, mobile/tablet/desktop)** — ✅ satisfied by *building on Carbon* (its design system, palette, typography, icon set, accessibility and responsive grid) instead of separate Figma artefacts. ⬜ standalone mockup/wireframe/user-journey documents were not produced (we built the working UI) — revisit only if a formal design sign-off deliverable is required.

**Portal UI polish (small, newly tracked here)** — ✅ **technology & size cards** with icons/specs (6.3). ⬜ header **Search**; on-form **Environment** & **Approval** summary panels; **multi-step wizard** progress; **sticky submit** bar. Low effort; not tied to a backend phase.

> Roll-up to enterprise phases: requester enrichment → **E1** · approvers/notifications/SLA → **E2** · cost breakdown + Excel → **E3** · AI Copilot → **E4** · catalog/advanced-options + multi-cloud → **catalog/multi-cloud increments** · Validate/Preview + UI polish → **small increments**.

---

## 5. Phase 3+ — Enterprise increments (E1–E4)

Sequenced per ARCHITECTURE.md §12 — **build what's hard to retrofit first.** Listed here at batch level; each will be decomposed into Phase-1-style small increments when we reach it.

- **E1 — Enterprise foundations:** F-IAM-01 (RBAC — roles built, Jira-group-driven; live enforcement awaits the admin's group→role map) **· ✅ F-IAM-03 segregation of duties delivered (E1.2): a user can't approve/apply/destroy a request they raised; `SOD_ENFORCED` default on; poller exempt · ✅ F-SEC-01 tamper-evident audit delivered: `verify_chain` + `GET /api/audit/verify` + `python -m api.audit verify` CLI (concurrency-tolerant: detects edits/deletions, tolerates benign poller/API forks), optional keyed HMAC (`AUDIT_HMAC_KEY`)**. Remaining: F-IAM-09 (group ownership), F-OPS-09 (admin console), F-INT-01 (public API), F-OPS-04 (tracing), F-OPS-01 (HA), F-SEC-09 (hardening). **Plus, per ARCHITECTURE.md §12 notes:** make the orchestrator handoff idempotent/resumable (F-ORC-01/03/04) and add IaC scanning (F-SEC-03/04) here — both are foundational.
- **E2 — Governance depth:** F-GOV-03/01/02/06/05/08/10 (policy-as-code, SLA/escalation, quorum, change windows, waivers, four-eyes, evidence pack), F-ORC-02 (plan preview, if not already in 1.8).
- **E3 — FinOps & lifecycle:** F-FIN-07 (TTL & renewal), F-FIN-06 (auto-shutdown), F-FIN-01/02/03 (variance, budget guardrails, showback), F-FIN-08 (quotas), F-LCM-08/09/10 (health, drift, ownership), F-LCM-03/06 (refresh, restore), F-IAM-07 + F-INT-05 (JIT access + vault delivery).
- **E4 — Intelligence & ecosystem:** F-RPT-06/07/08 (AI drafting, explanation, failure triage — inside approved templates/runbooks only, ARCHITECTURE.md §7), F-INT-02/08/09 (event stream, chat bot, CLI), F-FIN-12/09/13, F-RPT-05/10.

**Priority within all of this:** land the fifteen highest-impact features (ARCHITECTURE.md §11) ahead of the wider catalogue, then sequence the rest by measured evidence.

---

## 6. The one open decision that affects this plan now

**HTMX vs React for the portal (ARCHITECTURE.md §14.1).** This plan assumes HTMX. If you choose React instead, only the *portal* increments change shape — 0.3, 1.2, 1.3, and 1.5 would build a React app calling the same API — while the API, database, policy, Jira, orchestrator, and every enterprise increment stay identical. So the decision is real but low-blast-radius; it doesn't block starting Phase 0, which is stack-neutral either way.

Everything else in ARCHITECTURE.md §14 (control frameworks, sovereign mode, Arabic-at-launch, budget source, CMDB sync, etc.) can be settled before its own increment and does not block Phase 0 or Phase 1.

---

## 7. Your first three prompts to Claude Code

To start, point Claude Code at the folder containing `CLAUDE.md`, `ARCHITECTURE.md`, and this `PLAN.md`, then:

1. *"Read CLAUDE.md, ARCHITECTURE.md and PLAN.md. Confirm you understand the phased approach and do not write code yet. Then show me your plan for Phase 0, increment 0.1 only, and wait for my go."*
2. After you approve and it builds 0.1: *"Good. Show me how to run it and what I should see. Then commit, and stop."*
3. Then: *"Plan increment 0.2 only, and wait for my go."*

Repeat that rhythm — plan one increment, approve, build, run, commit, stop — all the way through. That loop is what keeps you in control of a build you can verify at every step.
