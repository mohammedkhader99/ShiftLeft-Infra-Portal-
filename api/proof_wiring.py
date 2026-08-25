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
import time
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
        # Generous, because run_proof now READS this: the plan summary decides
        # whether the recipe creates anything at all, and truncating it to 300
        # characters would silently discard the evidence.
        return True, (response.text or "")[:4000]
    return post


def make_image_delete(post):
    """Delete one retired image, in the shape `golden.reap` expects.

    Unreachable or refused is `(False, why)`, which leaves the row alone to be
    retried. Never `(True, ...)` on doubt: a row marked deleted for an image
    still in the tenancy is a cost nobody can find again.
    """
    def delete(image_ocid: str) -> tuple[bool, str]:
        ok, detail = post("/delete-image", {"image_ocid": image_ocid})
        if not ok:
            return False, detail
        try:
            body = json.loads(detail)
        except (ValueError, TypeError):
            return False, f"Unreadable delete response: {(detail or '')[:200]}"
        if not isinstance(body, dict):
            return False, "The delete response was not an object."
        return bool(body.get("deleted")), str(body.get("detail") or "")
    return delete


def make_image_states(post):
    """Ask the orchestrator what OCI says about images we captured.

    `{ocid: lifecycle_state}`, and an EMPTY MAPPING when the question could not
    be asked at all. That distinction is the point: "I could not reach the
    orchestrator" and "the image failed" must not look the same, or a network
    hiccup would condemn perfectly good images. `golden.promote` leaves an
    unanswered row alone and asks again next cycle.
    """
    def ask(image_ocids: list[str]) -> dict[str, str]:
        if not image_ocids:
            return {}
        ok, detail = post("/image-state", {"image_ocids": list(image_ocids)})
        if not ok:
            return {}
        try:
            body = json.loads(detail)
        except (ValueError, TypeError):
            return {}
        states = body.get("states") if isinstance(body, dict) else None
        return states if isinstance(states, dict) else {}
    return ask


def make_capture(post):
    """Keep the proven machine, in the shape run_proof expects: always a dict.

    `make_post` hands back the orchestrator's body as TEXT, so this is the seam
    that turns it into the mapping the caller reads. Every failure — a refused
    call, an unreachable orchestrator, a body that is not JSON — becomes
    `{"captured": False, "detail": ...}` rather than an exception, because the
    proof's verdict is already decided by the time this runs and must not be
    reachable from here.
    """
    def capture(reference: str, payload: dict) -> dict:
        ok, detail = post("/capture-image", payload)
        if not ok:
            return {"captured": False, "detail": detail}
        try:
            body = json.loads(detail)
        except (ValueError, TypeError):
            return {"captured": False,
                    "detail": f"Unreadable capture response: {(detail or '')[:200]}"}
        return body if isinstance(body, dict) else {
            "captured": False, "detail": "The capture response was not an object."}
    return capture


def make_price(session: Session, target: str):
    """The plan's monthly cost, from the portal's own rate cards.

    Returns None when it cannot be priced, and `check_cost` refuses on None —
    "we could not work out what this costs" is not a reason to spend money
    unattended.
    """
    def price(components: list[dict]) -> float | None:
        try:
            estimate = pricing.estimate_cost(components, target, session)
            # A total of 0.00 means two very different things: "this is free" and
            # "we could not price it". check_cost approves the first and must
            # refuse the second, so the difference has to survive to here.
            if estimate.get("unpriced"):
                return None
            monthly = estimate.get("totals", {}).get("monthly")
            return float(monthly) if monthly is not None else None
        except Exception:  # noqa: BLE001 — an unpriceable plan is a refusal, not a crash
            return None
    return price


