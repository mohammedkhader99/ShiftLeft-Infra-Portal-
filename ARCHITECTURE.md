# ARCHITECTURE.md
## Shift-Left Infrastructure Provisioning Portal — Governing Architecture

**Owner:** Infrastructure Management Department (IMD)
**Status:** Draft v2 (July 2026) — integrates the Enterprise Feature Catalogue (126 features, 11 domains). Supersedes the earlier React/Node design for the portal and agent layers, and supersedes the provisional E1–E4 mapping in draft v1.
**Audience:** Claude Code (implementer) and the IMD reviewer
**Companion documents:** SCOPE.md (delivery plan) · PROMPT.md (build brief) · FEATURES.md / Enterprise Feature Catalogue (feature source of truth)

---

## 0. How Claude Code must use this document

This document is the **architect's authority** for the build. It governs every later phase.

1. Do **not** write application code until this document and `PLAN.md` are approved by the reviewer.
2. Every implementation increment must conform to this document. If a request — from the reviewer or discovered mid-build — conflicts with anything here, **stop and flag it before coding**, explaining the conflict in plain language.
3. Feature identifiers (e.g. `F-FIN-07`) in §11 are the traceability keys. Every increment you build must reference the feature IDs it implements, so the backlog, the plan, and the code stay linked.
4. Any technology or design decision you make that is not already fixed below must be recorded back into this file with a one-sentence, plain-language reason.
5. Build **one increment at a time** (§12). After each: explain in plain steps how to run it and what a working result looks like, commit to git with a clear message, then stop for approval.
6. Favour **simple, mainstream, well-documented** technology over clever choices.

---

## 1. Purpose and scope

A self-service portal that lets IMD's internal customers request infrastructure environments that are **validated, policy-checked, costed, and approved *before* anything is provisioned** — the "shift-left" principle: catch problems at request time, not at apply time.

**Request types in scope (four):** create environment · add component · resize component · decommission environment or component.

**Deployment targets:** Microsoft Azure, Oracle Cloud Infrastructure (OCI), and on-premises.

**Regulatory context:** serves GDRFAD mission-critical operations (airport immigration, eGate, visa, biometrics). Auditability, least-privilege, explainability, and — where required — sovereign/air-gapped operation are first-class requirements, not add-ons.

**What "enterprise-grade" means here (six properties):** provable governance, financial control, operational resilience, lifecycle ownership, extensibility (config/adapters not code releases), and fit for the sector (data residency, sovereign deployment, bilingual interface, control-framework mapping).

---

## 2. Architectural principles (non-negotiable)

**P1 — Separation of authority (the governing rule).** The **browser and portal collect and validate**; **Jira holds the approval**; the **orchestrator executes** — and only after independently re-verifying the approval. No layer may take on another layer's authority.

**P2 — The client holds no authority.** Client-side validation is UX only. All validation, sizing, costing, and pricing are re-computed server-side and are authoritative. The browser holds no credentials and no pricing logic.

**P3 — The agent recommends; it never decides or executes.** AI agents may *assist* a requester and *recommend* sizing, technology, and policy interpretation. They must never hold provisioning credentials, approve a request, or trigger the orchestrator. This principle is also the answer to the catalogue's open decision on "which actions an assistant may never take."

**P4 — Everything privileged is auditable and explainable.** Every request, validation, estimate, policy decision, approval, agent recommendation (with reasoning), and provisioning action is written to a tamper-evident, append-only audit trail (F-SEC-01).

**P5 — Extensibility over releases.** New technologies, clouds, approvers, and rates are configuration and adapters, not code releases (F-OPS-09, F-INT-11).

**P6 — One language where it helps.** The stack is Python end to end so sizing, costing, policy, and agent logic share models and code rather than crossing a language boundary.

**P7 — Catalogue facts are fetched, never hard-coded.** Versions, shapes, images and service limits are asked of the cloud when they are needed, and cached briefly. A constant in this repository describing a cloud's offering is a defect waiting for a date: eight were found on 17–18 Aug 2026, including a Kubernetes version OCI had retired, a database shape it does not publish in this region, and a worker image chosen by list position that turned out to be Oracle Linux 7.

**P8 — Certification is earned by evidence and revoked by evidence.** A blueprint is certified because a proof build provisioned it, verified it healthy, priced it within budget and destroyed it — not because someone said so — and that certification expires after 30 days without fresh proof (decided 2026-08-21). Withdrawal is automatic; coming back is not.

---

## 3. System architecture

Six cooperating parts.

