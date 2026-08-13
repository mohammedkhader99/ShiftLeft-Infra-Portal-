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

Every section of the brief is mapped here so nothing is dropped (traceability pass, 2026-07-29; **status refreshed 2026-08-03**). The reskin (UX.1–UX.5) delivered the **look-and-feel**; the added **breadth and features** land as backend/catalog increments sequenced *after* the reskin — pointers name the target phase. Legend: **✅ done · ◐ partial · ⬜ planned (not started) · ⛔ deliberate deviation.**

> **Where the live roadmap lives.** This section tracks the UX brief. The current
> build sequence — turning the portal from "records provisioning intent" into
> self-service infrastructure — is in **[GAP-ANALYSIS.md](GAP-ANALYSIS.md) §7**,
> with a progress log in §10. Read that for what is being built now; read this for
> whether the brief has been covered.

**Platform & design language**
- ⛔ *"Built on Backstage.io"* — **declined**; React + IBM Carbon via a BFF instead (ARCHITECTURE.md §14.1). The brief's design *language* is still followed.
- ✅ Premium enterprise look: sharp edges, blue/white, large type, minimal palette, Carbon icons, **dark/light**, WCAG 2.2, enterprise data tables, no rounded buttons.
- ◐ Per-breakpoint **mobile/tablet** optimisation (Carbon grid is responsive but not yet audited); animation/pixel polish — ongoing.

**Page layout panels** — ✅ top nav · left nav · main form · validation messages · live cost panel **with breakdown (6.4/6.5)** · **environment-summary panel · approval-summary panel · multi-step wizard progress · sticky submit** (all delivered in the Portal UI polish increments) · **AI Assistant panel** (Draft with AI + Recommend cloud & size, E4).

**Header** — ✅ logo · app name · notifications *icon* · user avatar · help *icon* · **Search** (delivered). ✅ AI assistant — its own **Assistant** page (natural-language approvals + portal help). ⬜ working notifications (→E2), profile page.

**Requester Information (~30 fields, auto from Entra)**
- ✅ email · roles · cost centre · project code (on the form today).
- ⬜ identity-derived (name, employee ID, business unit, department, designation, phone, manager name/email, country, location, time zone, organization, division, company, auth method, privilege level) → **requester profile enrichment via Microsoft Graph (E1)**.
- ✅ owner/metadata: business justification, priority, business criticality, required delivery date, and the four owner roles — **delivered in increment 6.1** (validated, stored, shown in the Jira ticket + costing PDF + request detail). ⬜ request date auto-stamp is a trivial follow-up.

**Request Type (10)** — ✅ Create · Add Component · Decommission · **Reduce Capacity · Clone · Disaster Recovery · Sandbox · Temporary** (catalog expansion, delivered) · Refresh · Restore · **DNS** (new, GAP-ANALYSIS step 5). ◐ Increase Capacity (=resize) · Remove Component (=decommission-by-reference).

**Target Platform (7)** — ✅ Azure · OCI · On-Premises · **AWS · Google Cloud** (all five selectable and **priced**; the catalogue is target-aware). ◐ Real *provisioning* exists for OCI (bucket, compute VM, managed PostgreSQL) and AWS (S3, gated + unverified); Azure/GCP provisioning **deferred by the customer**. ⬜ Hybrid · Multi-Cloud.

**Target Environment (7: Dev/Test/SIT/UAT/PreProd/Prod/DR)** — ✅ **delivered in 6.2**: the full tier ladder is a required-on-create field (`environment_tier`), validated + shown in the ticket/PDF/detail.