def make_verify(post, *, deadline_seconds: int | None = None,
                interval_seconds: int | None = None, sleep=time.sleep):
    """Ask the orchestrator whether what it built is actually healthy — patiently.

    IT WAITS, and that is the whole point. Terraform returns when an instance
    reaches RUNNING, which is BEFORE cloud-init has finished installing anything.
    Asking once, straight after apply, finds no report and calls a healthy
    machine silent — which would refuse to certify a component that works. A
    proof stricter than the real path is worse than no proof: it takes working
    things off the menu.

    So it waits on the SAME terms the real request path waits on
    (BOOT_VERIFY_DEADLINE_MINUTES, BOOT_VERIFY_POLL_SECONDS) and reads the SAME
    fields of the same response — `checked`, `settled`, `all_ok`. Reading it a
    second, private way is how two services come to disagree about one answer.
    """
    from api import settings

    def _cfg(key: str, fallback: int) -> int:
        try:
            return max(0, int(settings.env(key, str(fallback))))
        except (TypeError, ValueError):
            return fallback

    def verify(reference: str, payload: dict) -> tuple[bool, str]:
        # THE PROOF'S OWN PAYLOAD, forwarded whole. Composing a second one here
        # is what let /verify ask about a different resource kind than /apply
        # built, and an earlier version of that same mistake posted an empty
        # policy_input and asked "is nothing healthy?".
        deadline = (deadline_seconds if deadline_seconds is not None
                    else _cfg("BOOT_VERIFY_DEADLINE_MINUTES", 15) * 60)
        interval = (interval_seconds if interval_seconds is not None
                    else _cfg("BOOT_VERIFY_POLL_SECONDS", 20))
        give_up_at = time.monotonic() + deadline
        payload = {**(payload or {}), "reference": reference, "proof": True}
        last_problem = "no answer was ever read"

        while True:
            ok, detail = post("/verify", payload)
            if not ok:
                last_problem = f"could not verify: {detail}"
            else:
                try:
                    body = json.loads(detail) if detail.strip().startswith("{") else {}
                except ValueError:
                    body = {}
                # No news is NOT good news. A verification that returned nothing
                # readable is unknown, and unknown must not read as healthy —
                # that inference is what reported four broken machines as
                # provisioned.
                if not body:
                    last_problem = "the verification result could not be read"
                elif not body.get("checked", 0):
                    # Nothing here CAN report — a bucket, a managed database, or
                    # mock mode. Silence is the complete and correct answer, and
                    # waiting for it would burn the deadline every time.
                    return True, "nothing here files a report; it built and tore down"
                elif body.get("settled"):
                    if body.get("all_ok"):
                        return True, "verified healthy"
                    # THE MACHINE'S OWN WORDS, not a verdict to take on trust.
                    #
                    # A machine-backed resource reports `problems`; only the
                    # resource-state path (a bucket, a cluster) sets `note`.
                    # Reading `note` alone collapsed "package keycloak is not
                    # installed" into the bare word "broken" on REQ-2026-0177 —
                    # discarding the one thing a boot report exists to carry, and
                    # leaving whoever reads the failure nothing to act on.
                    hurt = (body.get("broken") or []) + (body.get("unreadable") or [])
                    said = []
                    for r in (body.get("resources") or []):
                        if r.get("state") not in ("broken", "unreadable"):
                            continue
                        words = ("; ".join(r.get("problems") or [])
                                 or r.get("note") or "broken")
                        said.append(f"{r.get('kind')}: {words}")
                    return False, ("; ".join(said)
                                   or f"not healthy: {', '.join(hurt)}")[:400]
                else:
                    last_problem = ("still waiting for "
                                    f"{', '.join(body.get('waiting') or ['a machine'])} "
                                    "to report")

            # Read the clock ONCE per pass. Asking twice lets the deadline fall
            # between the two readings, which turns one fault into another.
            if time.monotonic() >= give_up_at:
                return False, last_problem
            sleep(interval)
    return verify


def make_withdraw(root: pathlib.Path | None = None):
    """Remove a published draft from the generated store.

    Needed because a technology profile must be in the store BEFORE its proof can
    use it — the orchestrator reads it when rendering first-boot configuration —
    so a proof that fails has to take it back out. Left behind, an unproven
    profile makes an uninstallable technology look installable to every later
    request.

    Confined to the store by the same rule as writing, and silent about files
    that are already gone: withdrawing twice is not an error.
    """
    base = (root or GENERATED_ROOT).resolve()

    def withdraw(files: dict[str, str]) -> list[str]:
        removed: list[str] = []
        for path in (files or {}):
            name = pathlib.PurePosixPath(str(path)).name
            if not name or name.startswith("."):
                continue
            for sub in ("profiles", "blueprints", "terraform"):
                target = (base / sub / name).resolve()
                if not target.is_relative_to(base):
                    raise ValueError(
                        f"refusing to delete outside the generated store: {path}")
                if target.exists():
                    target.unlink()
                    removed.append(str(target))
        return removed
    return withdraw


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
            # Three stores, by what the file IS. A technology profile is not
            # Terraform and must not land among modules, or configure.py
            # would never read it and the machine would boot with nothing
            # installed while every step reported success.
            if name.endswith(".json"):
                sub = "profiles"
            elif name.endswith((".yaml", ".yml")):
                sub = "blueprints"
            else:
                sub = "terraform"
            target = (base / sub / name).resolve()
            if not target.is_relative_to(base):
                raise ValueError(
                    f"refusing to write outside the generated store: {path}")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content or "", encoding="utf-8")
            written.append(str(target))
        return written
    return publish
