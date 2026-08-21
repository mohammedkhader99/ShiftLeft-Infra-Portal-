"""The real collaborators for a proof build and the autobuild loop.

`proof.run_proof` and `autobuild.build` take their I/O as arguments so the whole
path — including the failure branches a real orchestrator will not produce on
demand — can be driven by a test. This module supplies the real ones.

It is deliberately thin. Everything here is a translation between an injected
callable's shape and machinery that already exists and is already trusted: the
signed handoff, the authoritative pricing, the verification the poller uses. No
new decisions are made in this file, which is why there is so little of it.

THE PRICE IS THE PORTAL'S, NEVER THE PAYLOAD'S. `price` calls the same
`estimate_cost` the request form and the cost API use, and the orchestrator
re-prices independently before it will act on a proof (ARCHITECTURE.md §4). Two
prices computed from the same authoritative source, in two services, neither
taking the other's word.
"""

from __future__ import annotations

import json
import os
import pathlib
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from api import pricing

# Where agent-written blueprints land. The same directory the orchestrator has
# mounted at /generated — the API writes, the orchestrator discovers.
GENERATED_ROOT = pathlib.Path(os.getenv("GENERATED_ROOT", "/generated"))


def make_post(post_to_orchestrator, sign, secret: str):
    """A signed handoff, in the shape run_proof expects: (ok, detail)."""
    def post(path: str, payload: dict) -> tuple[bool, str]:
        body = json.dumps({**payload, "contract_version": "1.0",
                           "issued_at": datetime.now(timezone.utc).isoformat()},
                          sort_keys=True).encode()
        response, error = post_to_orchestrator(body, sign(secret, body), path=path)
        if response is None:
            return False, error or "the orchestrator could not be reached"
        if response.status_code != 200:
            # The orchestrator's refusal text is the useful part — it names which
            # bound was exceeded (sandbox tier, cost cap, an unproven reference).
            return False, f"{response.status_code}: {response.text[:300]}"
        return True, (response.text or "")[:300]
    return post


def make_price(session: Session, target: str):
    """The plan's monthly cost, from the portal's own rate cards.

    Returns None when it cannot be priced, and `check_cost` refuses on None —
    "we could not work out what this costs" is not a reason to spend money
    unattended.
    """
    def price(components: list[dict]) -> float | None:
        try:
            totals = pricing.estimate_cost(components, target, session).get("totals", {})
            monthly = totals.get("monthly")
            return float(monthly) if monthly is not None else None
        except Exception:  # noqa: BLE001 — an unpriceable plan is a refusal, not a crash
            return None
    return price


def make_verify(post):
    """Ask the orchestrator whether what it built is actually healthy.

    The same /verify the poller uses after a real request: boot self-reports for
    machines, resource-state checks for the things that file none. Terraform
    exiting zero says an API call was accepted; this says the thing works.
    """
    def verify(reference: str) -> tuple[bool, str]:
        ok, detail = post("/verify", {"reference": reference,
                                      "proof": True,
                                      "policy_input": {}})
        if not ok:
            return False, f"could not verify: {detail}"
        try:
            body = json.loads(detail) if detail.strip().startswith("{") else {}
        except ValueError:
            body = {}
        # No news is NOT good news. A verification that returned nothing readable
        # is unknown, and unknown must not read as healthy — that inference is
        # what reported four broken machines as provisioned.
        if not body:
            return False, "the verification result could not be read"
        resources = body.get("resources") or []
        broken = [r for r in resources if r.get("state") in ("broken", "unreadable")]
        waiting = [r for r in resources if r.get("state") == "waiting"]
        if broken:
            return False, "; ".join(f"{r['kind']}: {r.get('note', 'broken')}"
                                    for r in broken)[:300]
        if waiting:
            return False, "still waiting for a machine to report"
        return True, "verified healthy"
    return verify


def make_publish(root: pathlib.Path | None = None):
    """Write an agent's draft into the generated store.

    Called ONLY after every gate has passed (see autobuild.build). Paths are
    confined to the store: a draft naming `../../orchestrator/blueprints/x.yaml`
    would otherwise overwrite a reviewed recipe, which is the same impersonation
    the registry refuses at discovery — refused here too, because two doors into
    the same room need two locks.
    """
    base = (root or GENERATED_ROOT).resolve()

    def publish(files: dict[str, str]) -> list[str]:
        written: list[str] = []
        for path, content in (files or {}).items():
            # A draft names repository-style paths; only the tail matters here.
            name = pathlib.PurePosixPath(str(path)).name
            if not name or name.startswith("."):
                continue
            sub = "blueprints" if name.endswith((".yaml", ".yml")) else "terraform"
            target = (base / sub / name).resolve()
            if not target.is_relative_to(base):
                raise ValueError(
                    f"refusing to write outside the generated store: {path}")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content or "", encoding="utf-8")
            written.append(str(target))
        return written
    return publish
