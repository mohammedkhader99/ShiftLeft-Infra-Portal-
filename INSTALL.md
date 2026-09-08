# Moving the portal to another machine

Everything here was read out of this repository on 2026-09-05 — the compose
file, the three Dockerfiles, `.env.example` and `.gitignore` — rather than
recalled. If a version number below disagrees with the file it came from, the
file is right and this guide is stale.

---

## 1. What you are actually moving

The portal is **five containers** started by one command. Almost nothing runs on
the host, which is what makes the move simple:

| Container | What it is | Host port |
|---|---|---|
| `webapp` | the React portal people use | **5173** |
| `api` | the FastAPI backend (Python 3.12) | 8081 |
| `orchestrator` | executes Terraform against the cloud (Python 3.12 + Terraform 1.9.8) | 9091 |
| `db` | PostgreSQL 16 — every request, approval and audit entry | 5432 |
| `opa` | Open Policy Agent, reads `policy/` | 8181 |

Two more exist but stay off unless asked for: `vault` (profile `dev-vault`) and
`portal`, the parked HTMX front end (profile `fallback`).

**Postgres, Terraform, Node and OPA are all inside containers.** You do not
install any of them on the new machine.

Alongside the containers there are **four things that live outside git** and are
the whole reason a move needs a plan:

1. `.env` — settings and secrets
2. `secrets/` — the OCI private key and the container credentials file
3. `generated/` — the recipes the agent has proved (see §5, this one bites)
4. two Docker volumes — `infra-portal_db_data` and `infra-portal_tfstate`

---

## 2. Choose what you need

**A. Run the portal.** Docker and git. That is the whole list.

**B. Run it *and* run the tests / work on the code.** Add Python 3.12. Node is
only needed if you want the front end's dev server outside Docker; the
production build happens inside the image.

---

## 3. Prerequisites by machine

### Windows 10/11 PC

| Install | Version | Why |
|---|---|---|
| Docker Desktop | current, **WSL 2 backend** | runs all five containers |
| Git for Windows | current | version control, and it provides the `bash` shell used below |
| Python | **3.12.x** | only for option B |

- In Docker Desktop → Settings → Resources, give it **at least 8 GB RAM** and
  **20 GB disk**. The orchestrator image alone is 2.55 GB.
- Enable WSL 2 first (`wsl --install` in an admin PowerShell, then reboot) if
  Docker Desktop asks for it.
- Both PowerShell and Git Bash work. The commands below are Git Bash; where they
  differ I give both.

### Apple Mac

| Install | Version | Why |
|---|---|---|
| Docker Desktop for Mac | current | runs all five containers |
| Git | comes with Xcode command line tools (`xcode-select --install`) | |
| Python | **3.12.x** (`brew install python@3.12`) | only for option B |

> **⚠ Apple Silicon (M1–M4) needs one change.** `orchestrator/Dockerfile`
> downloads `terraform_1.9.8_linux_amd64.zip` — the Intel build, hard-coded. On
> an Apple Silicon Mac the base image is arm64, so that Terraform binary will not
> run and the orchestrator will fail the moment it tries to provision.
>
> Pick one:
>
> - **Simplest:** force that one service to Intel emulation. Add to the
>   `orchestrator:` service in `docker-compose.yml`:
>   ```yaml
>       platform: linux/amd64
>   ```
>   Slower, but nothing else changes.
> - **Cleaner:** change the Dockerfile to fetch `linux_arm64` instead, and
>   rebuild. Do this only if you are comfortable editing it.
>
> Intel Macs need neither.

### Linux server (Ubuntu / RHEL / Oracle Linux)

| Install | Version | Why |
|---|---|---|
| Docker Engine + Compose plugin | Engine 24+, Compose **v2** | runs all five containers |
| git | current | |
| Python | **3.12.x** | only for option B |

Do **not** install Docker Desktop on a server — use Docker Engine:

```bash
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker "$USER"   # then log out and back in
docker compose version            # must print v2.x or later
```

If a firewall is on, open **5173** to whoever uses the portal. Keep 5432, 8081,
9091 and 8181 closed to the outside — they are internal.

---

## 4. Get the code across

**This repository has no git remote.** `git remote -v` prints nothing, so there
is nowhere to clone from. Choose one:

**Option 1 — push it somewhere first (best if you want history and a backup).**
On the old machine:
```bash
git remote add origin <your-git-server-url>
git push -u origin master
```
Then on the new machine: `git clone <that url>`.

**Option 2 — copy the folder.** Copy the whole `infra-portal` directory,
including the hidden `.git`, `.env` and `secrets`. Skip these, which are large
and machine-specific and will be rebuilt:

```
.venv/          node_modules/          __pycache__/
```

**Option 3 — a git bundle** (one file, keeps all history, no server):
```bash
git bundle create infra-portal.bundle --all
# copy that single file over, then:
git clone infra-portal.bundle infra-portal
```
Note that options 1 and 3 carry **only what git tracks** — you must still move
§5 by hand.