| # | Part | Technology | Responsibility |
|---|------|-----------|----------------|
| 1 | **Portal (UI)** | React (Vite + TypeScript) + IBM Carbon Design System, served via a FastAPI Backend-for-Frontend | Guided multi-step request, live validation, live cost panel, approvals preview, people-pickers, My-Environments dashboard. *(Decision §14.1, 2026-07-29: React + Carbon chosen over the HTMX+Jinja of draft v2, for a premium enterprise UI; the BFF keeps Entra OIDC + token handling server-side so the API/authority layer is unchanged.)* |
| 2 | **API / Authority layer** | FastAPI (Python 3.12), Pydantic | Re-validation, sizing, costing, persistence, Jira creation, signed orchestrator handoff. Holds all credentials and pricing logic. |
| 3 | **Agent service** | Python, Anthropic SDK + agent framework, behind tool-scoped APIs | Skilled Agents + Supervisor. Assists requesters; recommends sizing/technology; explains estimates and policy. Read-and-recommend only. |
| 4 | **Database** | PostgreSQL 16 | Lookups, sizing anchors, rate cards, requests, estimates, approvals, registry, budgets/quotas, policy decisions, audit |
| 5 | **Policy engine** | OPA / Rego | Deterministic policy gate at request time and re-checked before execution |
| 6 | **Orchestration layer** | Durable workflow engine → Terraform / Ansible / GitOps | Idempotent, resumable, reversible provisioning with durable state. Separate service. |

**Horizontal (across all parts):** Langfuse (agent tracing/eval); OpenTelemetry distributed tracing (F-OPS-04); OPA governance; the tamper-evident audit log as system of record.

