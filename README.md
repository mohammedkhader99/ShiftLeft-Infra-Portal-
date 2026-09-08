# Shift-Left Infrastructure Provisioning Portal

A self-service portal for requesting infrastructure environments that are
**validated, policy-checked, costed and approved before anything is
provisioned** — catching problems at request time rather than at apply time.

Built and maintained by the Infrastructure Management Department (IMD).

---

## The idea

Most provisioning tools find out a request was wrong when Terraform fails
halfway through creating it. This one refuses the request instead.

Before a request can be approved, the system has already re-computed its sizing
server-side, evaluated it against policy, priced it against live cloud rates,
and confirmed that the thing being asked for can actually be built. A request
that survives all four is one the orchestrator can execute without surprises.

**Four request types:** create environment · add component · resize component ·
decommission environment or component.

**Targets:** Oracle Cloud Infrastructure, Microsoft Azure, and on-premises.

---

## The governing rule

Authority is split three ways, and no layer may take another's job:

| Layer | Authority | Never does |
|---|---|---|
| **Portal / API** | collects and validates | approve anything |
| **Jira Service Management** | holds the approval | execute anything |
| **Orchestrator** | executes — after independently re-verifying the approval | decide what is allowed |

Everything else follows from that separation:

- **The client holds no authority.** Browser-side validation is UX only. All
  validation, sizing, costing and pricing are re-computed server-side and are
  authoritative. The browser holds no credentials and no pricing logic.
- **The AI agent recommends; it never decides or executes.** It may help a
  requester and suggest sizing, technology or policy interpretation. It never
  holds provisioning credentials, never approves a request, and never triggers
  the orchestrator.
- **Everything privileged is audited.** Every request, validation, estimate,
  policy decision, approval, agent recommendation *with its reasoning*, and
  provisioning action lands in a tamper-evident, append-only trail.

Two principles earned the hard way:

- **Catalogue facts are fetched, never hard-coded.** Versions, shapes, images
  and service limits are asked of the cloud when needed and cached briefly. In
  August 2026 eight hard-coded constants were found to be wrong — including a
  Kubernetes version Oracle had already retired and a worker image chosen by
  list position that turned out to be Oracle Linux 7.
- **Certification is earned by evidence and revoked by evidence.** A blueprint
  is certified because a proof build provisioned it, verified it healthy, priced
  it within budget and destroyed it again — not because someone said so. That
  certification expires after 30 days without fresh proof. Withdrawal is
  automatic; reinstatement is not.

---

## Architecture

Five containers, one command:

| Container | What it is | Host port |
|---|---|---|
| `webapp` | React + TypeScript portal (Vite, IBM Carbon) behind a FastAPI BFF | 5173 |
| `api` | FastAPI backend — validation, sizing, pricing, audit | 8081 |
| `orchestrator` | Executes Terraform against the cloud (Python 3.12 + Terraform 1.9.8) | 9091 |
| `db` | PostgreSQL 16 — every request, approval, audit entry and proof | 5432 |
| `opa` | Open Policy Agent, evaluating the Rego policies in `policy/` | 8181 |

Postgres, Terraform, Node and OPA all run inside containers. Nothing but Docker
is required on the host.

**Stack:** Python 3.12 · FastAPI + Pydantic · SQLAlchemy 2.0 + psycopg ·
PostgreSQL 16 · OPA/Rego · React + Vite + TypeScript · Terraform · Jira Service
Management · Anthropic SDK for the agent layer.

The stack is Python end to end on purpose, so sizing, costing, policy and agent
logic share models and code instead of crossing a language boundary.

---

## Getting started

Full instructions, including moving an existing installation to another machine,
are in **[INSTALL.md](INSTALL.md)**. The short version:

```bash
cp .env.example .env      # then set the five variables that have no default
DOCKER_BUILDKIT=0 docker compose build
docker compose up -d
docker compose exec api python -m db.seed
```

Then open **http://localhost:5173**.

> **`DOCKER_BUILDKIT=0` is deliberate.** With BuildKit on, this project has
> repeatedly produced images that silently kept stale copies of edited files, so
> a fix appeared not to work.

> **The database does not seed itself.** First boot creates empty tables and
> stops. Without `db.seed` the portal starts, answers `/health`, serves a front
> end — and offers a catalogue of nothing.

### Start in mock mode

`USE_MOCK=true` is the default and should stay that way until you have decided
otherwise. In mock mode nothing real is created and nothing is billed. The live
switches are turned on one at a time, deliberately.

### What a working result looks like

```bash
docker compose ps                  # five services Up, db healthy
curl http://localhost:8081/health  # {"ok":true,"mock":true}
curl http://localhost:8181/health  # 200
```

In the portal, a new request should list technologies — MySQL Community Server
among them, priced at **90.59 AED/month** for Small, not 0.00. A zero there
means pricing is not resolving.

---

## Repository layout

| Path | Contains |
|---|---|
| `api/` | FastAPI backend: validation, sizing, pricing, audit, agent endpoints |
| `orchestrator/` | Terraform execution, blueprints, cloud state, proof builds |
| `webapp/` | React front end and its FastAPI BFF |
| `portal/` | The parked HTMX portal, kept behind a Compose profile |
| `db/` | SQLAlchemy models, session handling and the catalogue seed |
| `policy/` | OPA policies in Rego, with their own tests |
| `common/` | Code shared across services: signing, paths, doctrine, security |
| `cli/` | `infractl`, the command-line client |
| `ops/` | Operational scripts — catalogue refresh, batch proving, evidence capture |
| `generated/` | Agent-drafted blueprints and Terraform. Git-ignored by design |

---

## Tests

```bash
python3.12 -m venv .venv
source .venv/bin/activate        # Windows: .venv/Scripts/activate
pip install -r requirements.txt
python -m pytest -q
```

**3,280 tests**, measured at 10–16 minutes on a normal machine. They need no
database and no policy server: the suite is self-contained, and a test that
passes only on the machine that wrote it is treated as a bug.

---

## The documents that govern this project

Read in this order:

| Document | Role |
|---|---|
| **[CLAUDE.md](CLAUDE.md)** | How we work: the standing rules, one increment at a time |
| **[ARCHITECTURE.md](ARCHITECTURE.md)** | The design authority — principles, the 138-feature catalogue across 11 domains, technology decisions |
| **[AGENT-DOCTRINE.md](AGENT-DOCTRINE.md)** | How the agent reasons: reuse → discover → compose → build, and what it may never do alone |
| **[PLAN.md](PLAN.md)** | The build sequence — increments in order, each with an acceptance check |
| **[INSTALL.md](INSTALL.md)** | Running it, and moving it between machines |

`ARCHITECTURE.md` outranks `AGENT-DOCTRINE.md`. Where any two disagree, that is
a defect in the documents and gets resolved before code is written.

Feature IDs such as `F-FIN-07` are the traceability keys: every increment names
the features it implements, so the backlog, the plan and the code stay linked.

---

## Secrets

`.env` and everything under `secrets/` are git-ignored and must stay that way.
Local development uses `.env`; production uses a vault. No credential has ever
been committed to this repository, and it stays that way.
