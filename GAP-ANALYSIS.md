# Gap analysis — self-service infrastructure without administrator dependency

**Status:** Point-in-time assessment · **Date:** 2 August 2026 · **Commit:** `27930ec`
**Audience:** Stakeholders and sponsors of the Shift-Left Infrastructure Provisioning Portal
**Author:** Prepared for Mohammed Khader (Infrastructure lead), assisted by Claude

---

## 1. The question this document answers

> *"Can requesters and approvers manage end-to-end infrastructure operations through this portal, with zero dependency on infrastructure administrators, across any domain?"*

This document assesses the portal against that vision, identifies the gaps, and proposes a sequence to close them. Every claim is verified against the source code, not against status markers in planning documents. Section 9 lists the evidence.

---

## 2. Verdict

**Not yet. The remaining gap is structural, not incremental.**

The portal today is an excellent **decision plane** and an early-stage **execution plane**.

| Layer | What it does | Maturity |
|---|---|---|
| **Decision plane** | Request → validate → size → price → policy-check → approve → govern → audit → report | **~85% complete** |
| **Execution plane** | Actually creating infrastructure and keeping it operable without a human | **~10–15% complete** |

The vision requires the execution plane to reach parity with the decision plane. That remaining work is larger than everything built to date — but it is *additive*, and the existing architecture (separated orchestrator, signed handoff, re-verified approval, per-resource registry) is the correct foundation to carry it.

### The single most important finding

A request for *"Postgres 16, medium, on OCI"* does **not** produce a Postgres database.