**Portal front end (revised 2026-07-29):** the baseline was built HTMX+Jinja (draft v2's choice for one-language/no-JS-toolchain). It is now being migrated to a **React (Vite + TypeScript) SPA on IBM Carbon**, for premium enterprise UI fidelity (sharp-edged, blue/white, WCAG 2.2, dark/light). A **Backend-for-Frontend** (the existing FastAPI portal service) keeps doing Entra OIDC and proxies the SPA's calls to the API with the user's token — so **the API, RBAC, policy, Jira, orchestrator and every server-side control are unchanged**. Migration was incremental (screen by screen, HTMX portal running until parity); **cutover completed 2026-07-29 (UX.5)** — the React portal is now the front door on port 5173 and the HTMX portal is parked as a demo fallback (docker-compose `fallback` profile). Backstage.io was considered and declined (heavyweight platform re-architecture; conflicts with the simple/mainstream principle).

---

## 4. The authority-separation rule, in detail

- **Collect & validate** — portal and API gather the request, validate every field server-side, size it, price it, run the OPA policy gate (F-GOV-03), and pre-check budget (F-FIN-02) and capacity/quota (F-FIN-08). Nothing is provisioned here.
- **Approve** — the request is raised in Jira Service Management with configuration **and** cost shown together, plus the Terraform plan preview (F-ORC-02). Approval authority lives in Jira and nowhere else. Dynamic, rule-driven approver sets by type/environment/cost/target/licence.
- **Execute** — on approval, a signed webhook (HMAC-SHA256, `X-Signature`) reaches the orchestrator, which **independently re-verifies the approval in Jira and re-checks OPA**, then runs the durable workflow. It trusts the signature for authenticity, not for authority. A cost re-validation gate (F-ORC-09) halts if the plan's price exceeds the approved threshold.

- **Attest (certification runner only)** — the runner proves a blueprint still builds, by building it. It has its own narrow authority to provision **without a Jira approval**, and that authority is bounded on every side:
  - **sandbox tier only** — currently `Development` (decided 2026-08-21, "for the time being"). An unset or unrecognised sandbox tier makes the runner refuse to start, rather than default to somewhere.
  - **under a cost cap** — the Terraform plan is priced first, and a proof whose plan exceeds the cap is refused *before* apply. It never discovers the cost by paying it.
  - **only what it created** — teardown is scoped by a proof marker, never by tier. The sandbox is a real tier holding real environments people are using, so "everything the runner built" and "everything in Development" must never be the same query.
  - **it destroys everything it builds** — a proof that leaves a resource behind is a failed proof.
  - **it never touches a user request.** It cannot approve one, advance one, or tear one down.

  Its verdict is a fact about a build, not a permission. It certifies nothing for a user; it records what happened, and certification follows from that record.

**Why this exception exists, in one sentence:** certification was a person clicking a button, and that promise failed in practice — `oci-oke` was certified by hand and then failed four consecutive real requests while staying on offer — so the portal now proves the claim by building the thing, which it cannot do if every proof needs a human approval (decided by the reviewer, 2026-08-21, choosing this over a Jira ticket per proof).

Approval authority for **user requests** remains in Jira and nowhere else. The runner's authority extends to its own sandbox proofs and stops there.

If any increment would let the browser, the API, or an agent shortcut this chain, that is a conflict — flag it (§0.2). The certification runner is the single named exception, with the limits above; anything wider is a new conflict and must be flagged again.

---

## 5. Data model (high level)

- `lookup` — projects, cost centres, technologies, existing environments
- `blueprint` / `blueprint_version` — versioned golden-path patterns with changelogs (F-CAT-01)
- `sizing_anchor` — effective-dated, versioned anchors (F-CAT-07)
- `rate_card` — `onprem_rate`, `licence_rate`, `cloud_price_cache` (Azure/OCI); discount-aware rates (F-FIN-04)
- `request` — typed (create/add/resize/decommission), requester, project, cost centre, data classification (F-SEC-02)
- `request_component` — one or more technology+size components per request; an environment is composed of components (recorded at increment 1.3a)
- `estimate` — server-computed cost tied to a request
- `budget` / `quota` — per cost-centre/project ceilings and remaining budget (F-FIN-02, F-FIN-08)
- `approval` — Jira key, status, approver chain, SLA timers (F-GOV-01)
- `resource_registry` — provisioned assets, owner (directory **group**, F-IAM-09), environment class, **TTL/expiry** (F-FIN-07), lifecycle state, health score (F-LCM-08)
- `policy_decision` — OPA input/result per request, retained for audit
- `agent_interaction` — every agent recommendation with inputs and reasoning
- `audit_log` — append-only, hash-chained, tamper-evident (F-SEC-01)

---

## 6. Request lifecycle (end to end)

Requester opens the portal → agent optionally assists in plain language (F-RPT-06) → structured request built and validated client-side (UX) and server-side (authoritative) → sizing, costing, OPA gate, budget + capacity pre-checks → persisted with estimate and policy result → Jira ticket raised with config + cost + plan preview → approver reviews and approves → signed webhook → orchestrator re-verifies approval + policy → durable workflow provisions, assigns just-in-time access (F-IAM-07), registers the asset with TTL, verifies first backup (F-INT-07), onboards observability (F-INT-06), writes audit → day-2 lifecycle, drift, and cost feedback loops run against the registry.

---

## 7. Agent architecture and guardrails

- Skilled Agents sit behind **narrowly scoped, least-privilege tool APIs** exposed by the authority layer (e.g. `search_directory`, `get_sizing_anchor`, `estimate_cost`, `check_policy`, `explain_estimate`). Read-only unless a write is unavoidable and reviewed.
- The Supervisor routes between Skilled Agents; no agent holds cloud or provisioning credentials.
- **Prompt-injection defence:** any free-text a requester provides is untrusted data, never instructions to the agent. Tool-use is authorised by the API, not by the model's say-so.
- AI scope (F-RPT-06/07/08): draft requests confined to approved templates and sizes; explain estimates and required approvals in plain language; summarise provisioning failures and propose remediation **within approved runbooks only**. Never approve, never execute (P3).
- Every recommendation is written to `agent_interaction` with reasoning; agents are covered by a Langfuse eval harness (golden cases + regression).

---

## 8. Security architecture

OIDC bearer validation against the IdP's JWKS (no mock auth in production) · secrets from a vault, injected at runtime, delivered to environments via time-bound vault links never tickets/email (F-INT-05) · verify `X-Signature` HMAC and restrict the webhook's network exposure · least-privilege DB role, TLS enforced · tamper-evident audit (F-SEC-01) · IaC secret/policy scanning before apply (F-SEC-03) · image CVE gate + SBOM (F-SEC-04) · application hardening: CSP, CSRF, rate limiting, strict sessions (F-SEC-09) · SIEM forwarding of security events (F-INT-12) · optional sovereign/air-gapped profile with cached rate cards and internal identity only (F-SEC-11).

---

## 9. Technology decisions with rationale

| Decision | Choice | One-line reason |
|----------|--------|-----------------|
| Portal UI | React (Vite + TypeScript) + IBM Carbon, via a FastAPI BFF | Premium enterprise UI (Carbon: sharp-edged, WCAG 2.2, dark/light); BFF keeps auth server-side so the API is unchanged. Revised 2026-07-29 from HTMX+Jinja (see §14.1). |
| Backend/API | FastAPI (Python 3.12) + Pydantic | Same language as the agents; strong typed validation; async |
| Agents | Python + Anthropic SDK + agent framework | The agentic ecosystem is Python-first |
| Database | PostgreSQL 16 | Mature relational store for rate cards, registry, audit |
| DB access layer | SQLAlchemy 2.0 + psycopg | Mainstream, well-documented Python ORM; typed models shared across the app (P6). *(Recorded at increment 1.1.)* |
| Approvals | Jira Service Management | Approval authority lives where IMD's workflow already is |
| Policy | OPA / Rego | Deterministic, testable policy gate independent of app code |
| Orchestration | Durable workflow engine + Terraform/Ansible/GitOps | Retry/rollback/state for provisioning that can fail midway |
| Agent observability | Langfuse | Tracing + evaluation for agent decisions |

**UI brand:** petrol teal `#0B5563`, amber `#D97706`, Calibri.

---

## 10. Feature catalogue (126 features, 11 domains)

The complete feature surface, grouped conceptually into four layers — **experience, governance, execution, insight** — and organised into eleven domains. Priority: **Must** (enterprise baseline) · Should (high value) · Could (differentiating, evidence-led). IDs are the backlog and traceability keys.

### 10.1 Identity, Access & Multi-Tenancy (IAM)
| ID | Feature | Priority | Purpose |
|----|---------|----------|---------|
| F-IAM-01 | Fine-grained RBAC | Must | Distinct roles: requester, approver, platform admin, auditor, FinOps, read-only |
| F-IAM-02 | Attribute-based scoping | Must | Visibility scoped by cost centre, project, environment class |
| F-IAM-03 | Segregation-of-duties engine | Must | Detects and blocks requester/approver/executor overlap |
| F-IAM-04 | Approval delegation | Should | Time-bound, audited out-of-office proxy |
| F-IAM-05 | Break-glass path | Should | Expedited emergency route with mandatory post-hoc review |
| F-IAM-06 | Business-unit isolation | Should | Separate catalogues, rates, approvers, branding per BU |
| F-IAM-07 | Just-in-time access | Could | Time-bound approved access instead of standing privilege |
| F-IAM-08 | Service accounts & API tokens | Should | Scoped machine identities for pipeline-initiated requests |
| F-IAM-09 | Group-based ownership | Must | Ownership on directory groups so it survives staff movement |
| F-IAM-10 | Step-up authentication | Should | Re-auth/MFA before destructive or high-value actions |

### 10.2 Request Experience & Intake (UX)
| ID | Feature | Priority | Purpose |
|----|---------|----------|---------|
| F-UX-01 | Saved drafts | Must | Save and resume incomplete requests |
| F-UX-02 | Request templates | Should | Personal/team templates for recurring patterns |
| F-UX-03 | Clone request or environment | Should | Start from a prior request or existing environment |
| F-UX-04 | Bulk requests | Could | Multiple environments, one approval chain |
| F-UX-05 | Target comparison view | Should | Azure/OCI/on-prem side by side with cost & capability |
| F-UX-06 | What-if cost simulator | Should | Explore sizing/stack without submitting |
| F-UX-07 | Bilingual interface (AR/EN) | Should | Full Arabic + English with RTL layout |
| F-UX-08 | Accessibility conformance | Must | WCAG 2.2 AA |
| F-UX-09 | Mobile & approver experience | Should | Responsive portal + mobile approval view |
| F-UX-10 | Guided error remediation | Should | Messages explain the rule and offer the compliant option |
| F-UX-11 | Live request timeline | Should | Stage-by-stage progress with streamed logs |
| F-UX-12 | Notification centre | Should | In-portal notifications, per-user channel/digest prefs |
| F-UX-13 | Collaboration on requests | Could | Comments, mentions, shareable links |
| F-UX-14 | Theming & dark mode | Could | Org branding + dark theme |
| F-UX-15 | My Environments dashboard | Must | Owned environments with health, cost, expiry, actions |

### 10.3 Catalogue, Standards & Blueprints (CAT)
| ID | Feature | Priority | Purpose |
|----|---------|----------|---------|
| F-CAT-01 | Versioned blueprint catalogue | Must | Golden-path blueprints with versions and changelogs |
| F-CAT-02 | Technology lifecycle states | Must | Certified/preview/deprecated/EOL; deprecated warns, expired blocks |
| F-CAT-03 | Version compatibility matrix | Should | Prevents unsupported DB/middleware/platform combos |
| F-CAT-04 | Naming standard enforcement | Must | Names generated and validated against the standard |
| F-CAT-05 | Mandatory tagging policy | Must | Cost centre, owner, classification, project at creation |
| F-CAT-06 | Data-residency policy | Must | Region/sovereignty rules enforced at request time |
| F-CAT-07 | Versioned sizing anchors | Must | Effective-dated anchors with visible diff |
| F-CAT-08 | Environment-class profiles | Must | Prod vs non-prod defaults for HA, backup, monitoring |
| F-CAT-09 | Catalogue administration UI | Should | Add technologies/sizes/rates/rules without a release |
| F-CAT-10 | Blueprint certification pipeline | Should | Test and approve blueprints before publication |
| F-CAT-11 | Custom-size review workflow | Should | Non-standard sizing routed to architecture review |

### 10.4 Cost Management & FinOps (FIN)
| ID | Feature | Priority | Purpose |
|----|---------|----------|---------|
| F-FIN-01 | Actual-vs-estimate variance | Must | Compare billed cost to approved estimate; alert on drift |
| F-FIN-02 | Budget guardrails | Must | Check remaining budget at request time; warn/block early |
| F-FIN-03 | Showback & chargeback | Must | Cost by cost centre/project/environment/owner on schedule |
| F-FIN-04 | Discount-aware pricing | Should | Reserved instances/savings plans/negotiated rates in estimates |
| F-FIN-05 | Idle environment detection | Should | Flag under-utilised environments with rightsizing/removal |
| F-FIN-06 | Scheduled auto-shutdown | Should | Stop non-prod out of hours; quantify the saving |
| F-FIN-07 | Environment TTL & renewal | Must | Expiry date + renewal workflow on every non-prod environment |
| F-FIN-08 | Quota management | Should | Per-project/cost-centre ceilings on resources or spend |
| F-FIN-09 | Cost anomaly detection | Could | Alert on unexpected consumption vs baseline |
| F-FIN-10 | Multi-currency handling | Should | AED and USD with a defined FX policy |
| F-FIN-11 | Spend & capacity forecasting | Could | Project future spend/demand from pipeline and estate |
| F-FIN-12 | Optimisation digest | Should | Periodic recommendations to owners |
| F-FIN-13 | Sustainability estimate | Could | Indicative energy/carbon per environment |

### 10.5 Approvals, Policy & Governance (GOV)
| ID | Feature | Priority | Purpose |
|----|---------|----------|---------|
| F-GOV-01 | Approval SLA & escalation | Must | Track time in state, remind, escalate on breach |
| F-GOV-02 | Parallel, sequential & quorum | Must | Concurrent checks, ordered gates, quorum rules |
| F-GOV-03 | Policy-as-code evaluation | Must | Machine-evaluated policy (OPA) with human-readable reasons |
| F-GOV-04 | Automated risk scoring | Should | Risk score from class/exposure/classification/cost drives approval depth |
| F-GOV-05 | Exceptions & waivers | Should | Time-limited waivers with owner and re-review at expiry |
| F-GOV-06 | Change window enforcement | Must | Freeze/maintenance windows enforced at request and execution |
| F-GOV-07 | Change record integration | Should | Automatic change-record creation and linkage |
| F-GOV-08 | Four-eyes on destructive actions | Must | Two distinct approvers for irreversible operations |
| F-GOV-09 | Compliance control mapping | Should | Features/evidence mapped to audited control frameworks |
| F-GOV-10 | Auditor evidence pack | Must | One-click export of a request's full decision/approval/execution evidence |
| F-GOV-11 | Policy simulation | Could | Test a policy change against historical requests first |

### 10.6 Environment Lifecycle Management (LCM)
| ID | Feature | Priority | Purpose |
|----|---------|----------|---------|
| F-LCM-01 | Expiry & renewal campaigns | Must | Scheduled reminders, bulk renew/remove drives |
| F-LCM-02 | Start/stop scheduling | Should | Owner-defined running-hours calendars |
| F-LCM-03 | Environment refresh & clone | Should | Refresh lower env from higher with data masking |
| F-LCM-04 | Patch & upgrade requests | Should | Governed request type for upgrades/patching |
| F-LCM-05 | Certificate & secret expiry | Should | Track expiry across the estate; renew before outage |
| F-LCM-06 | Self-service backup & restore | Should | Owner-initiated, approval-governed, verified restore |
| F-LCM-07 | DR failover test | Could | Repeatable, evidenced DR test request type |
| F-LCM-08 | Environment health score | Should | Score from patch/backup/monitoring/drift/compliance |
| F-LCM-09 | Drift detection & remediation | Should | Detect divergence from approved state; correct or raise |
| F-LCM-10 | Ownership transfer & orphans | Must | Reassignment workflow + auto-detect owner-left environments |
| F-LCM-11 | Reversible quarantine | Must | Two-stage decommission: isolate first, destroy after retention |
| F-LCM-12 | Dependency & blast-radius view | Should | Show what depends on an env before resize/removal |

### 10.7 Orchestration & Reliability (ORC)
| ID | Feature | Priority | Purpose |
|----|---------|----------|---------|
| F-ORC-01 | Idempotent, resumable workflows | Must | Safely retry/resume from point of failure with compensation |
| F-ORC-02 | Plan preview before approval | Must | Terraform plan/change preview attached to the ticket |
| F-ORC-03 | Automatic rollback | Must | Failure taxonomy, safe auto-rollback, route to owner otherwise |
| F-ORC-04 | Retry & partial success | Must | Backoff, retry limits, explicit partial-provision handling |
| F-ORC-05 | Concurrency & rate control | Should | Queueing and per-target limits to protect cloud APIs |
| F-ORC-06 | Live log streaming | Should | Execution progress/logs to the requester |
| F-ORC-07 | Scheduled execution | Should | Provision within an approved maintenance window |
| F-ORC-08 | Target capability matrix | Must | Unsupported combinations fail at request time, not mid-apply |
| F-ORC-09 | Cost re-validation gate | Must | Re-price from the actual plan; halt if variance exceeds threshold |
| F-ORC-10 | In-flight approval checkpoints | Should | Human checkpoints inside long-running workflows |

### 10.8 Integration & Extensibility (INT)
| ID | Feature | Priority | Purpose |
|----|---------|----------|---------|
| F-INT-01 | Public API & OpenAPI spec | Must | Versioned API so pipelines/systems can request programmatically |
| F-INT-02 | Lifecycle event stream | Should | Publish request/environment events to an event bus |
| F-INT-03 | ITSM abstraction layer | Should | Jira today, ServiceNow/other tomorrow without rewrite |
| F-INT-04 | Bi-directional CMDB sync | Must | Registration, reconciliation, drift detection with the CMDB |
| F-INT-05 | Vault credential delivery | Must | Time-bound vault link; never a ticket or email |
| F-INT-06 | Observability auto-onboarding | Must | Dashboards, alerts, SLOs created with the environment |
| F-INT-07 | Backup registration & verification | Must | Backup policy applied and first backup verified before handover |
| F-INT-08 | Chat bot for approvals | Should | Approve/reject/query from Teams or Slack |
| F-INT-09 | CLI & Terraform provider | Could | Power users interact with the portal as code |
| F-INT-10 | Outbound webhooks | Should | Subscribable webhooks for other teams |
| F-INT-11 | Adapter SDK | Should | Defined interface to add technologies/clouds/pricing as plug-ins |
| F-INT-12 | SIEM forwarding | Must | Security events forwarded to the SIEM in a standard format |

### 10.9 Reporting, Analytics & Intelligence (RPT)
| ID | Feature | Priority | Purpose |
|----|---------|----------|---------|
| F-RPT-01 | Executive dashboard | Must | Lead time, volume, cost, automation rate, approval SLA |
| F-RPT-02 | Delivery metrics & trends | Should | Throughput, cycle time, failure rate, change-success trends |
| F-RPT-03 | Estate inventory & export | Must | Searchable, filterable inventory with export |
| F-RPT-04 | Immutable audit reporting | Must | Auditor-ready reports from the tamper-evident trail |
| F-RPT-05 | Forecasting | Could | Projected spend/capacity from pipeline and estate |
| F-RPT-06 | AI request drafting | Should | NL description → draft request confined to approved templates/sizes |
| F-RPT-07 | AI cost & decision explanation | Should | Plain-language explanation of estimate drivers and approval need |
| F-RPT-08 | AI failure triage | Should | Summarise a failure, propose remediation within approved runbooks |
| F-RPT-09 | Rightsizing recommendations | Should | Utilisation-based sizing advice fed back into anchors |
| F-RPT-10 | Request anomaly detection | Could | Flag unusual request patterns as security/misuse signal |
| F-RPT-11 | Report subscriptions | Should | Scheduled reports to finance/security/owners |

### 10.10 Security & Compliance (SEC)
| ID | Feature | Priority | Purpose |
|----|---------|----------|---------|
| F-SEC-01 | Tamper-evident audit trail | Must | Append-only, hash-chained events provably unaltered |
| F-SEC-02 | Data classification | Must | Classification drives encryption/access/backup/residency |
| F-SEC-03 | IaC secret & policy scanning | Must | Templates/plans scanned before apply |
| F-SEC-04 | Image CVE gate & SBOM | Must | Images scanned and signed; SBOM retained |
| F-SEC-05 | Supply-chain attestation | Should | Provenance attestation for blueprints/modules |
| F-SEC-06 | Network policy templates | Must | Segmentation/network policy by environment class |
| F-SEC-07 | Encryption & key management | Must | In transit and at rest; BYOK and HSM options |
| F-SEC-08 | Privileged access & session recording | Should | Elevated prod access governed and recorded |
| F-SEC-09 | Application hardening | Must | CSP, CSRF, rate limiting, strict sessions for the portal |
| F-SEC-10 | Vulnerability management | Must | Regular pen-testing and defined remediation cycle |
| F-SEC-11 | Sovereign / air-gapped mode | Should | No external calls; cached rates and internal identity |

### 10.11 Platform Operations (OPS)
| ID | Feature | Priority | Purpose |
|----|---------|----------|---------|
| F-OPS-01 | Multi-region high availability | Must | Portal itself deployed for resilience |
| F-OPS-02 | Zero-downtime releases | Must | Blue/green + backward-compatible migrations |
| F-OPS-03 | Feature flags | Should | Progressive rollout and rapid disablement |
| F-OPS-04 | Distributed tracing | Must | End-to-end tracing across portal, API, orchestrator |
| F-OPS-05 | Service-level objectives | Should | Availability/latency objectives with error budgets |
| F-OPS-06 | Backup & DR for the portal | Must | Tested recovery objectives for the portal's own data |
| F-OPS-07 | Performance & load testing | Should | Performance budgets under realistic load |
| F-OPS-08 | Resilience testing | Could | Failure injection to prove graceful degradation |
| F-OPS-09 | Administration console | Must | Rates/anchors/rules/catalogue/flags managed by admins, not devs |
| F-OPS-10 | Sandbox & training mode | Should | Safe seeded environment for onboarding/demos/testing |

---

## 11. The fifteen highest-impact additions

The priority spine. If nothing else is adopted from the catalogue, these move furthest toward enterprise-grade for the least disruption:

1. Environment expiry & renewal (F-FIN-07) · 2. Actual-vs-estimate variance (F-FIN-01) · 3. Budget guardrails (F-FIN-02) · 4. Auto-shutdown scheduling (F-FIN-06) · 5. Policy-as-code evaluation (F-GOV-03) · 6. Plan preview before approval (F-ORC-02) · 7. Idempotent, resumable workflows (F-ORC-01) · 8. Tamper-evident audit trail (F-SEC-01) · 9. Vault credential delivery (F-INT-05) · 10. Public API & OpenAPI (F-INT-01) · 11. Approval SLA & escalation (F-GOV-01) · 12. Administration console (F-OPS-09) · 13. RBAC & segregation of duties (F-IAM-01/03) · 14. Health score & drift detection (F-LCM-08/09) · 15. Executive dashboard (F-RPT-01).

---

## 12. Enterprise increments (E1–E4)

Adopted from the catalogue's sequencing (authoritative). Guiding principle: **build what is hard to retrofit first** — access control, audit integrity, and the API contract shape everything after; cost and lifecycle value follow once there is an estate to optimise; intelligence comes last, when there is data for it.

| Increment | Theme | Representative content |
|-----------|-------|------------------------|
| **E1** | Enterprise foundations | RBAC & segregation of duties (F-IAM-01/03), group ownership (F-IAM-09), administration console (F-OPS-09), tamper-evident audit (F-SEC-01), public API (F-INT-01), distributed tracing (F-OPS-04), high availability (F-OPS-01), application hardening (F-SEC-09) |
| **E2** | Governance depth | Policy-as-code (F-GOV-03), approval SLA & escalation (F-GOV-01), quorum approvals (F-GOV-02), change windows (F-GOV-06), exceptions & waivers (F-GOV-05), plan preview (F-ORC-02), auditor evidence pack (F-GOV-10), four-eyes on destructive actions (F-GOV-08) |
| **E3** | FinOps & lifecycle | Environment expiry & renewal (F-FIN-07), auto-shutdown (F-FIN-06), actual-vs-estimate variance (F-FIN-01), budget guardrails (F-FIN-02), showback (F-FIN-03), quotas (F-FIN-08), health score (F-LCM-08), drift detection (F-LCM-09), ownership transfer (F-LCM-10), refresh & restore (F-LCM-03/06) |
| **E4** | Intelligence & ecosystem | AI request drafting & failure triage (F-RPT-06/08), forecasting (F-RPT-05), optimisation digests (F-FIN-12), event stream (F-INT-02), chat bot (F-INT-08), CLI & provider (F-INT-09), sustainability (F-FIN-13), anomaly detection (F-FIN-09/F-RPT-10) |

**Architecture notes on placement (my earlier seven gaps, reconciled):**
- Orchestration resilience (F-ORC-01/03/04/08) is foundational and hard to retrofit — build it alongside **E1**, even though the catalogue lists most ORC items only implicitly. The first real orchestrator handoff in the baseline phases must already be idempotent.
- Capacity/quota pre-checks (F-FIN-08) are needed at request time from the start; the *governance* of quotas lands in **E3**, but the *pre-check gate* belongs with the baseline validation path.
- Just-in-time access (F-IAM-07) pairs with vault credential delivery (F-INT-05) and belongs with **E1/E3** access work.
- IaC shift-left QA (F-SEC-03/04) belongs in **E1/E2** — the plan must be scanned before the first production apply.
- Agent security (P3, §7) is a cross-cutting acceptance condition on **E4**, not a feature to bolt on afterwards.

---

## 13. Enterprise non-functional requirements (acceptance conditions, not features)

- **Availability:** 99.9% portal + API; graceful degradation when pricing/directory is slow or unavailable.
- **Performance:** estimate ≤ 2 s (p95); lookups ≤ 500 ms; dashboards ≤ 3 s.
- **Scalability:** stateless API scaled horizontally; tested to peak concurrent request and estate size.
- **Recoverability:** defined, *tested* RPO/RTO for portal data; restores rehearsed.
- **Auditability:** every state change attributable, immutable, exportable; retention per policy.
- **Security:** least privilege throughout; no standing credentials; secrets from a vault; annual pen-testing.
- **Accessibility:** WCAG 2.2 AA, validated by tooling and manual keyboard/screen-reader testing.
- **Localisation:** Arabic + English, RTL layout, locale-correct dates/numbers/currency.
- **Observability:** structured logs, metrics, distributed traces with correlation IDs across all components.
- **Maintainability:** reference data and rules changed by configuration; adapters isolated; no integration logic in the front end.

---

## 14. Decisions required before build

Each shapes multiple features and should be settled before the relevant increment starts.

1. **Portal framework** — ✅ **RESOLVED 2026-07-29: React (Vite + TypeScript) + IBM Carbon, via a FastAPI BFF.** Chosen over the baseline HTMX+Jinja for premium enterprise UI; the BFF keeps auth server-side so the API/controls are unchanged. Migration was incremental and **cutover completed 2026-07-29 (UX.5)**: the React portal is the front door on port 5173 (reusing the classic portal's registered Entra redirect URI); the HTMX portal is parked as a demo fallback under the docker-compose `fallback` profile.
2. **Workflow engine** — Temporal vs. Argo Workflows for durable orchestration.
3. **Control frameworks to evidence** — e.g. ISO 27001 and the national information-assurance standard (drives F-GOV-09 and the evidence pack).
4. **Sovereign / air-gapped profile** — required or not (constrains all outbound pricing/identity calls, F-SEC-11).
5. **Arabic at launch** or later increment (F-UX-07 affects every screen).
6. **Authoritative budget source** — finance system, cost-centre master, or manual load (F-FIN-02/03).
7. **Default environment lifetimes by class**, and who may extend them (F-FIN-07, F-LCM-01).
8. **ITSM abstraction now or later** — Jira only for the foreseeable future, or abstract now (F-INT-03).
9. **CMDB system of record** — internal registry only, or bi-directional sync to ServiceNow (F-INT-04).
10. **AI position in a governed workflow** — which actions an assistant may *never* take (answered by P3; confirm and record).
11. **Ownership of catalogue, rate cards and approval rules** — a permanent operational responsibility, not a project task.
12. **IDP re-visit** — ✅ **RESOLVED 2026-07-29: custom portal (now React + Carbon), Backstage declined** — adopting Backstage would mean re-architecting around its catalog/scaffolder/auth model and re-homing everything already built; conflicts with the simple/mainstream principle.

---

## 15. Explicitly out of scope (v1)

- The orchestrator's internal runbooks (separate service; this document defines only the signed contract to it).
- Billing integration beyond showback/chargeback reporting.
- Any client-side authority (permanently out of scope by principle P2).

**Scope change (recorded by reviewer):** real provisioning is now **in scope, limited to a non-production OCI sandbox**, using Terraform behind a hard `PROVISION_MODE` switch (default mock). Guardrails: the AI never holds cloud credentials or triggers a real apply (P3); credentials live in the orchestrator's vault, supplied by the reviewer; sandbox only until proven; `terraform plan` shown before any apply; idempotency + cost re-validation gate + rollback required first. Production provisioning remains out of scope pending its own hardening (change windows, four-eyes, IaC scanning).

---

## 16. Recommendation (build order)

Complete the baseline request-to-approval spine first. Then adopt **E1 in full** before widening scope, because access control, audit integrity, the administration console, and the public API are materially harder to retrofit once in production. From there, sequence by evidence rather than ambition: land the fifteen highest-impact features (§11) ahead of the wider catalogue, measure adoption and cost outcomes, and let results decide the rest. A smaller platform that is trusted, governed, and well operated beats a larger one that is none of those.
