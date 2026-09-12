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

- **E1 — Enterprise foundations:** F-IAM-01 (RBAC — roles built, Jira-group-driven; live enforcement awaits the admin's group→role map) **· ✅ F-IAM-03 segregation of duties delivered (E1.2): a user can't approve/apply/destroy a request they raised; `SOD_ENFORCED` default on; poller exempt · ✅ F-SEC-01 tamper-evident audit delivered: `verify_chain` + `GET /api/audit/verify` + `python -m api.audit verify` CLI (concurrency-tolerant: detects edits/deletions, tolerates benign poller/API forks), optional keyed HMAC (`AUDIT_HMAC_KEY`)**. **✅ F-OPS-09 admin console** (`GET /api/config` — effective governance & FinOps posture, platform_admin-only; an Admin portal page showing that posture at a glance, cost-centre **budget management** with live spend/remaining add/edit/delete, and the **orphaned-environments** list). **✅ F-INT-01 programmatic API access** (`ApiKey` — sha256-hashed, acts as the issuing user so it inherits their roles and can never exceed them; a new `_authed_requester` resolves `X-API-Key` to the bound identity ahead of the unchanged Entra/mock auth, so every endpoint gains key support; `POST/GET/DELETE /api/api-keys` self-service, secret shown once, `apikey.created`/`apikey.revoked` audits; API keys panel on the admin console). **✅ All four since delivered** (verified 2026-09-09 against the code, not this text): F-IAM-09 group ownership (`api/main.py`, 6 tests), F-OPS-04 tracing (`api/audit.py` — correlation only, deliberately outside the entry hash), F-OPS-01 HA (`api/leader.py` single-active leader election for the poller, 9 tests), F-SEC-09 hardening (`common/security.py` — CSP, X-Content-Type-Options, X-Frame-Options, Referrer-Policy, Permissions-Policy, COOP, plus a shared DB-backed rate-limit store; `api/tests/test_security.py`). **✅ F-SEC-03/04 IaC security scanning** (`orchestrator/scanner.py` — a dependency-free ruleset over the terraform plan JSON: public-bucket access + unencrypted sensitive data = HIGH, missing mandatory tags = MEDIUM, versioning off = LOW; `provisioner.terraform_plan` scans the saved plan via `terraform show -json`; `/provision` returns the findings and blocks on HIGH only when `IAC_SCAN_ENFORCE=true`, default off; the API records a `scan.findings` audit + status_detail, and now surfaces plan failures in status_detail). **The OCI bucket module was then hardened** (object versioning enabled unconditionally; optional `kms_key_id`/`OCI_KMS_KEY_OCID` for customer-managed encryption) so a compliant internal env now scans clean and sensitive data can be CMK-encrypted; `terraform validate` passes. **Plus, per ARCHITECTURE.md §12 notes:** make the orchestrator handoff idempotent/resumable (F-ORC-01/03/04 — idempotency ledger already in place) — foundational.
- **E2 — Governance depth:** **✅ F-GOV-01 approval SLA + breach escalation** (server-computed on-time/due-soon/breached vs `APPROVAL_SLA_HOURS`; poller escalates once via a Jira comment + `sla.breached` audit; My Requests SLA tag + Estate KPI). **✅ F-GOV-08 four-eyes** (the Jira approver must differ from the requester; live gate in the poller + approve, `four_eyes.blocked` audit + Jira comment, `FOUR_EYES_ENFORCED` default on, fail-open on unknown author). **✅ F-GOV-10 evidence pack** (`GET /api/requests/{ref}/evidence.pdf` — request + cost + approval + full audit trail + a re-verified "Audit chain: VERIFIED" attestation; My Requests download link). **✅ F-GOV-05 change windows** (approved requests provision only inside `CHANGE_WINDOW_*` days/hours/tz; held with a visible reason + `change_window.held` audit and auto-provision when it opens; default off; tzdata added for real timezones). **✅ F-GOV-02 policy waivers** (`POST /api/requests/{ref}/waiver` — an authorised approver, never the requester, grants a documented, expiring exception so a policy-blocked request can submit; `waiver.granted`/`waiver.blocked`/`policy.waived` audits; shown in the evidence pack + My Requests; only OPA policy violations are waivable, field validation still applies). **✅ F-GOV-06 approval quorum** (re-verifies N distinct Jira approvers, never the requester, before provisioning — `APPROVAL_QUORUM` default 1; `get_approvers` reads the JSM approval API with a changelog fallback; held with a visible "X of N approved" reason + `quorum.blocked`/`quorum.met` audits, re-checked each poll; live-only, fail-closed). **✅ F-GOV-03 policy-as-code depth** (deeper Rego guardrails — sensitive-data encryption-at-rest on cloud, restricted-data no public exposure, xlarge tier/criticality guardrail, dev/test right-sizing floor — plus a non-blocking advisory `warnings` channel surfaced in the submit response, the portal, and the evidence pack; 19 Rego unit tests; also fixed a latent bug where `not present(input.x)` let an *absent* mandatory tag slip past the gate). **✅ F-ORC-02 plan preview** (delivered in 1.8). **E2 — Governance depth: COMPLETE.**
- **E3 — FinOps & lifecycle:** **✅ E3.2 F-FIN-03 showback & chargeback** (`GET /api/showback?group_by=cost_centre|project|environment|owner&scope=active|committed` — sums the captured estimate by dimension; oversight-gated; Showback portal view with a Running/Committed toggle + proportion bars; reconciles with the estate's active monthly cost). **✅ E3.1 F-FIN-07 environment TTL & renewal** (non-prod envs get a lifetime `TTL_DAYS_NONPROD`; prod/dr exempt; poller sweep warns owners `TTL_WARN_DAYS` before expiry via Jira + `ttl.expiring`, flags expired via `ttl.expired`, and — only when `TTL_ENFORCE=true`, default off — auto-decommissions; `POST /api/requests/{ref}/renew` extends + audits `ttl.renewed`; TTL surfaced on requests + My Requests tag + Renew button). **✅ E3.3 F-FIN-02 budget guardrails** (`Budget` per cost centre; `GET/POST/DELETE /api/budgets` — set/list with current committed spend + remaining; submit-time guardrail compares projected committed spend vs the budget — near `BUDGET_WARN_PCT` warns, over blocks 422 only when `BUDGET_ENFORCE=true` else warns; `budget.warning`/`budget.blocked`/`budget.set` audits; undefined cost centres ungated; Budgets panel on the Showback view). **✅ E3.5 F-FIN-01 actual-vs-estimate variance** (`ActualCost` per request; `POST /api/requests/{ref}/actual` records the billed monthly — the slot a live cloud-billing sync fills later — computes variance vs the approved estimate and raises a one-time `variance.alert` when drift > `VARIANCE_ALERT_PCT`; `GET /api/variance` estate report + totals; variance exposed on requests + a My Requests tag + a Variance panel on Showback). **FinOps MVP complete: Showback + TTL/renewal + Budget guardrails + Variance.** **✅ E3.7 F-LCM-10 ownership transfer & orphan detection** (`POST /api/requests/{ref}/transfer-owner` reassigns the environment/application/business/technical owner, audits `ownership.transferred`, clears the orphan flag; a provisioned env whose effective owner is in `DEPARTED_OWNERS` is an orphan — `GET /api/orphans` lists them, the poller sweep flags new ones once via `ownership.orphaned` + a Jira note + `orphaned_at`; owner + orphaned exposed on requests; My Requests shows the owner, an orphaned tag, and a Transfer-owner control). **✅ E3.8 F-LCM-08 environment health score** (`_health_for` scores each provisioned env 0-100 + grade A-E from TTL/orphan/IaC-scan/cost-variance/backup/monitoring/ownership signals, with the deducting factors listed; exposed on requests + a My Requests health badge; `GET /api/health-scores` estate list worst-first + average). **✅ E3.9 F-FIN-08 quota management** (`Quota` per project; `GET/POST/DELETE /api/quotas` — set/list with current env count + remaining; submit-time guardrail on `create` requests compares the project's active-environment count vs the quota — at the ceiling warns, over blocks 422 only when `QUOTA_ENFORCE=true` else warns; `quota.warning`/`quota.blocked`/`quota.set` audits; undefined projects ungated; Quotas panel on the admin console). **✅ E3.10 F-LCM-09 drift detection** (`orchestrator/drift.py` `detect_drift` over a `terraform show -json` plan — any non-no-op/read change = drift; `provisioner.terraform_drift` re-plans a provisioned request's workspace read-only; orchestrator `/drift` endpoint; `POST /api/requests/{ref}/drift-check` (oversight) records `drift.detected`/`drift.none` + `drift_detected`/`drift_checked_at` on the request; My Requests "Check drift" action + a drift tag). **✅ F-FIN-06 scheduled auto-shutdown** (`api/shutdown.py` + `GET /api/shutdown` + a Scheduled-shutdown panel on Showback: a business-hours schedule (`SHUTDOWN_DAYS/START/END/TZ`) for non-prod environments (prod/DR never shut down), and — the headline — the **quantified monthly saving** = each non-prod env's compute cost × the off-hours fraction (storage keeps billing). Pausing is opt-in (`SHUTDOWN_ENABLED`, default off) and **portal-side**: the poller sweep records the off-hours ⇄ business-hours boundary as `shutdown.paused`/`shutdown.resumed` audits, once per boundary; it never stops real cloud resources (a real orchestrator stop/start is a documented hook). Read-only saving view; oversight-gated.) **✅ All since delivered** (verified 2026-09-09): F-LCM-03 refresh and F-LCM-06 restore are both request types with their own signed handoffs and 44 tests between them; F-IAM-07 + F-INT-05 JIT access and vault credential delivery are built (`api/vault.py`, `AccessGrant`, 26 tests).
- **E4 — Intelligence & ecosystem:** **✅ F-INT-09 CLI** (`cli/infractl.py`, run `python -m cli.infractl` — whoami/requests/request/showback/health/renew/drift-check; authenticates with an API key via `X-API-Key` so it rides the same RBAC; httpx client, no server change). **✅ F-RPT-06 AI request drafting** (`api/ai_drafter.py` + `POST /api/ai/draft` + a "Draft with AI" box on the request form: a plain-English description → a draft that pre-fills the form, constrained to the approved catalogue. The AI **recommends only** — it never submits/approves/prices/provisions (ARCHITECTURE.md §7); every value is re-checked against the live catalogue server-side, and the submit path still re-validates. Mock-first: `AI_MODE=mock` (default) is a deterministic offline drafter; `AI_MODE=live` uses the Claude API — needs `ANTHROPIC_API_KEY` in `.env`. Every draft is audited (`ai.drafted`).) **✅ F-RPT-07 AI cost explanation** (`api/ai_explainer.py` + `POST /api/cost/explain` + an "Explain this cost" action on the Live Cost panel: narrates the authoritative `estimate_cost` breakdown — ranks the cost drivers server-side and offers advisory optimization tips. **Explains/suggests only** — never changes the request, re-prices, submits, or provisions. Same mock-first/live-Claude pattern as F-RPT-06 (shared `anthropic_client()`); audited (`ai.explained`).) **✅ F-RPT-08 AI failure triage** (`api/ai_triage.py` + `POST /api/requests/{ref}/triage` + a "Diagnose failure" action on a failed/blocked request in My Requests: reads the request's real failure signals — status, `status_detail`, and its failure audit events (`apply.failed`/`plan.failed`/`scan.findings`/`poll.error`/…) — and returns a plain-English diagnosis + likely cause(s) + next steps. **Diagnoses/advises only** — never retries, applies, or changes the request. Platform-admin gated; audited (`ai.triaged`). Same mock-first/live-Claude pattern.) **✅ F-INT-02 lifecycle event stream** (`api/eventstream.py` + `GET /api/events` (cursor feed) + `GET /api/events/stream` (SSE) + a live **Activity** page: a curated, read-only view over the tamper-evident audit log publishing request/environment lifecycle events. Consumers tail a monotonic `cursor` (gap-free, at-least-once) or subscribe via SSE (`Last-Event-ID` resume); filter by `?since/tail/reference/types/all`. Oversight-gated (`view_overview`); external systems subscribe with an F-INT-01 API key. No external broker — a real bus or outbound webhooks (F-INT-10) can publish off the same feed later.) **✅ F-INT-08 ChatOps approvals bot** (`api/chatbot.py` + `POST /api/chatops` (runs as the signed-in user) + `POST /api/chatops/slack` (Slack signature-verified, live) + an in-portal **Assistant** console: `pending` / `status <ref>` / `approve <ref> [note]` / `reject <ref> [note]`. The bot **holds no authority** — approve/reject enact the mapped human's decision by transitioning the Jira ticket (system of record) + an attributed comment, then the normal re-verified flow proceeds; it never provisions. Enforces RBAC (approver/platform_admin) + segregation of duties; audited (`chatops.approved`/`chatops.rejected`). Mock-first: the console works offline; Slack turns on only with `SLACK_SIGNING_SECRET`. Teams is a later drop-in on the same engine.) **✅ F-RPT-05 spend forecast** (`api/forecast.py` + `GET /api/forecast?months=6` + a Forecast panel on Showback: a deterministic month-by-month projection of monthly spend — today's provisioned run-rate, plus in-flight pipeline (submitted/planned/in-progress, excl. decommissions) as it provisions, minus non-prod environments as their TTL expires. Read-only; reconciles with Showback's committed scope; oversight-gated (`view_overview`). Capacity projection can extend it later.) **✅ F-FIN-09 + F-RPT-10 anomaly detection** (`api/anomalies.py` + `GET /api/anomalies` + an Anomalies panel on the Estate Overview: deterministic detectors — cost (outlier spend vs estate mean+σ, actual-vs-estimate overruns, budget breaches) and request (requester bursts, restricted/confidential data on public cloud/endpoint, high-impact xlarge/prod in-flight). Each carries a severity; sorted most-severe first. Read-only and advisory — flags signals only, never blocks or acts. Thresholds via `ANOMALY_*` env; oversight-gated (`view_overview`).) **✅ F-FIN-12 optimisation digest** (`api/optimisation.py` + `GET /api/optimisation` (+ `?owner=`) + an Optimisation panel on Showback: a per-owner rollup of non-prod savings opportunities — rightsize large/xlarge components, disable HA, downgrade over-spec monitoring/support — each with a monthly saving **re-priced through `estimate_cost`**. Prod/DR left alone. Read-only and advisory — recommends only, never changes a request. Oversight-gated (`view_overview`). Periodic delivery to owners plugs into report subscriptions (F-RPT-11) later.) **✅ F-FIN-13 sustainability estimate** (`api/sustainability.py` + `GET /api/sustainability` + a Sustainability panel on Showback: indicative energy (kWh/mo) + carbon (kgCO2e/mo) per provisioned environment from its sizing × documented power/PUE/grid coefficients (cloud greener than on-prem), an estate total + a relatable equivalent (car-km / trees). Coefficients configurable via `SUSTAIN_*`; labelled indicative (not metered). Read-only; oversight-gated (`view_overview`).) **E4 complete** — including the two that were deferred and have since been built: F-INT-10 outbound webhooks (`api/webhooks.py`, publishing off the F-INT-02 feed) and F-RPT-11 report subscriptions (`ReportSubscription`).

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
  - **✅ Hourly cloud-option cache.** The form's shape/image data is fetched from the tenancy instead of typed into code. The ORCHESTRATOR reads the cloud (it holds the credentials; the API holds none) via a new read-only `POST /catalogue/oci-options`, and the API caches the answer hourly on a leader-elected thread as `component_option` rows tagged `source="oci-live"`. A refresh replaces only its own rows, so the hand-curated version lists — which no cloud API can answer — are never touched; a failed fetch changes nothing at all, because serving slightly stale options beats serving none. **Both allow-lists fail closed**: unset means offer nothing, so an unconfigured portal cannot put a bare-metal GPU shape on a dropdown. OS image becomes a real dropdown; shapes become a validation rule instead of a fifth control, catching an unbuildable vCPU/memory combination at request time rather than at apply time.
  - **✅ First-boot configuration is OS-family aware.** Offering Ubuntu images turned the config layer's single hard-coded package manager into a correctness problem: `dnf install` on Ubuntu fails, the `|| echo` swallows it, cloud-init finishes and the VM boots healthy with nothing on it — the GAP-ANALYSIS step 9 failure exactly. Package names, systemd unit names, the firewall tool and module-stream pinning now all follow the family, which is resolved PER MACHINE from the image the requester chose rather than from one global setting. Software with no recipe for that family is skipped and named on the machine, never rendered with another distribution's package names; the form withdraws it, and validation refuses the combination. Versions are withdrawn on Debian images because module streams are a Red Hat mechanism and a version we cannot pin is a wish, not a choice.
  - **⚠️ Ubuntu is NOT boot-tested.** The Debian package and unit names come from Ubuntu's documented catalogue, not from a machine that ran them, and plausible names are exactly what delivered Redis 6.2 under an entry called "Redis 7". `configure.VERIFIED_FAMILIES` is `{rhel}` and a test keeps it that way until a VM has been booted on Ubuntu and the service asked whether it works.
  - **Next (not started):** boot-test nginx on Ubuntu and certify what passes. Open and parked: the request form's size-card / dropdown duplication, single-option version dropdowns that would read better as text, and ARM shapes (held back until image/shape architecture pairing is guarded).

- **Self-certifying catalogue (C0–C6) — the agent proves its own work.** The standing rule was *"once a component is certified it must not fail when a user selects it."* Nothing enforced it: `oci-oke` was certified by hand on 15 Aug and then failed REQ-2026-0148, 0149, 0150 and 0151 in a row while staying on offer; `postgres16` did the same across 0154–0158. A person had to notice, and nobody did. This series removes the human from the certification path and replaces the click with evidence.
  - **✅ C0 — fix the foundation first** (52d3325): the poller sweep blocked, so work started twice and the poller looked dead. Nothing above it could be trusted until this was true.
  - **✅ C1 — auto-decertification** (`api/certification.py`, 8a53d52): a blueprint whose recent record is nothing but failures takes itself off the catalogue. Certification became revocable by machine, on evidence, without waiting for someone to notice.
  - **✅ C2 — certification runner + cost envelope** (`api/proof.py`, `common/proof_rules.py`, 2542f97): a *proof build* — build the thing for real in a sandbox tier, verify it healthy, price it under a cap, and tear it down again. The RULES live in `common/` because the orchestrator re-verifies them itself rather than trusting the portal, the same stance it takes on a Jira approval. Certification is reachable only through a passing proof.
  - **✅ C3 — catalogue sync** (87afe7d): notice when the catalogue and the cloud disagree.
  - **✅ C4 — AI blueprint drafter** (`api/ai_blueprint.py`, ff9f7f7): when nothing ships a recipe, propose one — checked by a linter carrying every defect this project has already paid for (image chosen by list position, public IP in a private subnet, hard-coded versions, billed constants nobody chose).
  - **✅ C5 — the agent writes its own Terraform, wired and switched on** (8494f43, d9497cb, cf9cb6f, 07a135f, 9c88a0c): a generated store kept apart from reviewed recipes (**shipped always wins** — a generated manifest may never take a reviewed one's name or resource kind), a security scan over the plan, and a closed loop — draft, lint, prove, diagnose, redraft — bounded by `AUTOBUILD_MAX_ATTEMPTS`. Certification now records `certified_by: certification-runner`; **no human clicks Certify.**
  - **✅ C6 — the agent teaches the proven module a new technology** (fae5cc3): the correction to C5's premise. REQ-2026-0175 drafted Terraform for keycloak whose own opening line said *"builds nothing yet"* — and it passed. The lesson was not "generate better Terraform"; it was that generating Terraform was the wrong move. `oci/service-vm` already builds machines correctly, and every one of its behaviours was bought with a failed request: the image resolved from OCI at run time filtered by shape (OKE picked Oracle Linux 7.9 by list position and kubelet crash-looped on cgroup v1), a private subnet with no public IP (OCI refused the bastion VNIC outright), Run Command enabled (the only way to interrogate a private-only machine), and the machine reporting what it *became* (Terraform exiting zero once reported four broken machines as provisioned). So the agent now **classifies** first: `oci-*`/`aws-*`/`azure-*`/`gcp-*` are cloud-managed services and still get Terraform; everything else is software on a machine and gets a **technology profile** — a package, a systemd unit, a port, and a command to ask the machine what it actually installed. The publish order is **inverted on purpose** (published, then proved, then *withdrawn* if the proof fails), because the orchestrator reads the profile when rendering first-boot configuration and proving first would boot a machine with nothing to install.
  - **✅ C6b — software that ships as an archive** (c953232, 9409cc2): the correction to C6's limit. A package guess cannot install a tarball, so a profile may now name a pinned release URL, a computed sha256 and the unit that runs it — and because that is *root fetching and executing a file a model chose*, the rules moved to `common/profile_rules.py` where **both services enforce them**. An adversarial review demonstrated three working RCE paths first (prefix checks that `https://evil/#https://good` defeats, `cmd()` quoting YAML rather than shell, unquoted `write_files`); the answer was whole-value allow-lists, three independent locks, and a **missing checksum is a blocker, not a warning** — TLS proves only that the host is the host the profile named, and with a model choosing that host the checksum is the entire question. An archive install also **declares that it needs internet egress**, so a subnet with only a service gateway is refused before a machine is spent.
  - **✅ C6c — remember what a machine disproved** (148adca): REQ-2026-0183 spent a real VM learning that `dnf install backup` finds nothing, and the next request would have spent another. Every failed proof already held the machine's exact words and nothing read them. What is remembered is a **recipe, not a component** (keycloak was refuted as a package and succeeded as an archive the next day), only a **machine's verdict** counts (a cost cap or a failed teardown is our problem, not the recipe's), and it **expires on the same clock as a certification**, because a package absent today may be packaged tomorrow.
  - **✅ C6d — the catalogue says how a thing is delivered** (f2df49f): capabilities that will always be fulfilled by hand stopped being offered as installable software, and the classifier stopped inferring delivery from the code name — a naming convention doing a domain model's job, wrong in both directions. `postgres16` was OCI's managed service read as software: a live trap found by grouping the catalogue, not by anything failing.
  - **✅ C6e — a second way to install, and the honesty to read the answer** (3a50ecc, 8a1764e): REQ-2026-0184 was told `vault is not installed` — correct, and the wrong question, because Vault lives in HashiCorp's repository and the agent had no vocabulary for one. An **install-method ladder ordered by cost** (package grants no trust → vendor repository trusts a publisher for everything it will ever serve → archive executes one verified file), each rung a real machine, each refutation remembered. Then REQ-2026-0185 installed Vault 2.0.4 *perfectly* and was refused anyway — by us: an `expects` this code **recalled rather than measured**, and a 400 from an API root judged as a dead port. **`expects` is the PROMISE and `version_command` is the EVIDENCE**; a rolling repository promises nothing, so the machine reports `UNPROMISED (2.0.4)` and is not failed for it. Proven end to end: REQ-2026-0186 took a component with no blueprint, no Terraform and no recipe from form to running VM in 25.6 minutes, no human in the certification path.
  - **⏳ C7 — the machine tells the agent what it can install.** *The rule that replaces curation.* C6e generalised the *ladder* and left the *knowledge* hand-written: every new technology costs one failed request plus a row I type in. REQ-2026-0188 proved it — RabbitMQ was guessed as `dnf install rabbitmq`, refuted, and stopped, while the machine we had already paid for was holding `dnf`, the full repository metadata, and the answer (`rabbitmq-server`). **The same shape as every serious defect in this project: an honest signal the deciding code cannot see.** Five parts, none of them per-technology: (1) when a package is missing, the same boot reports what the repositories *do* offer and from which repo; (2) candidate name shapes — `<code>`, `<code>-server`, digits stripped — all tested in one boot, so one machine answers every hypothesis instead of one machine each; (3) **EPEL as a standard rung**, one signed repository covering a large class instead of one row per technology; (4) the machine reports the ports it actually opened, by diffing listening sockets before and against after, so a broker is never certified with nothing reachable; (5) fix `UNPROMISED (12)` — a shell error's *line number* read as a version, which walked straight past the "asked and could not answer" guard shipped the same day. **The machine only ever reports; it never installs what it discovered in the same boot** — a discovered name goes back through `profile_rules` and is installed on the next rung, so root still never runs a name that has not passed the allow-list. **Acceptance:** raise RabbitMQ and it certifies with nothing added by hand. If a dictionary has to be touched, the increment failed.
  - **Four defects of the mechanism itself, each found on a live request and each worth more than the feature:** a **version skew** (`common/` is shared, only one image was rebuilt, so every proof was refused with *"not a proof reference"* — which reads like an attack, not a stale container; the orchestrator now publishes the pattern it enforces and the API refuses to start when they disagree); a **proof of nothing** (a plan adding 0 resources passed — now refused at the plan, before anything is applied); **pricing by size rather than by kind** (a bucket, a VM, a cluster and a managed database all quoted the same AED 162.25, so object storage was overstated 51× while OKE was **understated** 2.4× with its control plane charged at nothing — cost now comes from the blueprint's `resource_kind` at Oracle's published rates); and worst, **every proof was building a bucket** — the handoff carried no `resource_kind` and the orchestrator's fallback is `oci-bucket`, so across 21–22 Aug exactly two compute instances were ever created, both by real user requests, and *not one proof had ever built a machine*. nginx and keycloak were certified on the evidence of a bucket. Both certifications were withdrawn; the plan summary must now **name the kind under test** before anything is applied.
  - **The lesson that runs through all four:** the record and the reality can disagree, and the record gets believed. A proof that *recorded* `oci-service-vm` while *building* a bucket. A cost line saying `resolved: False` beside a total of `0.00`. In every case the honest signal existed and the thing making the decision could not see it.
  - **Not yet true, and deliberately so** *(revised 2026-08-23; keycloak and vault now provision — C6b and C6e closed that)*: **the drafter writes Red Hat recipes only**, every method and every technology, and the form only learns a technology's real OS families *after* a profile exists — so the first request for a brand-new component can still be given an Ubuntu image and fail **after** the VM is built and billing, which is worse than the clean sandbox refusal it would otherwise get. **Nothing schedules a proof**: the trigger is a request arriving for an uncertified component, so a 30-day expiry is discovered at request time rather than on a rhythm. **Software in no repository at all** still needs a hand-measured archive entry; C7's discovery can say so definitively but cannot invent a download URL. Five of the seven OCI catalogue entries remain certified by hand, `oci-oke` among them.

**Priority within all of this:** land the fifteen highest-impact features (ARCHITECTURE.md §11) ahead of the wider catalogue, then sequence the rest by measured evidence.

---

### Reconciliation, 2026-09-09 — this plan had drifted from the code

Every feature the E1 and E3 batches listed as *remaining*, and both the E4
features recorded as *deliberately deferred*, had in fact been built. Eight
items, all shipped with their own modules and tests, and the plan still sent a
reader to build them.

**Why that matters more than a stale sentence.** This document is one of the four
that govern the project, and CLAUDE.md says to read it at the start of every
session. A "Remaining" line is an instruction. Following it would have meant
rebuilding working, tested features — and the reader would have had no reason to
doubt it, because the rest of the file is meticulous.

**How it was checked, and it was not by reading.** Each feature ID was matched to
a module or route that implements it and to tests that exercise it, because a
comment mentioning an ID is not delivery. The audit itself then produced a false
negative — it reported F-SEC-09 hardening ABSENT because it scanned `api`,
`orchestrator`, `db` and `cli` but not `common/`, where the implementation
actually lives. A check that validates what it looks at cannot see what it does
not, which is the same failure this phase kept finding in the code and then in
its own tests. The scope was widened and the result re-checked.

**What is genuinely outstanding is not code.** F-IAM-01's RBAC is complete —
three role sources, `manage_access` for editing the map — and what it "awaits" is
an operational act: populating the group → role map for this organisation.
Alongside it sit the two other non-code dependencies recorded under Phase P: a
network route from the orchestrator to a cluster API, without which everything
cluster-related stays dormant; and measured sizing figures from a capacity owner,
to supersede the deliberately technology-agnostic baseline.

**So there is no next increment in this plan.** Anything further is new scope, and
should be added as its own numbered phase rather than found in a stale line.

---

## 5a. Phase P — Placement resolution (added 2026-09-08)

Today a requester picks components and the request goes straight to costing. Nothing decides **where** each component runs, and three things are wrong because of it: the estimate cannot know whether three components mean one machine or three; module selection has no topology to select against; and the approver in Jira reads a shopping list rather than an architecture.

This phase inserts one step — **placement resolution** — between component selection and cost, and then wires its consequences through sizing, cost, the Jira payload and the Terraform plan.

**Three findings from the code that shape this phase.** Recorded here because each changes what "the obvious implementation" would have been:

1. **There is no migration tool.** `_ensure_tables()` adds missing *tables* and can never add a column (`api/main.py`, and the same warning five times in `db/models.py`). A new column on `technology` would silently not exist on the restored production database, and every read would return `None` — which is exactly the failure `TechnologyDelivery` was created to end. **Every new fact below is a new table.**
2. **"Deployment model" already exists** as `technology_delivery.delivery_model` (`managed | software | machine | capability`). Adding a second, overlapping field would recreate the two-sources-of-truth bug that table exists to fix. The new fact is a different question — *where an instance may run*, not *what the thing is* — so it is named **host mode** and P.1 asserts the two agree.
3. **"Profile" is taken, and dangerously so.** A *technology profile* is JSON that becomes commands root runs at first boot (`common/profile_rules.py`); an adversarial review on 2026-08-22 found three working root escapes in its rules. Reusing "profile" for sizing would put a benign meaning and a remote-code-execution meaning behind one word in the same codebase. Sizing facts here are **requirements**.

---

**P.1 — Host-mode catalogue**
New table `technology_host_mode` (`technology_code`, `cloud`, `host_mode` ∈ `vm | container | managed`), one row per supported combination — PostgreSQL on OCI has three, Node.js has two, a managed-only service has one. A test asserts every row is consistent with that technology's `delivery_model`, so the two tables can never disagree.
*Implements:* F-CAT-17.
*You'll know it works when:* asking the catalogue for PostgreSQL on OCI returns all three host modes, and asking for a managed-only service returns exactly one.

**P.2 — Host-mode resource requirements**
New table `host_mode_requirement` (`technology_code`, `host_mode`, `size`, minimum and recommended `vcpu` / `memory_gb` / `storage_gb` / `iops`, `effective_from`, `version`) — versioned and effective-dated exactly as `sizing_anchor` is, because the same technology needs a different shape as a container than as a VM guest.
*Implements:* F-CAT-18, extends F-CAT-07.
*You'll know it works when:* the same technology and size returns different minimums for `vm` and `container`, and last month's effective-dated row is still retrievable.
*Extended to the whole catalogue 2026-09-09.* Only **three** technologies had requirement rows, so the placement step told anyone choosing OpenSearch, Kafka, MySQL or twenty others that it "cannot be sized" — honestly, and uselessly. 41 of the 48 seeded technologies now have a host mode and so a sizing row; the 7 without are exactly the capabilities, which have nothing to place.
*No new numbers were invented.* The shapes still come from `HOST_MODE_BASELINE`, which is per (host mode, size) and deliberately asserts no difference between technologies — see its own comment on why claiming PostgreSQL needs more than Node.js at a given size would be inventing a fact. What was added is which technologies HAVE a host mode, which is a statement about the catalogue rather than about capacity, and it is **derived from the delivery model** rather than listed: `machine` and `software` imply `vm`, `managed` implies `managed` on the clouds the catalogue offers it on, and `capability` implies none. Those are the same facts `host_modes_disagree_with_delivery` already checks the two tables against, so deriving one from the other means they cannot drift apart at all — and a technology added tomorrow cannot arrive without a host mode.
*The blind spot that let this sit.* `test_every_seeded_technology_agrees_with_its_delivery_model` walks the technologies that HAVE host modes. It passed throughout while 48 had none: a check that validates what is present cannot notice what is absent. Four tests now assert the coverage itself.
*`container` was NOT added for anything, and this is the finding worth carrying.* Every certified blueprint in this system builds a machine — `oci-service-vm`, `oci-instance`, `oci-apache`, `oci-kafka`, `oci-postgres` — and **none deploys a workload into a cluster**. A `container` host mode is therefore an option that resolves, prices, reaches an approver and fails at provisioning. The two that exist (postgres16, nodejs20) predate this and are left alone, because removing options is a separate decision; a test names the count so a third is a deliberate act rather than an oversight. **P.5a's Scenario B rests on this gap**: the cluster options build `container` hosts, and nothing can build one.
*One resolver change followed from it.* With the cloud services finally declared `managed`, the system can tell that an object store does not belong on a machine. "Consolidated" and "separated" now REFUSE a layout containing a cloud-only component, naming it and pointing at the managed option — where before they came back "not priced", which is the sentence for a layout whose numbers are missing rather than one that cannot exist.

**P.3 — Placement policy in Rego**
New `policy/placement.rego`, package `infra.placement`, returning `allow` plus human-readable `violations` — the same shape `infra.authz` already returns, so the admin Policies page renders it unchanged. Co-residency denials (`denied_with`, `denied_in_environments`) are rules here, not branches in Python: adding a component must never require a code change (ARCHITECTURE.md P5).
*Implements:* F-GOV-12.
*You'll know it works when:* `opa test policy/` passes, and a consolidated topology in `prod` is denied with a sentence naming the rule while the same topology in `dev` is allowed.

**P.4 — The resolver: enumerate candidate topologies**
`api/placement.py` turns (components, cloud, environment tier) into the ordered option set of Scenario A — managed-where-available, consolidated, separated — each carrying its resolved hosts and the components on them. Every option is evaluated against P.3; a denied option is **returned with its reason**, never dropped. Pure function, no I/O.
*Implements:* F-CAT-17, F-GOV-12.
*You'll know it works when:* VM + PostgreSQL + Node.js in `dev` returns three options in that order, and the same selection in `prod` returns the consolidated one marked ineligible with the rule that forbade it.

**P.5 — Cluster entitlement resolution (Scenario B)**
For an OKE/AKS selection, list clusters the caller is actually entitled to — scoped by project, cost centre and subsidiary, then filtered again by their real access — each with region, current allocatable capacity, and whether this request fits. Clusters are **fetched live and cached hourly**, never stored as catalogue rows (ARCHITECTURE.md P7); a cluster that would breach a quota is shown ineligible with the quota named. Stateful workloads on Kubernetes carry a warning and the managed alternative alongside.
*Implements:* F-IAM-11, uses F-FIN-08.
*You'll know it works when:* two requesters with different entitlements see different cluster lists, a cluster that would breach a quota appears greyed with the ceiling stated, and PostgreSQL-on-Kubernetes shows both the warning and the managed option.

**P.5a — Enumerate the cluster options (Scenario B) — added 2026-09-08**
A gap in this plan, found while building P.10. P.4 enumerates only Scenario A; nothing enumerates "deploy onto an existing cluster" or "provision a new one", so P.5 built the entitlement machinery for a caller that did not exist and `api/clusters.py` was reachable only from its own tests. A selection naming OKE or AKS gets the two cluster options, plus the managed alternative where a stateful workload has one, plus the warning that storage, failover and backups become the requester's burden. A cluster provider is identified by its BLUEPRINT's resource kind billing as a cluster — `Technology.resource_kind` is documented as "wrong for most components" and must not be used.
*Implements:* F-IAM-11, F-CAT-17.
*You'll know it works when:* selecting OKE offers both cluster options; a requester with no entitled clusters sees "deploy onto an existing cluster" refused with that reason rather than missing; and PostgreSQL on a cluster carries both the warning and the managed alternative.

*NARROWED 2026-09-09 — both cluster options are now refused, because nothing in this system can deploy a workload into a cluster.*

*The blocker is a network route, not missing code.* `oke-cluster.tf` sets `is_public_ip_enabled = false`, so the Kubernetes API endpoint is private; the VCN's subnets are all private, so `bastion.tf` cannot assign a public IP and the module's own output says to fetch the kubeconfig from the bastion by hand; and the orchestrator runs in a container with no `ssh`, no `kubectl` and no route into that VCN. Terraform's kubernetes provider must reach the API server at plan time. **For a cluster the same request builds there is a second, independent blocker:** Terraform configures a provider before it creates resources, so a kubernetes provider cannot be pointed at a cluster — or a bastion — that the same apply is creating. That needs two stages with separate state whatever the network does, which is why "extend the OKE module and do it in one workspace" is not merely hard but impossible.

*What was found when this was traced is worse than the false promise it was traced for.* A workload placed on a cluster resolves to its OWN blueprint's resource kind — `oci-service-vm` for Node.js — and P.13's `_placement_units` accepted a container host as a machine. The cluster provider is excluded from the workloads and so had no host of its own. **"Provision a Kubernetes cluster and run Node.js on it" would have built one virtual machine and no cluster, and reported success** — the exact failure this phase exists to end, introduced by the increment that reads the placement. Before P.13 the kind list would at least have built the cluster alongside a spurious VM.

*So two things changed.* The options are **refused with the reason** rather than hidden, naming the workloads and pointing at what does work; and the orchestrator **refuses a container host outright** rather than silently building a machine from it, so a layout that reaches the far end cannot build the wrong thing even if an option is offered in error.

*What still works, and is why the options are refused rather than removed:* placement is optional (P.11), so an OKE request that never opens the placement step provisions the cluster exactly as every one has to date. A cluster **on its own** is also still an available placement option — it is the workloads that have no path, not the cluster.

*The entitlement machinery in `api/clusters.py` is dormant, not wrong.* P.5a existed because that logic had no caller and its tests passed anyway; this takes the caller away again for a reason that has nothing to do with entitlement, so `test_the_entitlement_machinery_is_intact_and_dormant` exists to stop the next reader deleting it as dead code.

*What would widen this again, in order:* (1) a network route from the orchestrator to a cluster API — a public endpoint with a source allowlist, the orchestrator inside the VCN, or OCI managed Bastion port-forwarding; that is an infrastructure decision, and the OKE blueprint's own note records that a contract-consuming rewrite "has no contract to consume" while the tenancy has no hub, no spoke VCNs and no IPAM. (2) A two-stage apply for the new-cluster case. (3) A workload blueprint, certified by a proof build on a real cluster. (4) ~~Cluster discovery~~ — **built 2026-09-09, see below**. Only after (1)–(3) is a `container` host mode a claim the portal can honour.

**Cluster discovery — built 2026-09-09, and dormant on purpose**
`orchestrator/cluster_discovery.py` lists the tenancy's Kubernetes clusters with their node pool capacity; `POST /catalogue/clusters` serves it over the same signed, read-only channel as `/catalogue/oci-options`, because listing clusters needs OCI credentials and the API holds none. `_discover_clusters` caches the answer for five minutes in memory and feeds it through `api/clusters.py`, so the entitlement, capacity and quota logic P.5 built finally has a live input.
*Built knowing it changes no outcome today.* Both cluster options are refused, so the list tells a requester what they WOULD be entitled to beside the reason they cannot use it. The alternative was to wait for (1)–(3), and the argument for not waiting is that the entitlement answer is then already arriving on the day a deployment path exists.
*Capacity is an UPPER BOUND, said so on every row and in the envelope.* Node pool totals are all the OCI API can give; true allocatable capacity is smaller — the kubelet reserves, the eviction threshold holds back, and scheduled pods have taken their share — and reading it needs the Kubernetes API, which is the very thing nothing here can reach. A field named `allocatable` quietly holding total capacity is how an optimistic number becomes an authoritative one two layers later, so it is labelled rather than renamed.
*"We could not look" is never served as "you have none".* A tenancy that cannot be read raises rather than returning `[]`; the endpoint answers 200 with the reason so a reachable-but-blind orchestrator is distinguishable from an unreachable one; and **the cache refuses to serve a stale list in place of a failed read**, because a requester has no way to tell a current answer from an old one.
*An untagged cluster is not treated as shared.* `RequesterScope.covers` reads a scope of `None` as "shared infrastructure, visible to all", which is right for a cluster somebody CHOSE to leave unscoped. A cluster discovered in the tenancy with no portal tags chose nothing, so discovery maps a missing tag to `""` — matching no scope — rather than handing it to every requester. That was accidental behaviour of the conversion until it was made a stated decision.

**P.6 — Sizing from the resolved topology**
Sizing is recomputed from hosts, not from the component list: co-resident components **sum** their requirements and add a configurable headroom factor. The old per-component path stays for requests raised before placement existed.
*Implements:* F-CAT-18.
*You'll know it works when:* three components consolidated onto one host show a summed shape, not the largest of the three, and the headroom factor is visible in the breakdown.

**P.7 — Cost recomputed on every placement change**
Cost is derived from the resolved hosts and recomputed whenever placement changes, server-side and authoritative (ARCHITECTURE.md P2). Each option carries its delta against the others so the requester chooses with the number in front of them.
*Implements:* extends existing cost estimation, F-UX-06.
*You'll know it works when:* switching from separated to consolidated changes the estimate on screen, and the figure that later reaches Jira is the post-placement one.

**P.8 — Persist and version the placement**
New table `request_placement` (`request_id`, `version`, `topology` as structured JSON, `created_at`, `created_by`, `superseded_at`) — append-only, so a request's placement history is as auditable as everything else privileged (ARCHITECTURE.md P4). A resize or add-component request reads the prior placement and offers only options consistent with it: you cannot offer "deploy to existing cluster" for an environment built on VMs.
*Implements:* F-CAT-19.
*You'll know it works when:* changing placement twice leaves two versions with the first superseded, and an add-component request against a VM-built environment is not offered a cluster.
*Narrowed 2026-09-08, while building P.11.* The constraint was being read from **this request's own** last placement, which turned it into a trap: choosing "managed" in the wizard and then asking to compare it against "consolidated" was refused with *"this environment was built on managed"* about an environment that did not exist. It now applies only once the environment **is** something — the status is past draft, submitted, planned, rejected and cancelled — because that is the fact the rule is about. The exception list names what is *not* yet built, so an unrecognised status stays constrained and a governance rule cannot be lost by omission.
*Still unbuilt, and named rather than approximated:* the acceptance sentence above says *add-component* — a follow-up against an environment **someone else** built. `add` and `resize` name their environment in free text (`target_environment`) with no link to the request that provisioned it, so the portal cannot identify that placement. Matching by name would rest a governance constraint on a soft string comparison; the case is left undone.

**P.9 — API endpoints, authoritative**
`POST /api/placement/options` and `POST /api/placement/resolve`. The API — not the BFF — re-resolves every cluster ID and re-runs entitlement, quota and co-residency before it will persist anything. A cluster ID from the browser is an *assertion*, never a fact (ARCHITECTURE.md P1/P2).
*Implements:* F-IAM-11, F-GOV-12.
*You'll know it works when:* posting a cluster ID the caller is not entitled to is refused by the API even when the BFF would have shown it, and the refusal names the reason.

**P.10 — BFF wiring**
The BFF passes the caller's identity through and shapes the response for the browser. It filters for presentation; it is not the control. Tested by calling the API directly with a forged cluster ID and confirming the refusal.
*Implements:* F-IAM-11.
*You'll know it works when:* bypassing the BFF entirely cannot place a workload anywhere the requester is not entitled to.

**P.11 — Placement step in the request wizard**
A new step between components and cost. Every option shows its topology, its cost and its delta; every ineligible option shows the reason it is ineligible. Silent filtering is a defect here — it makes the portal feel broken and generates the tickets this portal exists to prevent.
*Implements:* F-UX-16, F-UX-10.
*You'll know it works when:* you can see, in one screen, what will run where, what it costs, and — for anything you cannot choose — the sentence explaining why.
*Built 2026-09-08.* `webapp/frontend/src/components/PlacementStep.tsx`, a fourth progress step between Stack and Details, and the client in `api.ts`. Placement is **offered, not required**: every request raised before this step existed carries none, so demanding one would block them, and inventing a default in the browser would be the client deciding. Working out the options **saves the draft first**, behind a button — the API resolves against the stored request rather than a body the browser composed, and a form that persisted a draft as a side effect of typing would leave one behind for every abandoned visit. Changing the stack, the target or the tier **drops** the computed options rather than leaving a stale price on screen. The chosen layout is re-resolved by the API at submit, so a refusal there stops the submit and says so.
*Found while building it, both fixed:* the follow-up constraint trapped a requester in their first click (see P.8 above), and `filter_options` — written before Scenario B existed — rebuilt a refused option **without** its cluster list or its stateful-workload advice, so an option refused for using a cluster lost the clusters from the page.
*The practical limit on how useful this screen is right now:* `host_mode_requirement` carries rows for **three** technologies (`compute-vm`, `nodejs20`, `postgres16`). Everything else is correctly reported as unsizeable — *"cannot be sized: no requirement recorded for opensearch as vm"* — and therefore unpriced, because sizing a machine from a missing requirement would under-build it. The screen is finished; the numbers behind it are a data exercise (per technology, per host mode, per size) that P.2 deliberately deferred.

**P.11a — Resuming a saved draft — added 2026-09-09**
A gap in increment 1.3, found while reading P.11. The form has said *"Draft saved as REQ-2026-0001 — you can resume it later"* since 1.3 and the portal could not resume anything: the reference lived in React state and nowhere else, so leaving the page lost the contents of a draft that was sitting in the database the whole time. The promise was true of the row and false of the portal. F-UX-01 is a **Must** in ARCHITECTURE.md §10.2 and only half of it — the save — was ever built. P.11 raised the cost of the gap: asking the platform to work out a layout now saves a draft first, so drafts are created far more often than before, and a resumed one that came back without its placement would have silently forgotten the one decision on it that decides what gets built.
*Implements:* F-UX-01 (the resume half), F-UX-16.
*You'll know it works when:* you fill in half a request, save it, go to **My requests**, press **Resume this draft**, and get back everything you typed — including the layout you chose — and saving again updates that draft rather than making a second one.
*Built 2026-09-09.* A route (`#/request/resume/<ref>`), `getDraft` in `api.ts`, a load path in `RequestForm.tsx` that also sets `draftRef` so the next save **updates**, a **Resume this draft** button on draft rows in `MyRequests.tsx`, and `GET /api/requests/{reference}` now attaching the placement in force. That endpoint already existed, and its docstring already said *"so it can be resumed/viewed"* — the server half of this was written in 1.3 and never called.
*The placement is restored by its own effect, declared after the one that drops a stale layout, and the order is load-bearing.* Loading a draft sets the stack, the target and the tier in one go, which is exactly what P.11's drop rule watches; setting the chosen layout during the load would have it wiped in the same render by a rule written about somebody editing the form. Effects run in declaration order, so the drop runs and then the recorded layout goes back. Moving it earlier breaks resume silently — no type error, no warning, just a layout that vanishes on load.
*The cluster id is named by the API, not dug out of the topology by the browser.* Only `existing-cluster` deploys onto a cluster and its id sits on the hosts, because that is the document the orchestrator selects modules from. A browser that knew which option key means "this host id is really a cluster id" would be a second place holding that rule.
*Two defects in the draft-save path, both pre-existing and both reachable from a browser once references leave the page that minted them.* `POST /api/requests/draft` authorised the **role** and never the **owner**, so anyone who could raise a request could overwrite anybody else's by naming its reference; and `status = "draft"` ran unconditionally, so saving against a submitted reference pulled a request back out of the approval it was waiting for while its Jira ticket went on existing — the portal and Jira then disagreeing about whether it had ever been sent, which is the split §4 exists to prevent. Now 403 and 409, each saying which.
*Found by the tests, and it would have lost a field on every resume:* the tier vocabulary changed on 16 Aug 2026 and the form's dropdown never followed it. `normalise_tier` is liberal in what it accepts and strict in what it stores, so a draft saved as `uat` comes back as `UAT` — a value that matches no option in the control, which would have rendered blank and re-saved as whatever the requester picked instead. Translated on the way in, with the server still deciding what a tier is.
*Named rather than fixed:* that dropdown still offers `dev | test | sit | uat | preprod | prod | dr` against a stored vocabulary of `Development | QMG | Pre-Test | Test | UAT | Production`. Two of its options are folded away on save (`sit`, `preprod`), one — `dr` — matches nothing and is **stored as empty**, and `QMG` cannot be requested at all. That is a bug in the form's vocabulary rather than in resuming, and fixing it means touching every rule keyed on a lowercase tier; it is worth its own increment.
*Also not done, and deliberately:* reading a request by reference is unauthenticated for anyone signed in, and so is `GET /api/requests` with a `reference` filter — the portal-wide read model, not something resuming introduced. The **write** is now guarded, so a draft opened by guessing a reference cannot be saved over. Whether reads should be scoped to the owner is a decision about the whole portal and belongs to the reviewer, not to this increment.

**P.12 — Topology summary in the approval ticket**
The Jira payload carries a human-readable topology summary — what runs where, on how many hosts, in which cluster — beside the post-placement cost, so the approver judges configuration and cost together rather than a component list.
*Implements:* F-INT-13.
*You'll know it works when:* a Jira ticket for a consolidated request reads as an architecture and its cost matches the post-placement estimate.
*Built 2026-09-08.* `build_topology_summary` in `api/jira.py`, read from the RECORDED placement rather than recomputed — deriving the topology again at submit could produce a third answer, and a ticket that disagrees with the record is worse than one that says nothing. A request with no placement produces a byte-identical ticket to before.
*The second half of "its cost matches" was not cosmetic.* `req.estimate` is signed into the orchestrator handoff as `approved_monthly`, and the orchestrator re-validates the real cost against it before building — and it builds the **placement**. Showing the post-placement figure in the ticket while leaving the per-component figure in `req.estimate` would have had the portal enforce a number nobody was ever shown, and refuse a build for a discrepancy it created itself. So a placed request now carries ONE cost through the budget guardrail, the stored estimate and the ticket: the placement's.
*What that required:* `estimate_placement_cost` now returns the same shape `estimate_cost` does — `by_category`, `unpriced`, `provisional`, `external_licences`, `lines`, `deployment_target`. Every one of those fields is in the ticket because an approver was once misled without it (the licence split, the "not the whole cost" caveat, the foreign-currency line), and substituting a thinner dict would have silently dropped all three while appearing to work. Components on a host P.6 refused to size are named in `unpriced` directly — the sub-estimates cannot report them, because an unresolved host is skipped before anything is priced, and without that the total would have looked complete.
*Two more defects the swap exposed, both fixed.* The approver's PDF and Excel cost sheet pair `sizing["components"][i]` with `breakdown["lines"][i]` **by position** — safe while a component and a priced line were the same thing, and wrong the moment the lines price machines. Three components on one host produce one line, so the PDF would have printed a machine's monthly figure beside a component's name: a per-component price nobody calculated, in the document handed to the person approving the money. `placement_attachment_rows` rebuilds those rows as machines, in the order the lines were priced. Separately, `machine_count` counts only hosts that could be SIZED, so a placement whose machines all failed to size printed *"0 machines are provisioned by this request"* directly beneath a line naming one.
*Also:* `OPTION_TITLES` in `api/placement.py` is now the single source for a layout's name. Only the KEY is persisted with a placement, so the ticket has to turn "consolidated" into something a person can judge — and an approver and a requester describing the same layout differently is drift nobody notices until the two are read side by side.

**P.13 — Terraform module selection from the topology**
Module selection and variable generation derive from the persisted structured placement. Placement reaches the orchestrator as data, never as prose.
*Implements:* F-ORC-11.
*You'll know it works when:* the same components with different placements produce different plans, and the plan can be traced back to the placement version that produced it.
*Built 2026-09-09.* The placement travels **inside the signed handoff body** as a list of hosts, each with its shape and its own resource kind — so the layout cannot be altered between the approval and the build, and the orchestrator re-derives no catalogue facts (ARCHITECTURE.md P7; the far end is the one holding the cloud credentials). `/provision` answers with `placement_version`, `placement_option` and the workspaces it planned, which is the traceability half.
*The unit of work changed, and this is the substance of the increment.* It was a RESOURCE KIND, which was right until a placement could put two machines on one kind: "separated" builds three machines that are all `oci-instance`, and one workspace per kind collapses them into a single VM — the portal building something other than what was approved and reporting success, the failure this phase exists to end, one layer further down. The unit is now one machine per host, sized from the placement rather than from the largest component, because three co-resident components need the SUM.
*The state-layout decision, which is where the real risk was.* A workspace stays named `<kind>` while a kind carries one host — every request built before this, its directory and its Terraform state untouched — and becomes `<kind>__<host id>` only when a placement gives that kind more than one. The kind stays at the FRONT because destroy deliberately sweeps up workspaces the portal did not name, and has nothing but the directory name to go on: get that wrong and teardown reaches `_module_dir`, `_timeout_for` and `_cloud_vars` with a string none of them recognise, so a cluster that takes twenty minutes to delete gets the ten-minute default and times out mid-teardown with the resources still live and still billing. Destroy and drift now map the name back through `kind_of_workspace`.
*Nothing is built at a shape or from a module nobody approved.* A host P.6 could not size, or one no certified blueprint builds, REFUSES the handoff rather than falling back to a default — the first was excluded from the cost the approver signed, and the second is how a request for Python 3.12 once received an empty bucket.
*A defect this increment would have introduced, caught by a failing test.* Converting vCPUs to OCI's OCPUs with `round()` turns 5 vCPU into 2 OCPUs, which is 4 vCPUs. P.6 adds headroom and deliberately rounds UP for exactly this reason, so the machine would have been built smaller than the figure the approver signed, with the headroom spent undoing itself. It rounds up now, with a test sweeping 1–32 vCPU asserting a machine never gets fewer than the placement resolved. The older `_instance_sizing` used `round()` too and was safe only because every sizing anchor in the catalogue has an even vCPU count. **Fixed 2026-09-09**: it rounds up as well, so a requester who types an explicit odd vCPU into the component detail form gets the machine they were priced for rather than one OCPU less. The concern that held this back — that changing it would alter the shape of machines already running, making a drift check report drift on environments nobody touched — turned out to be theoretical: every explicit vCPU in the whole request history is 2 or 4, so `round` and `ceil` agree on all of them and no existing environment moves. A test asserts the anchors really are all even, so the day an odd one is added the no-op claim stops being quietly false.

**P.14 — End-to-end integration test**
Select three components → resolve consolidated → summed sizing → correct cost → correct Jira payload shape, as one test.
*Implements:* the phase.
*You'll know it works when:* the test passes from a clean database and fails if any link in that chain is broken.
*Built 2026-09-09.* `api/tests/test_placement_end_to_end.py`. One test walks the whole chain on a freshly seeded database: three components → the three layouts → consolidated resolved at version 1 → one host summed to 6/20/250 and, with 20% headroom rounded up, built at **8 vCPU / 24 GB / 300 GB** → a cost derived from that shape and stored as the approved figure → a ticket reading *"Layout: Consolidated … 8 vCPU, 24 GB RAM, 300 GB disk … 1 machine is priced below"* at the same figure → the layout inside the signed handoff → one Terraform workspace at 4 OCPUs. The same three components resolved as *separated* build two machines at 5/20/240 and 3/5/60, in two workspaces.
*"And fails if any link is broken" is asserted, not assumed.* Six links are severed in turn — each replaced with a plausible EMPTY answer rather than an exception, because that is how these failures have actually presented — and the chain test must notice. A cut the chain still passes is not a passing test; it is a link the file does not really check.
*That clause immediately earned itself.* Two of the six cuts were ineffective and reported as caught: `api/main.py` does `from api.sizing import load_requirements`, so it holds its own binding and patching `api.sizing` replaces a name nobody reads. The two links written that way were the two carrying the NUMBERS — sizing and cost — so the chain test was claiming to check the arithmetic and checking nothing. Every link is now severed at its point of use. This is the same class of mistake the phase kept finding in the code, found this time in the test written to catch it.
*A defect this increment did not catch, found beside it and left for its own increment.* **The ticket's Terraform plan preview is not placement-aware.** `build_plan_preview` resolves per component and knows nothing about hosts, so a consolidated request whose placement section reads *“1 machine … 8 vCPU, 24 GB RAM, 300 GB disk”* carries, in the same ticket, a preview listing three modules at 2, 4 and 2 vCPU — and P.13 made the orchestrator build from the placement, so the preview describes something that will not happen. Two contradictory accounts of what gets built, in one document, in front of the person approving the money: the same class of defect P.12 fixed in the other half of that ticket. The chain test above does not assert it either way, because a test asserting a defect holds reads as endorsing it. Found while a second end-to-end test was being written in parallel; that test was dropped as a duplicate of this one and the finding kept.

---

**Phase P is complete** (2026-09-08 – 2026-09-09, 15 increments). Eight defects were found in code that already had passing tests, every one of them exposed by a new CONSUMER reading an existing output rather than by review: P.11 found two, P.12 three, P.13 three. The pattern is worth carrying forward — a component with green tests and no caller is not finished, and the increment that first reads its output is where the cost of that lands.

---

**Out of scope for this phase**, per the task: provisioning execution, Jira workflow changes and approval logic. The phase ends when a resolved, costed, persisted topology is attached to a submitted request.

---

## 5b. Phase D — The topology a requester can see and change (added 2026-09-10)

Raised from a screen. A request for OKE plus Apache produced two options, both
refused, both reporting *"cannot be sized: no requirement is recorded for apache
as container"* — and nothing the requester could choose. The question asked of it
was "what is the purpose of this functionality?", which is the right question to
ask of a screen that answers nothing.

**The defect underneath.** The cluster options placed EVERY workload on a
`container` host without asking whether the component runs as one. Apache and SQL
Server are `vm` in this catalogue, so the resolver was proposing a layout the
catalogue already says is impossible, and then reporting the missing sizing row
as though the data were at fault. Worse, the cluster branch returned ONLY cluster
options, so Apache — which does need a machine — had nowhere to go.

**Fixed 2026-09-10.** A cluster is only hosting something if the portal can put
something on it, and it cannot. So a cluster provider is placed as the managed
service the catalogue already calls it, every selection has a layout that
BUILDS, and the cluster options appear first, refused, with the reason — present
because a requester who asked for Kubernetes must learn why their workload is not
going on it, but no longer the whole reply.

    OKE alone       managed: oci-oke run by the cloud                    priced
    OKE + MS SQL    managed: oci-oke by the cloud, mssql on a machine    priced
    OKE + Node.js   the two cluster refusals, then the same managed      priced

*One earlier decision reversed deliberately.* P.5a asserted that "one machine or
three" is not a question about a Kubernetes request. That held while the
workloads were going on Kubernetes. They are not — so they are going on machines,
and how many machines is exactly the question. The test now says the opposite,
with the reasoning.

**D.1 — Evaluate a layout the requester arranged** — ✅ built 2026-09-10
`POST /api/placement/evaluate`. The browser proposes hosts-and-components; the
server judges it with the same OPA evaluation, sizing and pricing the enumerated
options get, and persists nothing.
*Implements:* extends F-UX-16, F-GOV-12.
*The property that makes it safe:* a layout may REARRANGE what was requested and
may never CHANGE it. A body placing `oracle-db` into a request for PostgreSQL is
refused, because everything downstream — sizing, cost, the approval ticket,
Terraform — would faithfully build what the body said. P.9's rule that a request
body cannot describe a placement into existence was written before there was any
way to want this; both hold because the arrangement comes from the browser and
every fact about whether it is buildable is re-derived server-side.
*Also refused:* a component on a host mode it does not support, a component
placed twice or not at all, an empty host, two hosts sharing an id. Every
complaint comes back rather than the first, so a requester who moved two things
is told about both.
*A malformed layout is never sent to the policy* — asking OPA about a topology
that cannot exist returns a verdict that reads as a policy decision rather than
as the malformed proposal it is.
*One thing to watch in D.3:* the response also carries what the platform's own
cheapest layout costs, which means a full enumeration per call. Fine for a button;
worth revisiting when every drag triggers one.

**D.2 — The topology as a diagram, read-only** — ✅ built 2026-09-10
`TopologyDiagram.tsx`, on request per option ("View as diagram"). Hosts drawn as
boxes with their mode, id and shape; components as blocks on them.
*Drawn in DOM elements rather than SVG, deliberately:* D.3 makes this surface
draggable, and drop targets, keyboard focus and live re-evaluation are all things
the DOM gives for free. An SVG version would look identical and be thrown away.
*The honesty rules carry over from the option cards*, because a picture makes it
easier to imply something untrue, not harder: an unsized host is drawn AS
unsized rather than as a box of zeros; a managed service is drawn dashed and says
no machine is provisioned; a refused layout is still drawn, with the reason,
because seeing the arrangement you cannot have is how somebody works out what to
change.

**D.3 — Arrange it yourself** — ✅ built 2026-09-10
`TopologyEditor.tsx`. Drag a component onto another machine, add a machine,
remove an empty one; every change re-evaluates through D.1 and re-prices, with
the delta against the platform's own cheapest layout.
*A refused arrangement is KEPT ON SCREEN.* This is the decision the component is
built around and the obvious implementation gets it wrong: snapping the block
back is what most drag-and-drop does, and here it would erase an intention the
requester was expressing — "these two belong together" — and say nothing they can
act on. The arrangement stays as they left it, each reason sits BESIDE the block
it names rather than in a banner they have to map back onto what they just did,
and Undo is there for when the answer is to put it back.
*Dragging is not the only way in.* Every block carries a "Move to" menu, so the
feature works by keyboard and with a screen reader. A capability reachable only
by dragging a mouse is one some colleagues do not have.
*Choosing it is a write, and it is re-judged.* `/api/placement/resolve` accepts
the `custom` key with the arranged hosts and runs the SAME validation and policy
evaluation `/evaluate` ran — one function, called by both, because a slightly
different check on the write path is how a layout passes on screen and is refused
on save, or worse the other way round. A smuggled component is refused there too:
nothing forces a browser to call `/evaluate` first.
*The arrangement is dropped when the stack changes*, because it describes
components the request may no longer contain — the server would rightly refuse
it, after the requester had already pressed submit.

---

## 5c. Found work — defects met while doing something else (added 2026-09-11)

Not a planned increment. Found while asking why the full suite took 1h44m on
2026-09-10 and 18m10s the next morning, against 9m a fortnight earlier, with an
identical pass count every time.

**H.1 — the policy gate stops building a client per call.** *Done 2026-09-11.*
`httpx.post(...)` is a convenience wrapper that constructs an entire client for
one request and discards it: a connection pool, and an SSL context loaded from
the CA bundle on disk, built whether or not the URL is https. Measured here:

| | per call |
|---|---|
| `localhost`, new client each call (what we had) | 804.8 ms |
| `127.0.0.1`, new client each call | 772.8 ms |
| `localhost`, one reused client | 47.5 ms |
| `127.0.0.1`, one reused client | 3.1 ms |
| *(client construction alone, no network at all)* | *318.1 ms* |

*Two costs, and the order matters.* Constructing the client dominates, so
changing the hostname alone buys almost nothing — the first guess, and the wrong
one. Once the client is reused the hostname becomes the larger remaining cost:
`localhost` resolves to both `::1` and `127.0.0.1`, and on Windows the address
Docker did not publish on is tried first. Both changes together, or neither is
worth much.
*Mostly a development-machine problem — measured, after an overstatement.* The
first write-up of this said that because 318ms of the cost is not network cost,
the containers must be paying it too. Measured inside the rebuilt `api`
container against the `opa` service, they are not:

| inside the container | per call |
|---|---|
| `httpx.post` per call (what we had) | 5.5 ms |
| shared client (what we ship) | 1.6 ms |

The client-construction cost is largely a Windows cost; on Linux the CA bundle
read is cheap. So this is a large win for development and the test suite
(804ms → 3.1ms) and a small one in production (5.5ms → 1.6ms — real, 3.4x, but
milliseconds). Worth having, and worth not overselling: the reason to keep it is
that building a client per request is wasteful everywhere, not that it was
costing the containers a third of a second. The commit message for `c1658bc`
carries the original overstatement and cannot be corrected in place.
*The timeout stays with the caller.* Every call site still passes its own
`timeout=`, so a slow dependency is bounded by the same number as before. A
shared client must never become the place where one caller's patience quietly
becomes another's — `common/tests/test_one_client_per_process.py` holds that.
*The orchestrator was converted whole, not half.* Its tests stub one function
that dispatches on URL across both OPA and the API, so converting only the OPA
calls would have meant stubbing in two places to answer one question.

**H.2 — the suite reached the public internet.** *Done 2026-09-11.*
`api/registry.py` resolves images against a real container registry over
`urllib` with an 8-second timeout, and nothing pinned it because there was
nothing to pin: it was the one adapter in the portal with no mode.
`test_asking_creates_nothing` made four such asks and took 68.9s — the single
slowest test in the suite, and H.1 did nothing for it. It is now **2.45s**.

*I called this a decision and it was not one.* The judgement I offered was
"pin a mock and lose the proof that we can really resolve an image". The
convention was already written down, in `test_vault_live.py`: "These tests mock
the HTTP layer so the suite stays offline; the real proof is a separate,
documented verification." Every other adapter follows it. The registry was the
exception, not the precedent.
*`REGISTRY_MODE` is `live` by default*, unlike every other mode, because
production must ask a real registry — the entire point of the module is that
nobody hard-codes what an image is. A mock default would quietly turn every
deployment into "no published image".
*The guard sits at the transport, and tests that replace the transport opt back
in.* First attempt put it at the top of `_get`, which refused eight tests that
substitute the socket precisely so they can exercise the real `_get` — the file
says so in a comment. They now set `REGISTRY_MODE=live` themselves, which is
honest: the code path under test is the live one, against a fake socket.

**The blind spot, closed rather than patched.** `conftest.py` pinned six modes
while the code read seventeen. The ten missing ones default to `mock`, so
nothing failed — until the day somebody's `.env` set one to `live`, which has
now happened twice (`CLOUD_STATE_MODE` on 2026-08-25, and this). All seventeen
are pinned, and `api/tests/test_the_suite_stays_offline.py` scans the source for
every `*_MODE` the code reads and asserts each is pinned in the environment the
tests actually run in. It keeps no list of its own — a second list is what
drifted — and it asserts the scan finds something, because a source-reading
check that stops looking goes green on the day it breaks.

**The lesson, again.** *A check that validates what it looks at cannot see what
it doesn't.* `conftest.py` pins auth, Jira, pricing, provisioning, cloud state
and the database to mock, and reads as though it has covered everything — it
covers what someone thought of. OPA and the registry were never in the list, so
the suite has always reached out to both, and nothing said so.

**H.3 — a held request quoted the price it was approved at.** *Done 2026-09-11.*
Found in a screenshot of REQ-2026-0305. The cost guard had worked perfectly:
minio could not be priced when the request was approved, it was certified
automatically while the request was in flight, the real total came to 1141.73 a
month against 916.13, and the portal refused to build at a cost nobody had
agreed to. Then the screen said both numbers at once — the Monthly column read
916.13 under a header that just says "Monthly", and the banner directly beneath
it read 1141.73.

*The browser could not have done better.* The list sent `{currency, monthly}`
and the real figure existed nowhere structured: it was interpolated into an
English sentence in `status_detail`. Parsing a price back out of prose in the
browser would have been wrong twice over — the client holds no pricing logic,
and prose is not an interface.
*Both numbers stay, because they are different facts.* `estimate` is what was
APPROVED and does not move: F-FIN-01 measures actuals against it and it records
what somebody said yes to. `repriced` is what the request would cost now. The
bug was having a field for only one of them. Overwriting the estimate until the
screen agreed would have destroyed the record of the agreement — the fiction
REQ-2026-0176 was approved on, running the other way.
*Read, never recomputed.* The figure comes from the guard's own
`cost.reapproval_requested` audit entry, which is append-only and hash-chained.
Re-pricing at read time would let the cell drift from the sentence beneath it
the moment a catalogue price moved — the same defect, rebuilt somewhere new.
*No schema change.* `Estimate.request_id` is `unique=True`, so a request has
exactly one estimate row by database constraint, and there is no migration tool
— `create_all` adds tables and never columns. The audit log already held the
number, so nothing had to be added to hold it again.
*It appears only where it is true.* Attached for `cost-changed` rows only, so a
cancelled request stops quoting a monthly cost it is never going to incur, and
batched into the list query because that list is polled every few seconds.

**H.4 — the column enforces its own width now.** *Done 2026-09-11.*
Raised as a thin margin: `status_detail` is `String(500)` and the cost guard's
sentence is 408 characters. The margin was fine. Two other things were not.

*A trim written against the wrong number.* `api/main.py` cut the cancel reason
to `[:2000]` for a `varchar(500)` column. A long enough reason passed the trim
and would then have been refused by PostgreSQL, rolling back the cancel —
written by somebody guarding against exactly this and picking the wrong limit.
*The guard moved to the model, where no write site can get it wrong.* There are
forty-four places that set `status_detail`. `Request._fits_the_column` reads the
column's own width, so widening it needs no second edit — and that second edit
is the one nobody makes.
*The two columns are treated differently on purpose.* `status_detail` is prose
for a person, so it is TRIMMED, visibly, with an ellipsis: losing its tail is a
far smaller harm than losing the write that carries it. `status` RAISES — a
truncated status is a value nothing in the state machine handles, and writing
one quietly would reproduce the 2026-09-09 incident through its own fix.
*And the tests now meet the limit production does.* SQLite does not enforce
VARCHAR, which is why both previous incidents passed a full suite. The check
happens in Python.

**What it found on its first run, which is the point.** The last live use of
`decommission-failed` — the nineteen-character value behind the original
incident — was still in `test_a_failed_request_does_not_block_a_retry_forever`,
building a request in a state production cannot produce. A test named for the
recovery path was proving nothing about it. It now uses `teardown-failed`, which
is what a failed decommission actually gets.

**Three layers of one fault, each fix blind to the next.** Production wrote a
19-character status into `varchar(16)`; the first fix was an AST scan over
`api/*.py`, which never looked at tests; the model now enforces the width for
both. *A check that validates what it looks at cannot see what it doesn't* —
fifth entry.

**H.5 — the editor discarded a drag it had already made.** *Done 2026-09-11.*
Reported from a screen: "When I moved the DB to a new VM it is not actually
doing." The drag HAD registered. `move()` took the component off its block and
never removed a block left empty, so the server refused the whole arrangement —
"Host 'managed-postgres16' carries nothing. It would be built and billed for
nothing." A refusal naming a host the requester had not touched read as nothing
having happened. "Add a machine" had the same fault in reverse: it created an
empty host, so the layout went refused the instant it was clicked, before
anything could be dropped on.

*An empty block is somewhere to drop things, not a machine*, so it is left out
of the proposal. The server's rule is untouched — an empty host that does reach
it is still refused, and a test holds that — because the two are different
things: what the browser proposes, and what the platform permits.
*Dragging out of the cloud is no longer a one-way door.* A managed block is the
cloud running one named component; nothing else may be dropped onto it, but the
component it was created for may come back. Without that, the only way to undo
the move was to undo every change made since it.

**H.6 — "it is not allowing me", with nothing to read.** *Done 2026-09-11.*
Same screen, different problem: a requester wanted Vault and Oracle running in
their OKE cluster, and found no control for it, no refusal, and no explanation.
The editor only ever makes machines and never said so.

*The sentence shown is the SERVER'S*, lifted from the cluster layout it already
refused and returned in the option list. Not written in the browser: the client
does not know why the platform cannot deploy into a cluster, and a copy of that
reasoning in the client is one that goes stale the day the route exists. Found
by host mode rather than by option key — the mode is the fact; a key is a string
the browser would have to keep in step with the server.

**The capability itself is still blocked, in three places.** Worth writing down
because it was asked for directly, and because adding any one of them alone
would re-create the promise this project already withdrew once:

  1. the editor can only make `vm` hosts, so the arrangement is not expressible;
  2. `host_mode_requirement` holds eight container rows in total, all
     `postgres16` — `vault` and `oracle-free` are vm-only;
  3. the orchestrator refuses container hosts outright: "Nothing in this system
     deploys into a cluster (private API endpoint, no route from here)."

(3) is the binding one, and it is §6's open decision, not a coding task. Adding
the catalogue rows for (2) would not unblock anything — it would offer a
placement the orchestrator then refuses, which is exactly the defect removed
when Scenario B was made to stop promising what it cannot do. *Note the
distinction:* **a container ON A MACHINE already works** — RabbitMQ ships that
way today, podman on the VM, pulled by digest — but the requester still receives
a Linux VM, explicitly "not on a Kubernetes cluster". That is not what was asked
for.

**H.7 — the drag became testable, after H.5 shipped broken.** *Done 2026-09-11.*
H.5 was reported fixed and was not. Asked to verify it rather than assert it,
two things came out.

*The build was broken and I had not run it.* `import { move as moved }` collided
with the component's own `moved` state — the code most recently dragged — and
the local shadowed the import, so `tsc` refused it: "This expression is not
callable." The image the requester was asked to try was the previous one. The
typecheck I had run was against the commit BEFORE the edits that broke it, and I
did not re-run it after.
*There was a second cause of "it does nothing", and it was never the drag.* The
panel renders above the list of offered layouts, so pressing "Arrange it
yourself" from a card further down the page opened it off the top of the screen
and left the requester where they were. It now scrolls itself into view and
takes focus, so a keyboard or screen-reader user gets the same answer as a
mouse user.

**Why neither was caught.** The arrangement rules lived in closures inside
`TopologyEditor`, and this frontend has no test runner, no jsdom and no way to
fire a drag event. "Does dragging the database onto its own machine work?" could
only be answered by opening the page and trying it — which is how it reached a
requester twice, and why the answer both times was a guess.

The rules now live in `src/components/topologyArrangement.ts` as plain functions
over plain data, and `webapp/frontend/tests/topology-editor.render.tsx`
exercises them directly alongside a server render of the panel in every state it
can be in — including the unpriced layout the second report arrived in, because
a requester whose layout cannot be priced is exactly the one who needs to
rearrange it. Twenty-five checks, run the same way the placement-step harness is
run, from PowerShell rather than Git Bash.

*The rules hold no authority and must never grow any.* They say what the
requester ARRANGED. Whether an arrangement may be built is still answered only
by `/api/placement/evaluate`.

---

## 5d. Phase U — one workspace, one question (added 2026-09-11)

Asked for after a screenshot of the placement step: "I am confused, why so many
choices are provided. I need just one window or workspace to decide my topology
by user choice arrangement."

**U.1 — the five cards become two routes.** *Done 2026-09-11.*
The step rendered every layout the platform produced as its own card — five for
a stack with a cluster in it, each with a title, summary, price, refusal, its own
advisory notices and its own diagram. Two of the five printed the SAME sentence
about the Kubernetes API being unreachable, at full size, one above the other.

*They were never five choices.* They were two — machines or Kubernetes — and
three variations on the first that the platform is better placed to pick than
somebody asking for a database. So: the stack, one line on cluster availability,
two route cards, and the topology drawn for whichever is chosen, redrawn when it
changes. The machine layouts collapse to the platform's recommendation (the
cheapest that can actually be built), with the rest behind a disclosure and
"Arrange it yourself" for anyone who wants something else entirely.
*The route is the SERVER's fact.* `Option.route` is derived beside the option
keys, where the keys are defined, and the browser groups by it. A list of cluster
keys kept in the client is one that goes stale the day a layout is added, and the
requester is the one who finds out.
*Nothing disappears to achieve the simplification*, and the render harness
enforced that within minutes of the first attempt: dropping the refused layouts
made it fail with "refused option not rendered at all". Every layout is
accounted for — on a route card, or in a compact "Also considered" line carrying
its reason. A layout that vanishes is indistinguishable from a portal that is
broken.
*No node pools, subnets, NSGs, image OCIDs, Kubernetes versions or storage
classes* anywhere in the workspace. Those were never asked of the requester and
still are not.

**What the harnesses caught, which is the point of having them.** The fixtures
predated `route`, so the workspace grouped nothing and the suite passed on a
render showing no route cards at all — twice, because the harness imports
fixtures from INSIDE the image and the first rebuild came after the recapture.
`webapp/frontend/tests/refresh_fixtures.py` now recaptures them from the real
endpoint against a seeded catalogue and the real OPA, so nobody hand-edits a
fixture and calls it a capture. A third failure was the harness's own regex:
`/0\s*vCPU/` matched the zero in "10 vCPU", and only fired once the fixtures
carried real sizes.

**U.2 — the Kubernetes route can be chosen.** *Done 2026-09-11.*
`api.placement.cluster_deployment_offered()` is a switch, default ON, and with
it on both cluster layouts resolve, price and can be recorded. A new-cluster
layout carries TWO hosts — the cluster as the managed service the cloud runs,
and the workloads as containers on it — because one combined container host
would price the control plane as a workload and lose the cluster from the
picture the requester is shown.

*What ON costs, stated before it was chosen and again here.* A request that
picks Kubernetes passes approval and then FAILS AT PROVISIONING. Asked whether
to offer the route, grey it out, or build as though the network route existed,
the platform owner chose the third with that consequence on the table. The
switch is how it is reversed without another code change, and how it becomes
truthful rather than optimistic the day the route is opened.
*The orchestrator's guard did not move.* Removing it does not make the cluster
path work — it makes a container host resolve to `oci-service-vm` and build a
lone machine with no cluster while reporting success, which happened once
already. Its refusal now names why the layout was offered and what makes it
buildable.
*Entitlement woke up.* While nothing could deploy anywhere, which cluster a
requester may use decided nothing, so `api.clusters` sat dormant with a test
saying so. It is now the only question left: a cluster the requester is not
entitled to, or one over quota, refuses the layout with its own reason rather
than being discovered at the cluster.
*No invented sizing figures.* `HOST_MODE_BASELINE` is deliberately per (host
mode, size) rather than per technology, and already carried container rows. What
changed is which technologies DECLARE a container form: software now declares
both, because "PostgreSQL can run as an image" is a fact about the software and
"the platform can deploy it" is a separate question answered separately.
*The twelve tests asserting the old refusal were not deleted.* They describe the
switch OFF, which is still exactly true there, and now pin it; a new section
covers ON, including that ON is the default.

**H.8 — the nine technologies that could not be sized.** *Done 2026-09-11.*
Nine
technologies — gitea, grafana, haproxy, mariadb, memcached, minio, prometheus,
traefik, valkey — are in the live `technology` table and in `DELIVERY` as
software, but missing from the seed's `TECHNOLOGIES` list, so the derivation
skips them and **they have no host mode at all**. They cannot be sized on ANY
route. That is what a requester saw as "Size not determined — no requirement for
minio" beneath a VIRTUAL MACHINE.

Defaulting their clouds was tried and reverted the same hour:
`test_host_modes_are_only_offered_on_clouds_the_technology_supports` refused it,
rightly, because a technology absent from `TECHNOLOGIES` has no declared clouds
and every cloud is then a guess.

**I then read the defect wrong, and the correction is the useful part.** They
were recorded here as "certified with `resource_kind` still on the `oci-bucket`
DEFAULT nobody set, which says half-created rather than proved", and offered as a
decision: withdraw them, or offer them properly.

There was no decision. All nine carry CERTIFIED BLUEPRINTS on `oci-service-vm`
and real certification proofs in this database — minio 5, grafana 5, gitea 4,
mariadb 4, memcached 3, valkey 3, prometheus 2, traefik 2, haproxy 1. They were
proved on real machines through the agent's own ladder. The `oci-bucket` I read
as evidence sits on `technology.resource_kind`, a column whose own docstring says
it "exists and is wrong for most components" and which nothing consults for this;
the blueprint carries the real kind. A default value was mistaken for a finding.

*The fix is nine lines*, adding them to `TECHNOLOGIES` so `_derived_host_modes`
stops skipping them. No new shapes: `HOST_MODE_BASELINE` is per (host mode, size)
and already carried every row they need. **A reseed is required** for an existing
database — the seed inserts what is missing and touches nothing else.
*Two seed tests pinned `48` technologies and `192` anchors as literals*, so
adding technologies they were not about broke them. They now assert
`len(TECHNOLOGIES)` and `len(TECHNOLOGIES) * len(SIZES)` — the relationship,
which is the property worth holding.

**U.3 — where a request has got to.** *Done 2026-09-11.*
Asked for as a picture of the pipeline: Agent, Blueprint, Terraform, Validate,
Plan, Security/Policy, Provision, Verify, Ready. Every one of those already
happened. What was missing was anywhere to see it, so a request sitting at
`in-progress` told its owner nothing about whether the agent was building a
recipe, whether Terraform had planned, or whether it was stuck.

*Nine stages, each mapped to something the system really records* — the
append-only audit trail, which already carried `autobuild.started`,
`orchestrator.handoff`, `plan.previewed`, `provisioning.started`, `apply.failed`
and the rest. `GET /api/requests/{ref}/progress` derives them from the request's
status and that trail. A test checks every stage is keyed on an event
`api/main.py` actually emits, read from its own source rather than from a list
kept beside it: a stage keyed on an event nobody writes would sit `pending` for
ever and look like a hung pipeline.
*IT DOES NOT INVENT HISTORY, and that is the whole point.* A progress view is
believed. One that fills itself in because a status looks advanced would tell
somebody their infrastructure was verified when nothing verified it — and this
portal has already shipped a request marked `provisioned` that had built an
empty bucket. A stage is `done` because the trail says so and names when; where
the trail is silent it is `done` with NO TIME rather than a plausible one. The
captured fixture shows exactly that: "Terraform plan — done — (no time
recorded)".
*"It did not happen" and "it has not happened yet" are different answers.* A
manually fulfilled request never reaches Terraform, so that stage is `skipped`
with the reason rather than failed or pending. A cancelled request sits on no
step at all — saying "in progress" about something that has stopped is how
somebody waits for a build that is not coming.
*Derived on the server, drawn in the browser.* A pipeline assembled from a
status string in the client would be a second opinion about history when the
audit log is the first one. The endpoint is read-only by construction: it loads
a request and its trail and returns a list, writes no audit of its own, and a
test asserts that asking three times moves nothing.
*The stage list was extracted so it could be tested* — the lesson from H.7,
where the behaviour lived inside a component and "does it work?" could only be
answered by opening the page.

**U.4 — a pod is not a shared machine.** *Done 2026-09-11.*
Found by walking a real request rather than a fixture. REQ-2026-0306 — OKE with
Vault, Oracle and PostgreSQL — had its Kubernetes route refused: "oracle-free
and postgres16 may not share a host."

*The policy rule is right and was not touched.* Two databases on a machine
compete for page cache and disk queue, and one patch takes both down. What was
wrong was the shape being judged: the cluster layout put EVERY workload on one
container host, so the rule fired against a topology that does not exist. On
Kubernetes they are separate pods with separate limits.
*`_cluster_hosts` now emits one container host per workload* — `cluster-postgres16`,
`cluster-oracle-free` — used by both cluster layouts, refused or not, so the
topology is drawn the same shape either way. Sizing and price are unchanged in
substance: same components, same per-component shapes, summed the same way.
Each is simply asked for on its own, which is what the cluster would do.

**U.5 — a pod stops being counted as a machine.** *Done 2026-09-11.*
`api/sizing.py` does `machines += 1` for every non-managed host, so a cluster
with two workloads now reports "2 machines" where it reported "1". Neither is
right — the portal provisions no machine for a pod; the cluster is a managed
service — but this change made the existing conflation louder rather than
introducing it.

*Counted apart in all four places that say it.* `api/sizing.py` counts only `vm`
hosts as machines and reports `container_count` beside it; `Option.host_count`
agrees, or the same layout would be described two ways on one screen. Both still
contribute their shape to `totals`, because both consume capacity somebody pays
for.
*The delicate one was the document an approver signs.* Counting correctly and
changing nothing else would have printed "0 machines are priced below" directly
above three priced pods — the exact contradiction that paragraph in `api/jira.py`
already fought once. It now reads "2 containers are priced below", or "1 machine
and 1 container are priced below", with a line saying no machine is provisioned
for a container. A machine-only layout reads EXACTLY as it always did, and a
test pins that: the wording change must not leak into the layouts every request
has used until now.
*One sentence, one place.* `whatGetsBuilt` in `api.ts` is used by the placement
card and the request summary rather than two copies drifting apart.

**A flaky test of my own, found by the suite rather than by luck.**
`test_the_time_shown_is_the_first_one_not_the_retry`, written earlier the same
day and shipped in `a5439fb`, failed about one run in three: `append_audit`
stamps `datetime.now()`, and two entries written microseconds apart can land on
the same timestamp, so the assertion compared a value with itself. The equality
— the time shown is the FIRST entry's — is the real property and always held;
the inequality now runs only when the clock actually ticked. A flaky test is
worse than no test, because it teaches people to re-run rather than look.

**Still to come.** Nothing in Phase U. The open items are the network route
(§5a, "what would widen this again", item 1),
the nine half-created catalogue rows above, and H.4.

**H.9 — a reason that stopped mid-word, and lost the way out.**
*Done 2026-09-11.* REQ-2026-0312 was held up showing "...so building this would
provision machines instead of th". Reported as "can you fix this for me".

*Two faults, and the second mattered more.* `_short_reason` cut at exactly 300
characters wherever that fell — against a 500-character column, so it was also
throwing away two fifths of the room the record has. That limit was right for
the wall of terraform output it was written for, and wrong for the
orchestrator's refusals, which are sentences composed for a person. It now takes
its limit from the column (H.4's lesson: read the width, do not remember it),
stops at a sentence where one is near the end, never mid-word, and marks the cut.
*And those sentences put the remedy last, which is what a trim eats.* The
refusal was reordered so what to do survives: consequence, then remedy, then the
placement detail — in the order of what can be afforded to lose.
*The switch's name is deliberately absent from it.* A requester reading a held-up
request cannot set an environment variable, and naming one says their request
failed on a setting rather than on a missing network route.

**What the reorder broke, and what caught it.** Leading with the remedy dropped
"would provision machines instead of the cluster that was asked for", and
`test_the_refusal_says_a_machine_would_have_been_built_instead` failed. Its
docstring says why that phrase exists: a reader told only "unsupported" does not
learn the alternative was silently WRONG rather than absent — which is what
REQ-2026-0144 was, an empty bucket reported as success. The final message is 451
characters and keeps all three.

*The new test calls the real refusal rather than a copy of it.* A literal would
have gone on passing while the actual message drifted past the column, which is
the failure being tested.

---

## 5e. Phase K — deploying into the cluster, from inside the VCN (proposed 2026-09-11)

> **WITHDRAWN 2026-09-12, in favour of the network route (§6.1).** The premise
> below turned out to be wrong and the correction cost the phase its whole
> reason for existing: it was the option that needed no network change, and it
> needs one. Kept rather than deleted because the reasoning is the useful part —
> and because the next person to notice that an NSG admits a whole VCN will have
> the same idea.
>
> **THE PREMISE BELOW IS WRONG, FOUND 2026-09-12 BEFORE THE PROBE WAS RUN.**
> Half of it holds and half of it was an inference I never checked. See
> "What the tenancy actually says" immediately below before reading the rest —
> K.2 onwards does not stand up as written.

**NOT APPROVED. Nothing below is built except K.1.** Written after REQ-2026-0312
was held up and the blocker turned out to be narrower than a year of comments
claimed.

**What changed the picture.** The OKE module's own security group already admits
TCP 6443 from `operator_cidr`, and `operator_cidr` is
`data.oci_core_vcn.provided.cidr_block` — the WHOLE VCN. There is no missing
firewall rule. Anything inside that VCN can already reach the Kubernetes API.
The orchestrator cannot for exactly one reason: it runs in a Docker container
outside the VCN.

Every note in this repository has said "the orchestrator has no route to it",
which is true, and implied the remedy must be a network change, which it is not.
The portal already builds machines INSIDE that VCN.

### What the tenancy actually says (2026-09-12)

Asked to run K.1's probe, and checked first whether the answer could mean
anything. It could not:

```
compute subnet : AI-ShiftL-DEV-VM-APP-SUBNET   10.56.39.0/24
  its VCN      : AI-ShiftLeft-DEV-VCN          10.56.32.0/19
cluster        : demo-noqodi-26-0146-oke       10.90.104.12:6443
  its VCN      : NOQODI-DEVELOPMENT            10.90.0.0/16
SAME VCN: False
```

*What is still true.* The OKE module's NSG does admit TCP 6443 from
`operator_cidr`, and `operator_cidr` is the whole VCN. Anything inside the
CLUSTER'S VCN can reach its API without a firewall change.

*What was wrong.* "The portal already builds machines INSIDE that VCN." It does
not. It builds them in `AI-ShiftLeft-DEV-VCN`; the cluster lives in
`NOQODI-DEVELOPMENT`. Those are different VCNs and, absent peering, cannot
reach each other at all.

*How the error was made.* Two true facts — the NSG admits the VCN, and the
portal builds machines — were joined into a third that was never checked: that
the machines are in that VCN. The same shape as reading a column's DEFAULT as
evidence about nine catalogue rows, earlier the same day. An inference wearing a
fact's clothes.

*What it would have cost to find out later.* A probe launched into the portal's
subnet would have reported `unreachable`, which is true of two unpeered VCNs and
says nothing whatever about Phase K. It would have read as decisive, sent us
back to the network route, and cost a machine to mislead ourselves.

**WITHDRAWN.** Of the three outcomes below, the platform owner settled it on
2026-09-12: the VCNs are not peered and this phase would need the network team
exactly as the route does — while also needing a deployer, an IAM policy,
manifests and a teardown path. Strictly more work for the same dependency. The
route wins.

*What survives, and why it was not deleted.* `orchestrator/oke_probe.py`, its
blueprint and its thirteen tests are DORMANT, not dead: the day the route opens,
somebody will want to prove a thing can reach that API before trusting it, and a
tested probe costs nothing to keep. `api.clusters` is the precedent — machinery
kept working and callerless, with a test saying so, precisely so the next person
to find it does not delete it. The `serves: platform` registry category stays for
the same reason and is a real guard on its own: it is what stops the next
platform blueprint inventing a fake catalogue technology to satisfy `builds`.

Three outcomes were possible, and which one held was a question about the
tenancy nobody here had answered:

  1. **A subnet exists in `NOQODI-DEVELOPMENT` the portal may build into.** Then
     the phase stands exactly as written, and it needs a configuration change —
     one subnet OCID — rather than a network one. The probe becomes meaningful
     the moment that OCID exists.
  2. **No such subnet, and the two VCNs must be peered.** Then Phase K needs the
     network team just as the route does, and needs a deployer and manifests on top —
     strictly more work for the same dependency. §6's proposal wins and this
     phase should be withdrawn.
  3. **`demo-noqodi-26-0146-oke` is not the cluster this portal is meant to
     serve.** It is the only ACTIVE cluster in the compartment and its name says
     "demo". If the real target is elsewhere, both of the above are being asked
     about the wrong VCN.

Until that is answered, K.2 onwards is planning against a topology nobody has
confirmed.

**The shape (as proposed, and subject to the above).** A short-lived deployer
instance, built by the existing machinery,
fetches the kubeconfig with an instance principal, applies the workloads, reports
what actually runs, and is destroyed. No inbound access, no network team.

*Four things it reuses rather than invents.* The container rung already resolves
`{image, digest, platform_digest, ports}` from the registry and pins by digest —
that is most of a Deployment. `configure.render` already produces cloud-init that
does work and writes a report. `boot_reports` already carries a machine's own
account of itself OUT through object storage, needing no inbound access. And the
Terraform workspace/plan/apply/destroy path already builds and tears down
machines per request.

### The increments

**K.1 — prove an instance in the VCN can reach the API, and nothing else.**
*Built 2026-09-12; the proof itself is the platform owner's to run.*
`orchestrator/oke_probe.py` renders cloud-init that asks the endpoint once and
reports what it found, and reads the answer back.

*It needs no IAM policy, and the plan above said it would.* That was wrong in
the direction of doing more than the question requires. Reachability is a TCP
and TLS question: a `401 Unauthorized` from the API server is a COMPLETE success
here — the packets arrived, the handshake completed, Kubernetes answered. Adding
an instance principal, a policy and a kubeconfig would have confused "we cannot
get there" with "we got there and were not allowed in", which have entirely
different remedies and only the first of which decides whether Phase K is
possible. Authentication starts in K.2.
*It carries no credential.* The script travels in instance metadata, which
anyone able to read that VM can see. The only URL it holds is the write-only
report PAR every other machine here uses.
*A missing report is not an unreachable endpoint.* `unknown` is a distinct
verdict: nothing reported may mean the machine never booted, and blaming the
network for a build that never happened sends somebody to the wrong team.
*It proves a ROUTE and nothing else.* The certificate is not verified, so it
says nothing about trust; K.2 must verify against the cluster's own CA.

**HOW TO RUN IT.** Launch one Oracle Linux 9 instance **in a subnet of the
CLUSTER'S VCN** — `NOQODI-DEVELOPMENT`, not the one `OCI_COMPUTE_SUBNET_OCID`
names — with this as its user-data, then read the report from the boot bucket and
destroy the instance. A probe launched anywhere else answers a different
question and answers it misleadingly:

```
docker compose exec api python -c "from orchestrator import oke_probe;   print(oke_probe.script('<CLUSTER_PRIVATE_ENDPOINT>', '<WRITE_ONLY_PAR_URL>'))"
```

`result=reachable` — with any HTTP status — means K.2 is worth building.
`result=unreachable` means Phase K is built on sand and the network route is
the answer after all — which is how it was settled, without the machine being
spent. Either way it would have cost one short-lived machine.

**NOT WIRED TO THE PROVISIONER, and the reason is a real question rather than an
omission.** A blueprint manifest must declare `builds` — the technologies it
delivers — and a probe delivers none. Declaring a fake `oke-probe` technology to
satisfy the field would distort the catalogue model in order to ask a question.
The options are: give the registry a category for blueprints that build
infrastructure for the PORTAL rather than for a requester, or leave the probe a
one-off launched by hand as above. **This needs deciding before K.2**, and a
proof increment should not be the thing that settles it.

*Found while looking:* `db/seed.py` says "Everything any blueprint builds is
listed, and test_the_form_says_how_a_thing_arrives keeps it that way." That test
does not exist anywhere in the repository. The invariant is real and unguarded.

**K.2 — one stateless workload, as a Deployment and a Service.**
Generated from the container spec the registry already resolves. One technology,
stateless. *Acceptance:* the pod runs the image pinned by digest, the Service
answers on the declared port, and the request records what was applied.

**K.3 — the workload reports what actually runs.**
The same rule machines are held to: a pod that STARTED is not a pod that WORKS.
The digest that actually arrived, the ports that actually serve — judged the way
`boot_reports.verdict` judges a machine. *Acceptance:* a deliberately broken
manifest is reported as broken rather than as provisioned.

**K.4 — stateful workloads, or an honest refusal.**
PersistentVolumeClaims and a storage class. This is where the advisory warnings
this portal already shows become real operational burden. *Acceptance:* either a
database keeps its data across a pod restart, or the option says plainly that it
will not and refuses. Deferring this is a legitimate outcome of the increment.

**K.5 — the orchestrator stops refusing a container host.**
`_refuse_unsized_hosts` currently refuses every container host because nothing
could build one. It becomes "no deployer is available for this cluster" — a
narrower and still-honest refusal. *Acceptance:* a Kubernetes placement builds;
one with no deployer path still refuses, with a reason naming why.

**K.6 — teardown removes the workloads, not just the machines.**
*Acceptance:* decommissioning a Kubernetes request leaves no pods, no PVCs and no
deployer instance, and says so from the cluster rather than from its own records.

### What I would want written down before starting

*The deployer holds cluster rights for a few minutes.* That is a concentration of
privilege this architecture is otherwise careful about: it must be minimal,
short-lived, audited to the tamper-evident log like every other privileged step,
and destroyed whether the apply succeeds or fails.
*Manifests are artefacts that need the same discipline as blueprints* — proved
before offered, not assumed. K.2 and K.3 exist so that a manifest is certified by
evidence from a real cluster rather than by review.
*None of the standing rules move.* Jira still holds the approval; the
orchestrator still re-verifies it before acting; the agent still recommends and
never executes.
*It costs money per request.* A deployer instance is real infrastructure. Short-
lived and per-request is the cheapest honest shape; a long-lived one per
environment would be cheaper still and is a standing privileged machine, which is
worse.

**The route is now the path, not the alternative.** §6.1 makes the orchestrator
reach the API directly and needs no deployer, no IAM and no manifests. It needs
the network team — which, once the premise above collapsed, this phase needed
too, while also needing everything else. GitOps (ARCHITECTURE §6, §9) would still
need something able to reach the cluster once, to bootstrap its controller, so it
is a successor to the route rather than a way around it.

---

## 6. The open decisions

### 6.1 — A network route from the orchestrator to the cluster API *(the one that blocks work)*

**Promoted here 2026-09-12.** It was recorded inside Phase P as item (1) of "what
would widen this again", and referred to from four places in this file as "§6's
network route" — which pointed at the HTMX decision below. A blocker cited four
times by the wrong name is one nobody can look up.

Everything else in the Kubernetes path now works. A requester can choose it, the
layout is sized and priced per pod, the placement is recorded, it passes
approval, and it fails at the handoff with a sentence naming the missing route
and the way out. Phase K existed to avoid this decision and was withdrawn on
2026-09-12 once its premise proved wrong: there is no path to a private cluster
API that does not cross a network boundary somebody has to open.

*The options, unchanged from Phase P's note:* a public endpoint with a source
allowlist; the orchestrator inside the VCN; OCI managed Bastion port-forwarding;
or peering `AI-ShiftLeft-DEV-VCN` to the cluster's VCN. The proposal drafted for
the network team covers the first three.

*What it is worth:* `CLUSTER_DEPLOYMENT_ENABLED` is on, so every Kubernetes
request today spends an approver's time and then fails. Turning it off refuses
them up front instead. Either is honest; leaving it on is only worth it while
the route is expected soon.

### 6.2 — HTMX vs React for the portal *(settled in practice)*

**HTMX vs React for the portal (ARCHITECTURE.md §14.1).** This plan assumes HTMX. If you choose React instead, only the *portal* increments change shape — 0.3, 1.2, 1.3, and 1.5 would build a React app calling the same API — while the API, database, policy, Jira, orchestrator, and every enterprise increment stay identical. So the decision is real but low-blast-radius; it doesn't block starting Phase 0, which is stack-neutral either way.

Everything else in ARCHITECTURE.md §14 (control frameworks, sovereign mode, Arabic-at-launch, budget source, CMDB sync, etc.) can be settled before its own increment and does not block Phase 0 or Phase 1.

---

## 7. Your first three prompts to Claude Code

To start, point Claude Code at the folder containing `CLAUDE.md`, `ARCHITECTURE.md`, and this `PLAN.md`, then:

1. *"Read CLAUDE.md, ARCHITECTURE.md and PLAN.md. Confirm you understand the phased approach and do not write code yet. Then show me your plan for Phase 0, increment 0.1 only, and wait for my go."*
2. After you approve and it builds 0.1: *"Good. Show me how to run it and what I should see. Then commit, and stop."*
3. Then: *"Plan increment 0.2 only, and wait for my go."*

Repeat that rhythm — plan one increment, approve, build, run, commit, stop — all the way through. That loop is what keeps you in control of a build you can verify at every step.