**Technology Stack (~24, cards w/ icons)** — ✅ **21 in the catalog after 6.2** (added Oracle DB, SQL Server, MongoDB, Kafka, RabbitMQ, Elastic/OpenSearch, Java, .NET, Node.js, Python, Apache, OpenShift, Vault, Keycloak; Oracle/SQL Server carry licences). ✅ **Istio/Service Mesh, Monitoring, Logging, Backup add-ons delivered** — the catalogue is now **46 technologies** including cloud-native services (RDS, S3, EKS, Lambda, Azure SQL, OCI ADB, GCP CloudSQL…), each scoped to the targets it runs on. ✅ **card-with-icon presentation delivered in 6.3**. *(Catalogue entry is cheap; real per-technology provisioning is the heavy work. **4 of 46 carry a certified blueprint** today — Apache, compute VM, object storage and managed PostgreSQL, all on OCI. Recipes also ship for RHEL 9, Windows 2019, AWS S3, nginx and Redis but are **not certified**, so the portal does not offer them: shipping a recipe is not evidence it works. Every uncertified entry is honestly badged **manual** in the form — see GAP-ANALYSIS step 1.)*

**Environment Size (5, cards w/ specs)** — ✅ Small/Medium/Large **+ XLarge (6.2)**, server-side sizing anchors + auto-pricing. ✅ **spec cards delivered in 6.3** (each size card shows vCPU · RAM · storage). ⬜ Custom (user-typed resources) → catalog. ⬜ per-card HA / recommended-use-case hints → later polish.

**Advanced Options (~20)** — ✅ **delivered in 6.5** (all 20 in an expandable accordion, stored as an `advanced_options` JSON bag, validated). HA / backup retention / monitoring level / support tier **drive real cost**; region, AZ, DB version, encryption, DR, logging level, storage tier, autoscaling, network type, firewall profile, private/public endpoint, DNS, certificates, secrets management, compliance profile are validated capture-only. *(None provision anything real yet — the deferred technology-to-real-resource boundary.)*

**Live Cost Panel** — ✅ one-time/monthly/annual totals, live update, **compute/storage/licence split (6.4)**, **+ backup/monitoring/support lines driven by advanced options (6.5)**, **+ "Explain this cost" — plain-English cost drivers + optimization tips (F-RPT-07)**. ⬜ network (usage-based, not estimated).

**AI Assistant (8 capabilities)** — ✅ **request drafting (F-RPT-06)** · ✅ **cost explanation + optimisation tips (F-RPT-07)** · ✅ **failure triage (F-RPT-08)** · ✅ **recommend cloud & sizing** with a cross-cloud price comparison · ✅ **natural-language approvals bot** (interpret-only; approve/reject always confirm first) · ✅ **portal help** ("what does this page do?", grounded in a curated knowledge base). ⬜ detect gaps · predict time · compliance advice. All **recommend-only per hard-rule P3** — the AI never decides, executes, prices authoritatively, or holds credentials.

**Bottom Actions** — ✅ Save Draft · Submit · (Cancel trivial) · **Generate Cost Sheet — PDF and Excel (6.4)**. ⬜ Validate Request · Preview Environment → small increments.

**Submission Flow** — ✅ validate fields · calculate pricing · **create Jira ticket** · **attach costing PDF + Excel to the ticket (6.4)** · plan-preview/blueprint in the ticket body · display tracking ID. ⬜ assign approvers · send approval notification → **E2**.

**Design-process outputs (Figma mockup, wireframe, component hierarchy, palette, typography, icons, journey, a11y, responsive, mobile/tablet/desktop)** — ✅ satisfied by *building on Carbon* (its design system, palette, typography, icon set, accessibility and responsive grid) instead of separate Figma artefacts. ⬜ standalone mockup/wireframe/user-journey documents were not produced (we built the working UI) — revisit only if a formal design sign-off deliverable is required.

**Portal UI polish (small, newly tracked here)** — ✅ **all delivered**: technology & size cards with icons/specs (6.3), header **Search**, on-form **Environment** and **Approval** summary panels, **multi-step wizard** progress, **sticky submit** bar, and deployment-target cards on a single row.

> Roll-up to enterprise phases: requester enrichment → **E1** · approvers/notifications/SLA → **E2** · cost breakdown + Excel → **E3** · AI Copilot → **E4** · catalog/advanced-options + multi-cloud → **catalog/multi-cloud increments** · Validate/Preview + UI polish → **small increments**.

---

## 5. Phase 3+ — Enterprise increments (E1–E4)

Sequenced per ARCHITECTURE.md §12 — **build what's hard to retrofit first.** Listed here at batch level; each will be decomposed into Phase-1-style small increments when we reach it.