---

## 5. The four things git does not carry

This is the part that silently breaks a move — and it is measured, not assumed.
Option 3 above was run for real on 2026-09-05: a 2.3 MB bundle, cloned beside
the original. What the clone contained is exactly the warning below.

```
present : docker-compose.yml, generated/README.md
ABSENT  : .env
ABSENT  : secrets/
ABSENT  : generated/profiles/     <- all 8 proved recipes, gone
```

A portal started from that clone alone would offer MySQL, MongoDB, RabbitMQ and
the rest, and be able to build none of them.

### `.env` — settings and secrets
Git-ignored by design. Either copy your existing `.env`, or start from the
template and fill it in:

```bash
cp .env.example .env
```

`.env.example` documents 84 settings, and nearly all have safe defaults. Only
**five have no default and must be set**:

| Variable | What to put |
|---|---|
| `POSTGRES_DB` | e.g. `infra_portal` |
| `POSTGRES_USER` | e.g. `infra_portal` |
| `POSTGRES_PASSWORD` | a password you choose |
| `USE_MOCK` | `true` for a safe first start |
| `WEBHOOK_SECRET` | a long random string; API and orchestrator must agree |

If you are copying the old `.env`, keep `POSTGRES_USER` / `POSTGRES_PASSWORD`
identical to the old machine's, or the copied database will refuse the login.

**Start with `USE_MOCK=true`.** Nothing real is created, nothing is billed. Turn
the live switches on only once the portal is up and you have decided to.

### `secrets/` — credentials
Git-ignored. Copy the directory across **securely** (not email, not a chat
message). It currently holds:

```
secrets/container.env        credentials an operator supplies to containers
secrets/oci_api_key.pem      the OCI API signing key
secrets/oke_node_key         SSH key for OKE nodes
secrets/oke_node_key.pub
```

On macOS and Linux, tighten the key afterwards:
```bash
chmod 600 secrets/oci_api_key.pem secrets/oke_node_key
```

### `generated/` — the proved recipes ← **easy to miss**
Git tracks only `generated/README.md`; the eight recipe files under
`generated/profiles/` are ignored. They are what the agent proved on real
machines — MySQL, MongoDB, RabbitMQ, Vault and the rest.

**Copy `generated/` by hand.** If you do not, the database will still say those
technologies are certified while nothing can build them, and a request will
produce a bare machine with no software on it. That exact failure has happened
in this project before and is what the certification gates exist to prevent.

### The two Docker volumes — your data

`infra-portal_db_data` is every request, approval, audit entry, blueprint and
proof. `infra-portal_tfstate` is Terraform's record of the **real cloud
resources it has built**.

> **⚠ Losing `tfstate` orphans real infrastructure.** Terraform would no longer
> know those OCI resources exist, so the portal could neither manage nor destroy
> them, and they would bill until someone deletes them by hand.

On the **old** machine, with the containers running:

```bash
# The database, as a portable dump. Written INSIDE the container and then copied
# out, rather than redirected with `>`. PowerShell's `>` re-encodes what passes
# through it, which silently corrupts a binary `-Fc` dump -- you get a file that
# looks plausible and fails at restore. This form behaves the same in PowerShell,
# Git Bash, and on a Mac.
docker exec infra-portal-db-1 pg_dump -U <POSTGRES_USER> -d <POSTGRES_DB> \
  -Fc -f /tmp/portal-db.dump
docker cp infra-portal-db-1:/tmp/portal-db.dump ./portal-db.dump

# the terraform state, as a tar
docker run --rm -v infra-portal_tfstate:/from -v "$PWD":/to alpine \
  tar czf /to/tfstate.tgz -C /from .
```

On the **new** machine, after §6 has started the database once:

```bash
docker cp portal-db.dump infra-portal-db-1:/tmp/portal-db.dump
docker exec infra-portal-db-1 pg_restore -U <POSTGRES_USER> -d <POSTGRES_DB> \
  --clean --if-exists /tmp/portal-db.dump

docker compose stop orchestrator
docker run --rm -v infra-portal_tfstate:/to -v "$PWD":/from alpine \
  tar xzf /from/tfstate.tgz -C /to
docker compose start orchestrator
```

> **⚠ On Windows, run the commands in this section from PowerShell, not Git
> Bash.** Git Bash rewrites anything shaped like a Unix path before Docker sees
> it, so `/tmp/portal-db.dump` reaches the container as
> `C:/Users/.../AppData/Local/Temp/portal-db.dump` and the command fails with a
> puzzling *"could not open input file"* naming a path you never typed. The
> `$PWD` in the `-v` mount is rewritten the same way. Both were hit doing
> exactly this on 2026-09-07. In PowerShell the commands work as written, except
> that `"$PWD"` becomes `"${PWD}:/from"`.

If instead you want a **fresh start** with no history, skip both -- but the
database does **not** seed itself, and this guide said for a while that it did.
First boot creates the empty tables and stops there. Load the catalogue by hand:

