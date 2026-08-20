# PROPOSAL — A self-certifying catalogue

**Status: DRAFT FOR APPROVAL. Nothing here is in force.**
`ARCHITECTURE.md` and `PLAN.md` are unchanged. This file exists so the wording can
be argued with before it becomes authority.

Raised by Mohammed Khader, 2026-08-18: *"every component we are manually
certifying… I don't want to have this human dependency."*

---

## The problem, stated precisely

Certification today means **an operator clicked a button**. That is an opinion,
and opinions are wrong. `oci-oke` was certified by hand on 15 August and then
failed four consecutive real requests. `postgres16` was certified and then failed
four times. In both cases the portal offered users a component that could not be
built.

So the human dependency is real — but removing the *button* is not the fix. A new
OCI service cannot be provisioned until a Terraform module exists, and
auto-certifying a component with no module makes the portal lie. Both Oracle
tiles in the catalogue today (`oci-adb`, `oracle-db`) carry
`resource_kind = oci-bucket`. Certify them as they stand and the portal builds an
Object Storage bucket and calls it a database.

What must be removed is the human as **judge of whether something works**.

## Evidence this is the right shape

Eight defects were found on 2026-08-17, every one a plausible constant that was
never checked against the service:

| Defect | Looked like | Did |
|---|---|---|
| OKE worker image | `sources[length - 1]` | picked Oracle Linux 7.9 of 120 images |
| kubelet | — | crash-looped on cgroup v1, node never registered |
| bastion | `assign_public_ip = true` | OCI refused the VNIC, killed the apply |
| k8s version | `v1.29.1` | retired by OCI |
| psql version | `"14"` | catalogue sold 16 |
| psql shape | f-string concatenation | `.1.4GB`, a name OCI never published |
| durability | `is_regionally_durable = true` | Dubai has one AD |
| storage | `iops = 75000` | billed on every database, chosen by nobody |

Four were introduced by Claude *with the provider documentation available*. Three
were dictionary keys guessed rather than read — and each was covered by a test
that used the same wrong guess, so the tests passed.

**Conclusion: AI-generated infrastructure code must be gated by a real build, not
by review alone, and not by tests the same author wrote.**

---

## ARCHITECTURE.md amendments

### §2 Architectural principles — ADD

> **P-CAT-1. Catalogue facts are fetched, never hard-coded.** Versions, shapes,
> images and service limits are asked of the cloud at the moment they are needed
> and cached briefly. A constant in this repository describing a cloud's offering
> is a defect waiting for a date. Already honoured by `kubernetes_versions.py`
> and `postgres_shapes.py`; the eight defects above are what violating it costs.

> **P-CAT-2. Certification is earned by evidence and revoked by evidence.** A
> component is certified because a proof build provisioned it, verified it
> healthy, priced it within budget and destroyed it — not because a person said
> so. Certification expires.

### §4 Authority separation — ADD a fourth role

Today: the portal collects and validates, Jira holds the approval, the
orchestrator executes. Certification is unowned, which is how a button became the
authority. Add:

> **The certification runner attests.** It neither approves nor executes user
> requests. It provisions only into the sandbox tier, only resources it created,
> and it destroys everything it builds. Its verdict is a fact about a build, not
> a permission.

### §7 Agent architecture and guardrails — EXTEND

The existing rule (*the agent recommends; it never decides or executes*) stays
**unchanged**. Make its reach explicit:

> This covers **generated infrastructure code**. The agent may draft a Terraform
> module and blueprint, and may diagnose a failed proof and propose a fix. It
> opens a branch and a diff. It never triggers the orchestrator, never holds
> provisioning credentials, and its output reaches users only after a proof build
> passes **and** a human reviews the diff.
>
> **The verdict is never the agent's.** The runner decides deterministically —
> `resource_state` checks, boot reports, a priced plan — so a certification means
> the same thing on every run and is auditable as a fact rather than an opinion.
> The agent explains, diagnoses and redrafts. It does not judge.

Rationale for the split: deciding whether OKE worked was one deterministic check.
Working out *why* it failed took hours of reading 840KB of console output. The
first must be reproducible; the second is exactly what an agent is good at.

### §8 Security architecture — ADD

> A passing proof says a module **builds**, not that it is **safe**. Public
> exposure, encryption, compartment placement and tagging are not detectable by a
> successful build — the OKE bastion carried `assign_public_ip = true` and built
> correctly for months. Human review of an AI-drafted diff is therefore confined
> to security, cost and policy, and is not optional.

### §10.3 (CAT) and §10.4 (FIN) — new features

- **F-CAT-13** Catalogue sync: detect services, versions and shapes OCI offers
  that the portal does not, and raise them as candidates.