`_environment_resource_kind()` ([api/main.py:2014](api/main.py#L2014)) maps every request onto one of exactly three resource types: `oci-bucket`, `oci-instance`, or `aws-bucket`. Unless a component is classified as compute, the request provisions an **object-storage bucket**. Nothing installs, configures, or hands over the requested technology.

**The 46-item technology catalogue is therefore a costing and governance abstraction, not an execution contract.** Any request outside those three shapes still ends its life as a Jira ticket fulfilled manually by an infrastructure administrator — which is precisely the dependency the vision seeks to remove.

---

## 3. What has been built, and is genuinely working

This assessment is critical by design; it should not obscure substantial delivered value. The following are real, tested, and in production use:

- **Governed request lifecycle** — 11 request types (create, clone, sandbox, temporary, reduce, decommission, refresh, restore, DR, add, resize) with server-authoritative validation.
- **Separation of authority** — the portal collects and validates; **Jira holds the approval**; the orchestrator executes only after independently re-verifying. This principle is intact throughout.
- **Cost before approval** — live server-side pricing across five deployment targets, cost/plan attached to the approval ticket, evidence packs, showback/chargeback, budgets, quotas, forecast, variance, optimisation and sustainability reporting.
- **Governance depth** — segregation of duties, four-eyes, approval quorum, SLA with breach escalation, change windows, expiring policy waivers, policy-as-code (OPA), and an append-only hash-chained audit log.
- **Autonomous provisioning path** — a leader-elected background poller advances approved requests to real provisioning without a button click.
- **Day-2 observation** — cloud-state reconciliation, drift detection, health scoring, TTL/renewal, ownership transfer and orphan detection.
- **AI assistance (recommend-only)** — request drafting, cost explanation, failure triage, cross-cloud sizing recommendation, and a natural-language approvals assistant. None can approve, price authoritatively, or provision.
- **Platform operations** — HA (leader lease), rate limiting, tracing, API keys, CLI, event stream, and runtime-editable governance settings.

Approximately 90 of the 126 catalogued features are delivered, covered by **769 automated tests**.

---

## 4. Capability audit by domain

The vision says *"any domain."* This is what a requester can obtain today **without** an administrator.

| Domain | Self-service today | Notes |
|---|---|---|
| Object storage | ✅ Yes | OCI bucket; AWS S3 wired but **unverified** (no credentials supplied) |
| Compute (VM) | ⚠️ Partial | Bare private VM, OCI only; requires administrator-supplied subnet and image OCIDs |
| Decommission | ✅ Yes | Real `terraform destroy` (OCI) |
| Power stop / start | ✅ Yes | OCI compute only; platform-admin gated |
| Database (any engine) | ❌ No | No database is ever created |
| Middleware / application runtime | ❌ No | No configuration management exists |
| Kubernetes / containers | ❌ No | In the catalogue; never provisioned |
| Block / file storage | ❌ No | Not modelled |
| Network — subnet, VLAN, IPAM | ❌ No | **Consumed as an administrator prerequisite** (`var.subnet_ocid`) |
| Firewall / security groups | ❌ No | F-SEC-06 is priority **Must**; not built |
| Load balancer | ❌ No | Not in the catalogue |
| DNS | ❌ No | Not in the catalogue |
| Certificates | ❌ No | F-LCM-05 not built |
| Access / credential delivery | ⚠️ Metadata only | Live vault raises `VaultUnavailable`; mock issues a placeholder link |
| Backup | ⚠️ Recorded only | Extension point; not executed |
| Restore | ❌ No | Orchestrator returns **501** in live mode |
| Resize / reduce capacity | ❌ No | Orchestrator returns **501** in live mode |
| Refresh (data copy) | ❌ No | Orchestrator returns **501** in live mode |
| Patching / upgrades | ❌ No | F-LCM-04 catalogued; not even a request type |
| Monitoring onboarding | ❌ No | F-INT-06 not built |

**Observe rows 15–20.** The day-2 operations — the *"manage end-to-end operations"* half of the vision — are fully governed, priced, approved and audited, but **not executed**. The orchestrator exposes `/refresh`, `/restore` and `/reduce` endpoints that return HTTP 501 when live.

---

## 5. Gaps by priority

### Tier 1 — Blocking the vision outright

| # | Gap | Consequence |
|---|---|---|
| 1 | **Execution breadth**: 3 real resource kinds vs 46 advertised | Most requests still require manual fulfilment |
| 2 | **No configuration management**: ARCHITECTURE §9 promises Terraform **/ Ansible / GitOps**; neither Ansible nor GitOps exists in the repository | A bare VM is not a working service; an administrator must finish the job |
| 3 | **Day-2 mutations are stubs** (resize, refresh, restore) | Every change after day one returns to a human |
| 4 | **Credential delivery is mock** | The requester cannot actually *access* what they requested |
| 5 | **Network is a prerequisite, not a service** | The portal consumes administrator-created VCN/subnet/image; the hardest and most security-sensitive gap |

### Tier 2 — Identity and workflow dependencies

| # | Gap | Consequence |
|---|---|---|
| 6 | **RBAC enforcement is off** (`ROLE_SOURCE=mock`) | Role decisions are not yet driven by real Jira group membership |
| 7 | **Manager routing blocked** — Jira's SDIMD create screen rejects `reporter` | Approvals cannot route to the requester's manager without a Jira administrator change |
| 8 | **Jira workflow and field configuration is administrator-owned** | A class of dependency the portal cannot remove by itself |

### Tier 3 — Domains never modelled

Patching and upgrade requests, certificate lifecycle, observability onboarding, backup verification, and capacity/IP address management.

---

## 6. What "zero dependency, any domain" structurally requires

Three capabilities that do not exist today:

1. **A certified blueprint / module library.** Each catalogue entry must map to a real, parameterised, tested infrastructure-as-code module — turning the catalogue from a price list into an execution contract. *(This is catalogued as F-CAT-10, "blueprint certification pipeline", and is unbuilt.)*
2. **A configuration layer.** Ansible or GitOps, so that a provisioned host becomes a *working service* rather than an empty machine.
3. **A privileged-operations broker.** The portal must hold scoped automation credentials per domain — network, DNS, database, IAM — so it can perform what an administrator performs. Today the orchestrator holds a single OCI identity covering compute and storage only.

Item 3 carries the most governance weight: broadening the portal's privilege is exactly what makes zero-dependency possible, and simultaneously what makes strong approval, segregation of duties and audit non-negotiable. Those controls already exist — which is the strongest argument for proceeding.

---

## 7. Recommended sequence

| Step | Work | Rationale |
|---|---|---|
| **1** | **Close the honesty gap.** Mark which catalogue technologies are genuinely provisionable, and show requesters when an item still requires manual fulfilment. | Small, fast, high trust value. Stops promising what only a human can deliver. |
| **2** | **Make one technology truly end-to-end** — e.g. Postgres on OCI: real managed-database module + real vault credential delivery + backup and restore. | Proves the entire pattern on a single vertical slice. |
| **3** | **Day-2 execution for that technology** — implement resize/reduce and restore. | Converts "governed" into "operated". |
| **4** | **Introduce the configuration layer** (Ansible) for VM-based stacks. | Unlocks every non-managed technology. |
| **5** | **Network services** — DNS, load balancer and security groups as governed request types. | Removes the largest remaining administrator dependency. |
| **6** | **Scale the module library** across the remaining domains. | Repeatable once steps 2–5 establish the pattern. |

Parallel, non-blocking: enable live RBAC enforcement (step 6 of Tier 2 requires only the group→role map to be populated), and resolve the Jira reporter/manager-routing constraint with the Jira administrator.

---

## 8. Honest caveats

- This is a **point-in-time** assessment at commit `27930ec`. Re-verify before citing it later.
- "~85%" and "~10–15%" are **judgement calls** for communication, not measured metrics. The capability table in section 4 is the factual basis.
- AWS S3 provisioning is **wired but never executed** — no credentials exist in this environment, and verifying it would create real billable resources.
- Azure and GCP have **pricing adapters only**; no provisioning path exists for either.
- The assessment covers *capability*, not code quality. The delivered code is well tested and the architectural principles are consistently upheld.

---

## 9. Method and evidence

Claims were verified directly against source, not planning documents:

| Claim | Verified by |
|---|---|
| Three real resource kinds | `find orchestrator/terraform -name "*.tf"`; resource-kind grep across `api/` and `orchestrator/` |
| Catalogue ≠ execution contract | [api/main.py:2014](api/main.py#L2014) `_environment_resource_kind()` |
| Day-2 operations return 501 | [orchestrator/main.py](orchestrator/main.py) `/refresh`, `/restore`, `/reduce` |
| Vault is mock-only | [api/vault.py](api/vault.py) — live mode raises `VaultUnavailable` |
| No Ansible or GitOps | Filesystem search for `ansible`/`gitops`/`playbook` — no matches |
| Network policy not built | No `network_policy` in `policy/` or `orchestrator/terraform/` |
| Compute presumes admin-built network | [orchestrator/terraform/main.tf](orchestrator/terraform/main.tf) — `var.subnet_ocid`, `var.image_ocid` |
| RBAC enforcement off | Running API environment: `ROLE_SOURCE=mock` |
| Feature and test counts | 126 catalogued features (ARCHITECTURE.md); 769 passing tests |

---

## 10. Progress log

The assessment above is deliberately left unchanged as a point-in-time record. Work completed against section 7 is tracked here.

| Step | Status | Delivered |
|---|---|---|
| **1 — Close the honesty gap** | ✅ Done, 2 Aug 2026 | `api/fulfilment.py` classifies every (technology, target) pair as **automated** or **manual** using the same rules as the provisioner, with a test that fails if the two drift apart. `/api/lookups` exposes `automated_targets`; the request form badges each catalogue card and warns before submission when the infrastructure team must fulfil the request. **Measured result: 5 of 46 technologies are automated on at least one target; 41 are manual everywhere.** |
| 2 — One technology end-to-end | Not started | |
| 3 — Day-2 execution | Not started | |
| 4 — Configuration layer | Not started | |
| 5 — Network services | Not started | |
| 6 — Scale the module library | Not started | |
