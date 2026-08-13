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
| **2 — One technology end-to-end** | ◐ In progress | **Provisioning module delivered, 2 Aug 2026.** PostgreSQL on OCI is now `resource_kind = oci-postgres`, provisioned as a real *OCI Database with PostgreSQL* system instead of a placeholder bucket — the first catalogue entry delivered as the thing actually requested. The admin password is referenced by **OCI Vault secret OCID** and never passed as a Terraform value, so it cannot reach the plan file, state, or this repository. Real provisioning is **double-gated** (`OCI_PSQL_ENABLED` plus a DB subnet and vault secret) because this deployment can run autonomously and a managed DB system is billable; absent either, an apply refuses with a clear message. `terraform validate` passes against the real OCI provider schema. **No resource has been created.**<br><br>**Credential delivery delivered, 2 Aug 2026.** `api/vault.py` live mode is now a real HashiCorp Vault integration. For each grant the portal creates a short-lived **child token**, uses it to read the credential with **response wrapping**, and returns a **one-time link**; it stores only the token *accessor*, which cannot authenticate. Revoking the accessor cascades to every lease the token created.<br><br>*Verified end to end against a real Vault with dynamic PostgreSQL credentials* (throwaway containers, since removed): the portal's record contained no username or password; the grantee unwrapped once successfully; a second unwrap was rejected (HTTP 400); and after revoke the database login was **confirmed gone from `pg_roles`**.<br><br>⚠️ *A first implementation used the lease id as the handle. Vault returns `lease_id: ""` on a wrapped response — the lease lives inside the wrapped payload — so revoke silently did nothing while reporting success. Caught only because revocation was verified at the database level, not at the API's return value. A regression test now pins the corrected behaviour.*<br><br>**Backup & restore delivered, 2 Aug 2026.** `orchestrator/backups.py` performs real OCI backups (`create_backup` → a backup OCID the portal stores) and real restores. A backup that fails is no longer recorded as a success — the portal returns an error rather than leaving a restore-point that protects nothing.<br><br>**Restore is not in place, and the portal says so.** OCI managed PostgreSQL has no rollback: a restore creates a *separate* database system with its own endpoint and leaves the original untouched. The response carries `in_place: false` and `verified: false`, the request's status detail and the Jira ticket both state that the application must be repointed, and tests assert the adapter never claims otherwise. A restore therefore creates a second billable system.<br><br>⚠️ **UNVERIFIED against a real OCI tenancy.** The adapter's logic and refusals are tested with the SDK mocked, but no real backup or restore has been exercised — that needs database credentials and would create billable systems. This is the same status as AWS S3 provisioning, and weaker evidence than the credential-delivery slice, which was proven end to end. Exercise it on a throwaway database before relying on it. |
| **3 — Day-2 execution** | ◐ Built, unverified | **Delivered 2 Aug 2026.** A `reduce` request now scales a provisioned VM or managed database **down for real**, replacing the 501 stub. It works by re-planning the target's **Terraform workspace** with the smaller sizing rather than calling the cloud API directly — the workspace is the source of truth, and a direct resize would show up as drift the next apply could revert. Managed-PostgreSQL shapes track the same sizing (`OCI_PSQL_SHAPE_FAMILY`). Refuses for resources with no capacity to reduce (a bucket). Off by default (`REDUCE_MODE`), and needs `PROVISION_MODE=apply`.<br><br>**A real resize restarts the resource** — a flex-shape change reboots a VM, a database shape change restarts the service. The response, the request's status detail and a Jira comment all say so, rather than letting "reduced" imply it happened invisibly.<br><br>🐛 **Bug found and fixed:** `_instance_sizing` applied an 8 GB floor to *every* request. A `small` environment — which the catalogue prices at 2 vCPU / 4 GB — was therefore built with 8 GB, and a reduction down to `small` silently changed nothing while reporting success. Sizing now matches what the catalogue prices; a regression test pins it. No live impact, since no compute has been provisioned yet.<br><br>⚠️ **UNVERIFIED** against real infrastructure: routing, sizing maths and refusals are tested, but no real VM or database has been resized. |
| **4 — Configuration layer** | ◐ Built, unverified | **Delivered 2 Aug 2026.** `orchestrator/configure.py` turns a request's technologies into **cloud-init user-data**, so a new VM installs and enables them itself at first boot — the step that converts a bare machine into a working service.<br><br>*Cloud-init rather than Ansible-over-SSH by necessity:* the VMs are private-only with no public IP and the orchestrator runs outside the VCN, so it cannot reach them to push configuration. Cloud-init inverts the direction; `ansible-pull` can later be bootstrapped the same way.<br><br>Package names are data (`CONFIG_PACKAGE_MAP`, `CONFIG_OS_FAMILY`) because the OS image is the customer's. Installs fail loudly to `/var/log/infra-portal.log` rather than leaving a silently dead VM. Off by default (`CONFIG_ENABLED`).<br><br>⚠️ **Two open dependencies.** (1) **UNVERIFIED** — no VM has been booted with this; the first real OCI VM is still blocked on the IAM grant. (2) The private subnet **must have egress** (NAT or service gateway) or an internal mirror, or the boot-time install cannot work at all.<br><br>Because of that, technologies with a template deliberately **still show as "manual"** in the catalogue. A template that has never run is a plan, not a capability; claiming otherwise would undo what step 1 bought. `CONFIG_VERIFIED_CODES` is empty, a test keeps it that way, and another test keeps the catalogue's list in step with the orchestrator's actual templates. |
| **5 — Network services** | ◐ DNS built, unverified | **DNS delivered 2 Aug 2026.** A new governed `dns` request type: a requester names a **provisioned** environment, and it goes through the normal validate → approve → orchestrator path to create a real OCI DNS record. Naming an environment is one of the most frequent reasons to raise a ticket with the infrastructure team, so this is the highest-frequency dependency removed so far.<br><br>**The record shares the environment's lifecycle.** It is planned into the *target environment's own Terraform workspace* rather than a separate one, so tearing the environment down removes its name too instead of leaving a record pointing at nothing.<br><br>An A record with no explicit value points at the environment's **own private IP** (a new `instance_private_ip` output), so nobody copies addresses around; a CNAME must name its target, which validation enforces. Host names are shape-checked per DNS label. Double-gated (`DNS_MODE=live` plus `OCI_DNS_ENABLED` and a zone), and blank `dns_name` means every existing environment plans exactly as before.<br><br>⚠️ **UNVERIFIED** — no real DNS record has been created; that needs a real zone.<br><br>**Deliberately deferred to their own increments, with reasons:** *security group rules* are a security decision — opening a port should route through OPA guardrails so restricted data cannot become internet-reachable — and *load balancers* are expensive and multi-resource (LB + backend set + listener + health check) and must reference the VMs they front. |
| **6 — Scale the module library** | ◐ Started | **Blueprint registry delivered, 3 Aug 2026.** The orchestrator's `blueprints/` directory *is* the registry: adding a build recipe is adding a manifest, not editing code in several places. Each manifest declares its own preconditions, so the portal distinguishes "certified" from "certified but not configured" instead of that surfacing as an apply-time failure. Certification is a separate, audited human act — shipping a recipe is not approving it. The first externally-supplied module (an Apache HTTP Server recipe written by the platform owner) was reviewed, corrected to the portal's variable contract, and registered through the same path an admin would use.<br><br>**One generic "service on a VM" recipe delivered, 4 Aug 2026.** Most of the catalogue is not a distinct cloud resource — it is a package on a machine. Rather than ~15 near-identical modules, one module takes the package, systemd unit and port as data. Ports are declared once and drive **both** the OS firewall and the network rules, so a service cannot end up running correctly behind a closed port — a failure indistinguishable from a broken install. Wired for nginx and Redis.<br><br>Redis deliberately opens **no port**: it ships without authentication, so exposing 6379 to the subnet would publish an unauthenticated data store to every host that can route to it.<br><br>⚠️ **NOT certified, and the catalogue still shows both as manual.** Package names are inferred from Oracle Linux 9's repositories and have never been installed on a real machine. `configure.VERIFIED_CODES` is empty and a test keeps it that way. Boot testing needs `CONFIG_ENABLED`, subnet egress, and a compartment to apply in — all three outstanding with the platform owner. **Current measured state: 4 of 46 technologies carry a certified blueprint.** |
| **7 — A request is a stack** *(gap found in use, not in the original assessment)* | ✅ Done, 4 Aug 2026 | **REQ-2026-0094 asked for Apache and a compute VM. It built Apache, silently dropped the VM, set the request to `provisioned` and closed its Jira ticket as Resolved.** Verified against the real tenancy: one instance existed, and the Terraform plan had only ever contained one resource.<br><br>The cause was a single line — the request's resource kinds were collapsed with `sorted(kinds)[0]`, so `oci-apache` won over `oci-instance` on **alphabetical order**. Not a rule about which resource mattered; an accident of sorting.<br><br>Plan, apply, destroy and drift now cover **every** resource in a request, each in its own Terraform workspace under `<reference>/<kind>` with its own name. Workspaces provisioned before this change keep their flat path permanently: the state file is there, and a workspace that cannot find its state believes its resource does not exist — it would build a second one and could never destroy the first. A failed apply reports what it already created, because a partially-applied stack has real, billable infrastructure in it.<br><br>**Separately, components with no automated recipe at all** were provisioned "successfully" as a placeholder bucket. A partially fulfilled request now names what was not built — in the portal, in the audit log, and as a comment on the Jira ticket. Reported as a clean success, a requester waits indefinitely for something nobody is going to build.<br><br>⚠️ **The multi-resource path is verified by tests and a live plan, not by a live apply.** No stack has been applied end to end. |
| **8 — One component proven end to end** *(the evidence everything else lacked)* | ✅ **Done, 11 Aug 2026** | **Apache HTTP Server on OCI: requested in the portal, approved in Jira, provisioned, PROVEN SERVING, decommissioned, and confirmed gone.** The first entry in this log that carries no "unverified" caveat.<br><br>**REQ-2026-0100** — approval read from Jira `SDIMD-73532` at 06:50:29, signed handoff at 06:50:30, instance launched 06:51:32, `provisioned` at 06:52:55. **Two minutes twenty-five seconds** from a manager's approval to a running web server, with no infrastructure ticket and no manual step.<br><br>**The verification is the point.** Every previous claim of success meant "Terraform reported it created something". This one asked the machine. Via the Oracle Cloud Agent, on the instance itself:<br><br>`systemctl is-active httpd` → **active**<br>`ss -ltn` → **listening on \*:80 and \*:443**<br>`curl -s -o /dev/null -w '%{http_code}' http://localhost/` → **200**<br>`curl -s http://localhost/` → **the exact page the Terraform module wrote**<br><br>That distinction matters because three separate blueprints this week passed `terraform validate` and would have produced a healthy VM with no working service on it — Apache with the wrong OS image, Kafka with an unreachable archive, OKE with an attribute the provider does not accept. A clean plan proves a module compiles. Only the service answering proves it works.<br><br>**REQ-2026-0101** decommissioned it: approved 10:32:07, `Destroy complete! Resources: 1 destroyed.` at 10:33:39 — **92 seconds**. Verified in OCI rather than taken from the portal's word: the instance is `TERMINATED`, **the boot volume was deleted with it** (the classic orphan — OCI can preserve one and bill for it indefinitely), and the private IP was released, leaving the subnet with zero addresses in use.<br><br>**Every fix from the preceding week is load-bearing in that result:** the resource-kind derivation routing to the certified blueprint rather than a placeholder bucket; the image lookup selecting Oracle Linux instead of the shared Ubuntu default, without which `dnf`, `systemctl` and `firewall-cmd` would all have failed silently; the service gateway giving the private subnet a path to the package repositories; the subnet correction after the VCN was rebuilt; and the hostname read from the VNIC rather than recomputed.<br><br>*Incidental finding, unrelated to the portal:* two unattached 500 GB boot volumes (`aispoc2`, `aispoc2_new`) are billing in this compartment for nothing. They predate the portal and match no request.<br><br>**What this does and does not establish.** It establishes that the whole chain works for one component: catalogue → validation → policy → pricing → Jira approval → signed handoff → independent re-verification → Terraform → a working service → clean teardown. It does **not** establish that any other blueprint works. Managed PostgreSQL, Kafka, OKE, nginx and Redis remain unproven, and the honest measure is still **1 of 46 technologies verified end to end**. |