```bash
docker compose exec api python -m db.seed
```

It prints what it wrote -- on 2026-09-07 that was 48 technologies, 24 rate cards,
192 sizing anchors, 3 projects, 3 cost centres and 2 environments. `db/seed.py`
gives that command in its own docstring, and `api/main.py` describes the seed as
"a script nobody runs on deploy".

Skip it and the portal still starts, still answers `/health`, and still serves a
front end -- it simply has an empty catalogue. Measured on a fresh clone that
day: `/api/lookups` returned `technologies: 0`, and the request form offered
nothing to request. You keep the code and lose the record.

---

## 6. Build and start

From the repository folder:

```bash
DOCKER_BUILDKIT=0 docker compose build
docker compose up -d
```

> **Why `DOCKER_BUILDKIT=0`.** With BuildKit on, this project has repeatedly
> produced images that silently kept **stale copies of edited files**, so a fix
> appeared not to work. Turn it off for builds here.

On Windows PowerShell, set it separately:
```powershell
$env:DOCKER_BUILDKIT = "0"
docker compose build
docker compose up -d
```

The first build downloads Terraform and its OCI/AWS providers and takes
**10–30 minutes** with a good connection. Later builds are much faster.

---

## 7. What a working result looks like

```bash
docker compose ps
```
`db`, `opa`, `api`, `orchestrator` and `webapp` all **Up**, and `db` **healthy**.

Then, in order:

```bash
curl http://localhost:8081/health          # the API answers
curl http://localhost:8181/health          # OPA answers
```

Open **http://localhost:5173** in a browser. You should see the portal, be able
to open a new request, and see technologies listed — MySQL Community Server
among them, priced at **90.59 AED/month** for Small, not 0.00.

If the database was restored, your existing requests appear under My Requests.
If it was seeded fresh, that list is empty and that is correct -- but the
**technology** list must not be. An empty catalogue means the seed in §5 was
skipped, not that something is broken.

---

## 8. Running the tests (option B only)

```bash
python3.12 -m venv .venv
source .venv/bin/activate        # Windows: .venv/Scripts/activate
pip install -r requirements.txt
python -m pytest -q
```

Expect roughly **3,400 tests passing** and a handful skipped. On a slow or
throttled laptop the full suite takes well over an hour; on a normal machine,
about twenty minutes.

### The policy tests

The Rego rules have their own suite, run by OPA rather than by pytest:

```bash
docker run --rm -v "$PWD/policy:/policies:ro" openpolicyagent/opa:latest test /policies -v
```

Expect **39 passing**.

### The placement screen's render check

There is no test runner in the frontend, so the placement step has a plain
script that server-renders it in every state against real captured API answers.
`tsc` proves the types line up; this proves the component actually runs — and it
sits inside the request form, so a crash there takes the whole form down rather
than just the new step.

It needs Docker, because there is no node on the host:

```bash
docker build --target build -t placement-render-check ./webapp
docker run --rm \
  -v "$PWD/webapp/frontend/tests:/tests:ro" \
  -v "$PWD/webapp/frontend/src:/frontend/src:ro" \
  -w /frontend placement-render-check sh -c \
  'cp /tests/placement-step.render.tsx . && ./node_modules/.bin/esbuild ./placement-step.render.tsx --bundle --platform=node --format=cjs --outfile=/tmp/r.cjs --jsx=automatic --loader:.json=json && node /tmp/r.cjs'
```

A pass ends with `ALL RENDER CHECKS PASSED`.

**On Windows, run that from PowerShell and not Git Bash.** Git Bash rewrites the
container paths — `-w /frontend` arrives as `C:/Program Files/Git/frontend` and
Docker refuses it. This is the same path-mangling trap warned about in §5.

---

## 9. If something goes wrong

| Symptom | Cause and fix |
|---|---|
| `port is already allocated` | Something else uses 5173/8081/5432. Stop it, or change the left-hand number in `docker-compose.yml`. |
| Build fails downloading Terraform | No internet, or a proxy. The first build needs to reach `releases.hashicorp.com` and Docker Hub. |
| Orchestrator exits immediately on an Apple Silicon Mac | The Terraform architecture problem in §3 — apply one of the two fixes. |
| `password authentication failed for user` | `.env` credentials do not match the restored database. Use the old machine's values. |
| Containers restart or the machine crawls | Docker has too little memory. Give it 8 GB. |
| Catalogue shows technologies but requests fail to configure | `generated/` was not copied (§5). |
| An edit does not seem to take effect | Rebuild with `DOCKER_BUILDKIT=0` and confirm the change is inside the container. |

---

## 10. Do not copy

`.venv/`, `node_modules/`, `__pycache__/`, `*.pyc`, and the local Docker images.
All are rebuilt on the new machine and are specific to the old one.

**Never put `.env` or anything under `secrets/` into git**, on either machine.