- **E1 — Enterprise foundations:** F-IAM-01 (RBAC — roles built, Jira-group-driven; live enforcement awaits the admin's group→role map) **· ✅ F-IAM-03 segregation of duties delivered (E1.2): a user can't approve/apply/destroy a request they raised; `SOD_ENFORCED` default on; poller exempt · ✅ F-SEC-01 tamper-evident audit delivered: `verify_chain` + `GET /api/audit/verify` + `python -m api.audit verify` CLI (concurrency-tolerant: detects edits/deletions, tolerates benign poller/API forks), optional keyed HMAC (`AUDIT_HMAC_KEY`)**. **✅ F-OPS-09 admin console** (`GET /api/config` — effective governance & FinOps posture, platform_admin-only; an Admin portal page showing that posture at a glance, cost-centre **budget management** with live spend/remaining add/edit/delete, and the **orphaned-environments** list). **✅ F-INT-01 programmatic API access** (`ApiKey` — sha256-hashed, acts as the issuing user so it inherits their roles and can never exceed them; a new `_authed_requester` resolves `X-API-Key` to the bound identity ahead of the unchanged Entra/mock auth, so every endpoint gains key support; `POST/GET/DELETE /api/api-keys` self-service, secret shown once, `apikey.created`/`apikey.revoked` audits; API keys panel on the admin console). Remaining: F-IAM-09 (group ownership), F-OPS-04 (tracing), F-OPS-01 (HA), F-SEC-09 (hardening). **✅ F-SEC-03/04 IaC security scanning** (`orchestrator/scanner.py` — a dependency-free ruleset over the terraform plan JSON: public-bucket access + unencrypted sensitive data = HIGH, missing mandatory tags = MEDIUM, versioning off = LOW; `provisioner.terraform_plan` scans the saved plan via `terraform show -json`; `/provision` returns the findings and blocks on HIGH only when `IAC_SCAN_ENFORCE=true`, default off; the API records a `scan.findings` audit + status_detail, and now surfaces plan failures in status_detail). **The OCI bucket module was then hardened** (object versioning enabled unconditionally; optional `kms_key_id`/`OCI_KMS_KEY_OCID` for customer-managed encryption) so a compliant internal env now scans clean and sensitive data can be CMK-encrypted; `terraform validate` passes. **Plus, per ARCHITECTURE.md §12 notes:** make the orchestrator handoff idempotent/resumable (F-ORC-01/03/04 — idempotency ledger already in place) — foundational.
- **E2 — Governance depth:** **✅ F-GOV-01 approval SLA + breach escalation** (server-computed on-time/due-soon/breached vs `APPROVAL_SLA_HOURS`; poller escalates once via a Jira comment + `sla.breached` audit; My Requests SLA tag + Estate KPI). **✅ F-GOV-08 four-eyes** (the Jira approver must differ from the requester; live gate in the poller + approve, `four_eyes.blocked` audit + Jira comment, `FOUR_EYES_ENFORCED` default on, fail-open on unknown author). **✅ F-GOV-10 evidence pack** (`GET /api/requests/{ref}/evidence.pdf` — request + cost + approval + full audit trail + a re-verified "Audit chain: VERIFIED" attestation; My Requests download link). **✅ F-GOV-05 change windows** (approved requests provision only inside `CHANGE_WINDOW_*` days/hours/tz; held with a visible reason + `change_window.held` audit and auto-provision when it opens; default off; tzdata added for real timezones). **✅ F-GOV-02 policy waivers** (`POST /api/requests/{ref}/waiver` — an authorised approver, never the requester, grants a documented, expiring exception so a policy-blocked request can submit; `waiver.granted`/`waiver.blocked`/`policy.waived` audits; shown in the evidence pack + My Requests; only OPA policy violations are waivable, field validation still applies). **✅ F-GOV-06 approval quorum** (re-verifies N distinct Jira approvers, never the requester, before provisioning — `APPROVAL_QUORUM` default 1; `get_approvers` reads the JSM approval API with a changelog fallback; held with a visible "X of N approved" reason + `quorum.blocked`/`quorum.met` audits, re-checked each poll; live-only, fail-closed). **✅ F-GOV-03 policy-as-code depth** (deeper Rego guardrails — sensitive-data encryption-at-rest on cloud, restricted-data no public exposure, xlarge tier/criticality guardrail, dev/test right-sizing floor — plus a non-blocking advisory `warnings` channel surfaced in the submit response, the portal, and the evidence pack; 19 Rego unit tests; also fixed a latent bug where `not present(input.x)` let an *absent* mandatory tag slip past the gate). **✅ F-ORC-02 plan preview** (delivered in 1.8). **E2 — Governance depth: COMPLETE.**
- **E3 — FinOps & lifecycle:** **✅ E3.2 F-FIN-03 showback & chargeback** (`GET /api/showback?group_by=cost_centre|project|environment|owner&scope=active|committed` — sums the captured estimate by dimension; oversight-gated; Showback portal view with a Running/Committed toggle + proportion bars; reconciles with the estate's active monthly cost). **✅ E3.1 F-FIN-07 environment TTL & renewal** (non-prod envs get a lifetime `TTL_DAYS_NONPROD`; prod/dr exempt; poller sweep warns owners `TTL_WARN_DAYS` before expiry via Jira + `ttl.expiring`, flags expired via `ttl.expired`, and — only when `TTL_ENFORCE=true`, default off — auto-decommissions; `POST /api/requests/{ref}/renew` extends + audits `ttl.renewed`; TTL surfaced on requests + My Requests tag + Renew button). **✅ E3.3 F-FIN-02 budget guardrails** (`Budget` per cost centre; `GET/POST/DELETE /api/budgets` — set/list with current committed spend + remaining; submit-time guardrail compares projected committed spend vs the budget — near `BUDGET_WARN_PCT` warns, over blocks 422 only when `BUDGET_ENFORCE=true` else warns; `budget.warning`/`budget.blocked`/`budget.set` audits; undefined cost centres ungated; Budgets panel on the Showback view). **✅ E3.5 F-FIN-01 actual-vs-estimate variance** (`ActualCost` per request; `POST /api/requests/{ref}/actual` records the billed monthly — the slot a live cloud-billing sync fills later — computes variance vs the approved estimate and raises a one-time `variance.alert` when drift > `VARIANCE_ALERT_PCT`; `GET /api/variance` estate report + totals; variance exposed on requests + a My Requests tag + a Variance panel on Showback). **FinOps MVP complete: Showback + TTL/renewal + Budget guardrails + Variance.** **✅ E3.7 F-LCM-10 ownership transfer & orphan detection** (`POST /api/requests/{ref}/transfer-owner` reassigns the environment/application/business/technical owner, audits `ownership.transferred`, clears the orphan flag; a provisioned env whose effective owner is in `DEPARTED_OWNERS` is an orphan — `GET /api/orphans` lists them, the poller sweep flags new ones once via `ownership.orphaned` + a Jira note + `orphaned_at`; owner + orphaned exposed on requests; My Requests shows the owner, an orphaned tag, and a Transfer-owner control). **✅ E3.8 F-LCM-08 environment health score** (`_health_for` scores each provisioned env 0-100 + grade A-E from TTL/orphan/IaC-scan/cost-variance/backup/monitoring/ownership signals, with the deducting factors listed; exposed on requests + a My Requests health badge; `GET /api/health-scores` estate list worst-first + average). **✅ E3.9 F-FIN-08 quota management** (`Quota` per project; `GET/POST/DELETE /api/quotas` — set/list with current env count + remaining; submit-time guardrail on `create` requests compares the project's active-environment count vs the quota — at the ceiling warns, over blocks 422 only when `QUOTA_ENFORCE=true` else warns; `quota.warning`/`quota.blocked`/`quota.set` audits; undefined projects ungated; Quotas panel on the admin console). **✅ E3.10 F-LCM-09 drift detection** (`orchestrator/drift.py` `detect_drift` over a `terraform show -json` plan — any non-no-op/read change = drift; `provisioner.terraform_drift` re-plans a provisioned request's workspace read-only; orchestrator `/drift` endpoint; `POST /api/requests/{ref}/drift-check` (oversight) records `drift.detected`/`drift.none` + `drift_detected`/`drift_checked_at` on the request; My Requests "Check drift" action + a drift tag). **✅ F-FIN-06 scheduled auto-shutdown** (`api/shutdown.py` + `GET /api/shutdown` + a Scheduled-shutdown panel on Showback: a business-hours schedule (`SHUTDOWN_DAYS/START/END/TZ`) for non-prod environments (prod/DR never shut down), and — the headline — the **quantified monthly saving** = each non-prod env's compute cost × the off-hours fraction (storage keeps billing). Pausing is opt-in (`SHUTDOWN_ENABLED`, default off) and **portal-side**: the poller sweep records the off-hours ⇄ business-hours boundary as `shutdown.paused`/`shutdown.resumed` audits, once per boundary; it never stops real cloud resources (a real orchestrator stop/start is a documented hook). Read-only saving view; oversight-gated.) Remaining (all Should/Could): F-LCM-03/06 (refresh, restore), F-IAM-07 + F-INT-05 (JIT access + vault delivery).
- **E4 — Intelligence & ecosystem:** **✅ F-INT-09 CLI** (`cli/infractl.py`, run `python -m cli.infractl` — whoami/requests/request/showback/health/renew/drift-check; authenticates with an API key via `X-API-Key` so it rides the same RBAC; httpx client, no server change). **✅ F-RPT-06 AI request drafting** (`api/ai_drafter.py` + `POST /api/ai/draft` + a "Draft with AI" box on the request form: a plain-English description → a draft that pre-fills the form, constrained to the approved catalogue. The AI **recommends only** — it never submits/approves/prices/provisions (ARCHITECTURE.md §7); every value is re-checked against the live catalogue server-side, and the submit path still re-validates. Mock-first: `AI_MODE=mock` (default) is a deterministic offline drafter; `AI_MODE=live` uses the Claude API — needs `ANTHROPIC_API_KEY` in `.env`. Every draft is audited (`ai.drafted`).) **✅ F-RPT-07 AI cost explanation** (`api/ai_explainer.py` + `POST /api/cost/explain` + an "Explain this cost" action on the Live Cost panel: narrates the authoritative `estimate_cost` breakdown — ranks the cost drivers server-side and offers advisory optimization tips. **Explains/suggests only** — never changes the request, re-prices, submits, or provisions. Same mock-first/live-Claude pattern as F-RPT-06 (shared `anthropic_client()`); audited (`ai.explained`).) **✅ F-RPT-08 AI failure triage** (`api/ai_triage.py` + `POST /api/requests/{ref}/triage` + a "Diagnose failure" action on a failed/blocked request in My Requests: reads the request's real failure signals — status, `status_detail`, and its failure audit events (`apply.failed`/`plan.failed`/`scan.findings`/`poll.error`/…) — and returns a plain-English diagnosis + likely cause(s) + next steps. **Diagnoses/advises only** — never retries, applies, or changes the request. Platform-admin gated; audited (`ai.triaged`). Same mock-first/live-Claude pattern.) **✅ F-INT-02 lifecycle event stream** (`api/eventstream.py` + `GET /api/events` (cursor feed) + `GET /api/events/stream` (SSE) + a live **Activity** page: a curated, read-only view over the tamper-evident audit log publishing request/environment lifecycle events. Consumers tail a monotonic `cursor` (gap-free, at-least-once) or subscribe via SSE (`Last-Event-ID` resume); filter by `?since/tail/reference/types/all`. Oversight-gated (`view_overview`); external systems subscribe with an F-INT-01 API key. No external broker — a real bus or outbound webhooks (F-INT-10) can publish off the same feed later.) **✅ F-INT-08 ChatOps approvals bot** (`api/chatbot.py` + `POST /api/chatops` (runs as the signed-in user) + `POST /api/chatops/slack` (Slack signature-verified, live) + an in-portal **Assistant** console: `pending` / `status <ref>` / `approve <ref> [note]` / `reject <ref> [note]`. The bot **holds no authority** — approve/reject enact the mapped human's decision by transitioning the Jira ticket (system of record) + an attributed comment, then the normal re-verified flow proceeds; it never provisions. Enforces RBAC (approver/platform_admin) + segregation of duties; audited (`chatops.approved`/`chatops.rejected`). Mock-first: the console works offline; Slack turns on only with `SLACK_SIGNING_SECRET`. Teams is a later drop-in on the same engine.) **✅ F-RPT-05 spend forecast** (`api/forecast.py` + `GET /api/forecast?months=6` + a Forecast panel on Showback: a deterministic month-by-month projection of monthly spend — today's provisioned run-rate, plus in-flight pipeline (submitted/planned/in-progress, excl. decommissions) as it provisions, minus non-prod environments as their TTL expires. Read-only; reconciles with Showback's committed scope; oversight-gated (`view_overview`). Capacity projection can extend it later.) **✅ F-FIN-09 + F-RPT-10 anomaly detection** (`api/anomalies.py` + `GET /api/anomalies` + an Anomalies panel on the Estate Overview: deterministic detectors — cost (outlier spend vs estate mean+σ, actual-vs-estimate overruns, budget breaches) and request (requester bursts, restricted/confidential data on public cloud/endpoint, high-impact xlarge/prod in-flight). Each carries a severity; sorted most-severe first. Read-only and advisory — flags signals only, never blocks or acts. Thresholds via `ANOMALY_*` env; oversight-gated (`view_overview`).) **✅ F-FIN-12 optimisation digest** (`api/optimisation.py` + `GET /api/optimisation` (+ `?owner=`) + an Optimisation panel on Showback: a per-owner rollup of non-prod savings opportunities — rightsize large/xlarge components, disable HA, downgrade over-spec monitoring/support — each with a monthly saving **re-priced through `estimate_cost`**. Prod/DR left alone. Read-only and advisory — recommends only, never changes a request. Oversight-gated (`view_overview`). Periodic delivery to owners plugs into report subscriptions (F-RPT-11) later.) **✅ F-FIN-13 sustainability estimate** (`api/sustainability.py` + `GET /api/sustainability` + a Sustainability panel on Showback: indicative energy (kWh/mo) + carbon (kgCO2e/mo) per provisioned environment from its sizing × documented power/PUE/grid coefficients (cloud greener than on-prem), an estate total + a relatable equivalent (car-km / trees). Coefficients configurable via `SUSTAIN_*`; labelled indicative (not metered). Read-only; oversight-gated (`view_overview`).) **E4 complete** — every catalogued E4 feature except the deliberately-deferred F-INT-10 (outbound webhooks, publishes off the F-INT-02 feed) and F-RPT-11 (report subscriptions).

- **Cloud control-plane roadmap (started):** the portal is growing from "records provisioning intent" toward a control plane that observes and manages real cloud state. **✅ Increment 1 — read-only cloud state sync / reconciliation** (`orchestrator/cloud_state.py` + orchestrator `/state` (signed, read-only, any mode) + `api/main._reconcile` + `POST /api/requests/{ref}/reconcile` + `GET /api/state` + a My Requests "Reconcile cloud state" action & cloud-drift tag): asks the orchestrator for the actual state of a request's resources and diffs it against the registry (`ProvisionedResource`), flagging out-of-band changes (a resource stopped/resized/deleted directly in the cloud); records `state_status`/`state_synced_at` + `state.reconciled`/`state.drift` audits. **Observes only** — never changes cloud state. Mock (default) reflects the registry (demo divergence via `CLOUD_STATE_SIMULATE_MISSING`); `CLOUD_STATE_MODE=live` is the OCI/Azure SDK path — an extension point needing cloud creds via a vault. Background sweep opt-in (`CLOUD_STATE_SYNC_ENABLED`, default off). Realises the reconciliation half of F-INT-04; extends F-LCM-09 from Terraform-plan to live state. **✅ Increment 2 — actuation (stop/start from the portal)** (`orchestrator/cloud_state.actuate` + orchestrator `/actuate` (signed) + `api/main.actuate_request` + `POST /api/requests/{ref}/actuate {action: stop|start}` + `ProvisionedResource.power_state` + a `power` field on the request views + My Requests Stop/Start controls, a power label & a stopped tag): an operator stops/starts a provisioned environment's resources from the portal. Goes **through the orchestrator** (signed handoff — the execute layer that holds cloud creds and re-verifies), never the API directly; records the resulting `power_state` on the registry + `resource.stopped`/`resource.started` audits. Reversible operational action, gated to **platform_admin** under a standing operational policy (no per-action Jira ticket — resize/decommission keep the approval path); idempotent. Mock (default) models the action and changes **no real cloud**; `CLOUD_STATE_MODE=live` calls the provider APIs — the same OCI/Azure extension point (`_actuate_live`) needing creds via a vault. Realises the actuation half of F-INT-04. **✅ Increment 3 — real OCI adapter** (`orchestrator/cloud_state._describe_live` + `_actuate_live` + `orchestrator/requirements.txt` (`oci` SDK, orchestrator-only — kept out of the api image; lazy-imported so mock/tests need it not) + `OCI_ACTUATE_ENABLED` opt-in): the live path is now real, reusing the **same OCI creds the orchestrator already uses for Terraform** (`OCI_*` env + the `./secrets` key mount) — no new secret. **describe** queries Object Storage (bucket exists?) and Compute (instance running/stopped?), read-only; **actuate** stops/starts real Compute instances (`instance_action` SOFTSTOP/START) — buckets have no power state, so a non-compute resource is refused, not silently ignored. Two independent gates: `CLOUD_STATE_MODE=live` turns on the real path, and real stop/start needs a **second** explicit opt-in `OCI_ACTUATE_ENABLED=true`, so enabling live reconciliation (read) never by itself enables live actuation (write); both refuse with a clear message if creds are absent. 13 offline unit tests (SDK client builders + the one write seam mocked). **Live-verified read-only against the real tenancy**: a real provisioned bucket describes as `active`, a bogus name as `missing`. **✅ Increment 4 — a Compute (VM) resource type** (`Technology.resource_kind` + `Request.resource_kind` + a "Compute Instance (VM)" catalogue entry + RHEL/Windows classified as compute + a branching Terraform module + orchestrator branch + `_environment_resource_kind` + compute-only power/stop-start): a request whose technology is compute provisions — and registers — a stoppable `oci-instance` instead of a bucket, so the control plane offers stop/start on it. The single Terraform module branches on `resource_kind`: bucket (unchanged) or a **private-only Compute VM** in an **existing subnet**, flex-shape sized from the request. Real VM creation is gated on three customer inputs (`OCI_COMPUTE_SUBNET_OCID` / `_IMAGE_OCID` / `_SSH_AUTHORIZED_KEY`, never committed); absent → a compute apply **refuses with a clear message** (verified live — no accidental VM in the apply-mode deployment). Mock mode records a mock instance so the control plane is demoable offline; power/stop-start is scoped to `oci-instance` so buckets never show it. 14 tests; full suite 480 passing; catalogue + gate + sizing live-verified. **Next:** supply the three compute inputs to provision a real VM, then connect actuation to F-FIN-06 so auto-shutdown actually stops non-prod (and knows when it's already stopped).

- **Blueprint registry & multi-resource provisioning (F-CAT-10, in progress):** the step from "the orchestrator knows how to build a handful of things" to "a request is a stack, and the recipes are data."
  - **✅ The registry** (`orchestrator/blueprints/*.yaml` + `orchestrator/blueprint_registry.py` + `Blueprint` + an admin Blueprints page): the blueprints *directory* is the registry — adding a recipe is adding a manifest, not editing code in several places. Each manifest declares its own preconditions (`enable_flag`, `requires_env`), so the portal can distinguish "certified" from "certified but not configured" instead of that surfacing as an apply-time failure. Certification is a separate, audited human act: shipping a recipe is not approving it.
  - **✅ Registering a customer's own Terraform** — the first externally-supplied module (`orchestrator/terraform/oci/apache-httpd`, written by the platform owner) reviewed, corrected to the portal's variable contract, and certified through the same path an admin would use.
  - **✅ One source of truth for what gets built.** Three bugs of the same shape, each found in production use, each fixed by removing a second source of truth: the catalogue badge read the technology table while certification lived in the blueprint table; `_environment_resource_kind` did the same one layer down (a certified Apache blueprint still provisioned an object-storage bucket — REQ-2026-0085); and the resource *name* and *image* were hard-coded per kind, so a new blueprint received an empty name and the wrong OS (REQ-2026-0092). Naming and image policy are now declared in the manifest (`name_var`, `vars`), with a per-technology image (`OCI_COMPUTE_IMAGE_MAP`) beating the blueprint's own default, which beats the shared compute default.
  - **✅ One generic "service on a VM" recipe** (`orchestrator/terraform/oci/service-vm` + `configure.TEMPLATES`): most of the catalogue is not a distinct cloud resource — it is a package on a machine. Rather than ~15 near-identical modules, one module takes the package, systemd unit and port as data. The ports are declared once and drive **both** the OS firewall and the network rules, so a service cannot end up running correctly behind a closed port. Wired for nginx and redis7; **not certified** — package names are unverified against a real image and `configure.VERIFIED_CODES` stays empty until someone boots a VM and watches the service answer.
  - **✅ A request is a stack, not one resource** (`_environment_resource_kinds` + `resource_kinds` on the handoff + per-kind workspaces): REQ-2026-0094 asked for Apache **and** a compute VM, built Apache alone, and closed its Jira ticket as Resolved. The kinds were collapsed with `sorted(kinds)[0]` — Apache won on alphabetical order. Plan, apply, destroy and drift now cover every resource in the request, each in its own workspace under `<reference>/<kind>` with its own name. **A workspace provisioned before this keeps its flat path forever**: its state file is there, and a workspace that cannot find its state believes the resource does not exist — it would build a second one and could never destroy the first.
  - **✅ No silent partial fulfilment** (`_unautomated_components` + `fulfilment.partial` audit + a Jira comment + `status_detail`): components with no automated recipe were provisioned "successfully" as a placeholder bucket. A partially delivered request now names what was not built, on the ticket as well as in the portal — reported as a clean success, a requester waits indefinitely for something nobody is going to build.
  - **✅ Permanent failures stop retrying** (`Request.provision_attempts` + `PROVISION_MAX_ATTEMPTS` + `POST /api/requests/{ref}/retry`): the poller re-attempted every cycle forever, so one request needing an unset flag generated a continuous stream of failed handoffs. It now halts after N attempts with a visible reason and a Jira note; clearing the hold is a human act, because an automatic reset would just resume the loop.
  - **✅ Drift stopped crying wolf** — OCI stamps `Oracle-Tags.CreatedBy/CreatedOn` on every resource; the modules did not set them, so every plan proposed removing them and every drift check reported a healthy environment as changed.
  - **✅ service-vm boot-tested and certified** — nginx and Redis 7 proven on real machines (GAP-ANALYSIS steps 9–10), then a multi-component request proven end to end. Certified = verified: 3 of 46.
  - **✅ Component detail form (F-CAT-03, F-CAT-07, F-CAT-11 partial).** Choosing a technology now asks for the version and the explicit shape (vCPU / memory / disk) rather than only a T-shirt size. The options are served by `GET /api/catalogue/component-options` from the SAME function that validates a submission, so the dropdowns and the rules cannot drift; the numeric options derive from the sizing anchors, so all 46 technologies work with no extra catalogue data. Versions are offered **only where the platform can install them** (the OL9 module streams proven in step 9) — a version dropdown the machine ignores is the Redis-6-sold-as-7 bug with a menu in front of it. The chosen numbers flow through sizing, pricing, the Jira ticket and Terraform; a shape off the anchors is allowed, priced as configured and flagged to the approver.
  - **Next:** the hourly cloud-option cache — shapes and images fetched from the tenancy, allowlist-filtered, cached in the portal DB with `source="oci-live"`, refreshed once an hour. It lands behind the endpoint above, so the form does not change.

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