- **F-CAT-14** Certification runner: prove, certify, expire.
- **F-CAT-15** Auto-decertification on failed proof or repeated request failure.
- **F-CAT-16** AI blueprint drafter (recommend-only, proof-gated).
- **F-FIN-12** Cost envelope: price the plan before apply; refuse over cap.

---

## PLAN.md increments

Ordered so each is useful alone and nothing depends on the agent.

### Increment C0 — fix the foundation first (prerequisite)

Two defects found 2026-08-17/18 that automation would otherwise be built on:

- the decommission loop: `destroy.handoff` fires every 30s because the request
  status never advances
- the poller can hang on an outbound call while holding its leader lease, with no
  timeout and no failover

**Acceptance:** an approved decommission completes and the request reaches a
terminal status; a hung Jira call cannot stall the poll loop or retain the lease.

### Increment C1 — auto-decertification (no new spend)

Feed signals already collected — `apply-failed`, `verify-failed`, resource-state
checks — back into `Blueprint.status`. N consecutive failures withdraw
certification and surface the reason in the Admin console.

**Acceptance:** plant four consecutive apply failures for a certified blueprint;
it decertifies automatically and the console names why. This alone would have
stopped `oci-oke` being offered while it was failing.

### Increment C2 — certification runner + cost envelope

Scheduled job: provision into the sandbox tier at the smallest resolvable shape →
verify (`resource_state` / boot reports) → destroy → record proof with timestamp.
Price the plan against existing rate cards first and refuse over cap.
`Blueprint.status` becomes derived from the last proof and its TTL.

**Acceptance:** a deliberately broken blueprint fails its proof and does not
certify; a working one certifies unattended; a blueprint whose plan exceeds the
cap is refused *before* apply.

**Teardown scoping — the acceptance that matters most.** An earlier draft of this
increment said proof builds are "hard-scoped to the sandbox tier". With
Development *as* the sandbox that is no longer protection: the tier is full of
real environments people are using. Scope by **marker, not tier**:

- a test that the runner refuses to destroy a resource lacking its proof marker,
  including one in the sandbox tier it is otherwise entitled to build in
- a test that a proof run destroys every resource it created and nothing else,
  with a real un-marked resource present in the same tier
- a test that an unset or unrecognised sandbox tier makes the runner refuse to
  start, rather than defaulting to somewhere

Plant-tested, as with every guard added on 2026-08-17: the tests must be shown
failing against a runner that scopes by tier.

### Increment C3 — catalogue sync

Poll OCI for versions, shapes and images not modelled in the catalogue; raise
candidates. Honest limit: OCI publishes no "list every service" API, so genuinely
new *services* are detected only where an API exposes them. Most real churn is
versions and shapes, which is fully automatable.

**Acceptance:** OCI offers PostgreSQL 13–18 while the catalogue sells only 16;
the sync raises 17 and 18 as candidates without a human noticing first.

### Increment C4 — AI blueprint drafter (last, deliberately)

Agent drafts module + blueprint for a candidate, opens a branch and diff, and on
a failed proof diagnoses and redrafts. Closed loop — draft, prove, diagnose,
redraft — with a deterministic gate it cannot talk past.

**Acceptance:** given a candidate with no module, the agent produces a diff that
passes a proof build unattended, and a human review of that diff is required
before it can serve a real request.

---

## What stays human, permanently

1. **Reviewing the diff** for security, cost and policy — one review of something
   already proven to build.
2. **Approving spend** for proof builds.
3. **Deciding what belongs in the catalogue at all.**

## Decided

**Sandbox tier = Development** (Mohammed Khader, 2026-08-18, "for the time
being"). No seventh tier is created.

This is the pragmatic choice — Development already has a mapped network, and it
is where every proof so far has been built by hand. It carries one risk that must
be designed against rather than accepted:

> **Development is a real tier that real people request into.** Proof resources
> will sit beside genuine development environments, so "destroy everything the
> runner built" and "destroy everything in Development" must never become the
> same query.

Required consequences, to be enforced by tests in C2:

1. The runner tags every resource it creates with a proof marker carrying the
   proof run id.
2. The runner may destroy **only** resources carrying its own marker. A resource
   without one is untouchable, whatever tier it is in.
3. Proof resources are named distinctly so a human reading the OCI console can
   tell at a glance what is a proof and what is someone's work.
4. If the sandbox tier is ever pointed at a tier the runner should not build in,
   that is a configuration error the runner refuses on, not a warning it logs.

Revisit if proof builds start colliding with real Development work — a dedicated
tier costs a little more and removes the whole class of risk.

## Open questions for you

1. **Cadence and budget** — nightly for cheap resources, weekly for expensive?
   An OKE proof is ~35 minutes of live cluster.
2. **Certification TTL** — 7 days? 30?
3. **Does a passing proof plus review auto-merge, or does a human merge?**
