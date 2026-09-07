"""Prove a list of technologies overnight, and stop before it costs more than you said.

WHY THIS EXISTS
---------------
Certifying a technology costs roughly forty minutes and two or three real
machines: the ladder drafts a recipe, boots a machine, reads what it reports,
narrows the recipe and proves it again. MySQL and Oracle Database Free were each
driven by hand, one at a time, an afternoon apiece. Thirty more that way is a
week of somebody's attention; as one overnight run it is one report in the
morning saying what certified, what refused, and what each machine actually said.

It changes nothing about WHAT gets certified. Every candidate goes through
`_autobuild_component` -- the same function a requester's approval calls -- so
every gate, every refusal and every teardown behaves exactly as it does for a
real request. This is a scheduler, not a shortcut.

WHAT IT WILL NOT DO
-------------------
This spends real money on real infrastructure, so its limits are the point:

  * DRY RUN BY DEFAULT. Without `--apply` it resolves each candidate against the
    registries and repositories -- which costs nothing and builds nothing -- and
    prints what it WOULD attempt. That report is useful on its own: it says
    which technologies the ladder can even reach.
  * A CAP IS REQUIRED, NOT OPTIONAL, and it is counted in machines rather than
    money because machines are what the portal can actually observe. It stops
    when the next candidate could exceed it, not after.
  * ONE AT A TIME. Never concurrent. Two proofs would provision into the same
    sandbox tier at once, and the teardown that scopes "everything the runner
    built" is the safety-critical query in this system.
  * IT REFUSES TO START while any request is executing or any proof is running,
    the same check made by hand before every proof this project has run.
  * IT STOPS DEAD IF A MACHINE LEAKS. Proof machines are destroyed by the
    runner; a proof recorded as `abandoned` is one that built a machine and
    could NOT tear it down -- "something may still be running under
    <reference>" in its own words -- and continuing would multiply the leak
    rather than discover it. `build()` already stops on the same signal.
  * IT OFFERS WHAT IT PROVES, only with `--offer`, and only what a machine
    certified. That reverses this tool's original rule -- "proving is not
    offering" -- at the reviewer's explicit instruction on 2026-09-05, and the
    reasoning behind the rule still stands where it matters: every catalogue
    incident this project has had came from listing something UNPROVED, and
    nothing unproved is listed here either. What changed is only who decides
    afterwards, and an entry can be withdrawn from the Admin console.

    AN ENTRY ADDED AUTOMATICALLY MUST BE COMPLETE, or it arrives broken in the
    two ways this project has already been caught by: with no delivery model it
    reaches the request form ungrouped, and with no sizing anchors it prices at
    0.00 -- a real machine shown to an approver as free. So the offer writes all
    four things a listing needs, or it writes none of them.
  * IT SKIPS WHAT IS ALREADY CERTIFIED, so a re-run costs nothing for work
    already done and can be used to pick up where a night left off.

    python ops/batch_prove.py                          # what it would attempt
    python ops/batch_prove.py --cap 6 --apply          # at most six machines
    python ops/batch_prove.py --cap 24 --apply --offer # and list what certifies
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

#: Machines a single candidate can reasonably consume before something is wrong.
#: The container rung alone is two -- the question pass and the narrowed pass --
#: and a package rung refused before it climbs adds a third.
MACHINES_PER_CANDIDATE = 3

#: A starting list, and nothing more than that. These are widely used, published
#: as official container images, and open source; none is certified today. Edit
#: it, or pass --only. The point of the tool is the mechanism, not this list.
#: code -> the name a requester reads. Written by a person, because a catalogue
#: that says "Mariadb" reads as unattended, and title-casing a code is exactly
#: the guess this project keeps removing. A code absent here is still proved and
#: still offered; it simply carries its code as its name until someone improves
#: it in the Admin console.
CANDIDATES = {
    "mariadb": "MariaDB",              # the other MySQL; the shape already certified
    "valkey": "Valkey",                # the maintained Redis fork
    "memcached": "Memcached",
    "grafana": "Grafana",
    "prometheus": "Prometheus",
    "minio": "MinIO",                  # S3-compatible object storage on a machine
    "traefik": "Traefik",
    "haproxy": "HAProxy",
    "gitea": "Gitea",
    "sonarqube": "SonarQube",
    "nexus": "Sonatype Nexus",
    "clickhouse": "ClickHouse",
    "cassandra": "Apache Cassandra",
    "neo4j": "Neo4j",
    "etcd": "etcd",
    "consul": "HashiCorp Consul",
    "nats": "NATS",
    "influxdb": "InfluxDB",
    "metabase": "Metabase",
    "wordpress": "WordPress",
}


class Budget:
    """Machines spent against machines allowed.

    Counted in machines, not money: the portal can observe a machine being
    built, and a price it could not read is what let a proof run against an
    unpriced plan all week.
    """

    def __init__(self, cap: int):
        self.cap = int(cap)
        self.spent = 0

    @property
    def left(self) -> int:
        return max(0, self.cap - self.spent)

    def affords(self, machines: int = MACHINES_PER_CANDIDATE) -> bool:
        """Whether the NEXT candidate could finish inside the cap.

        Asked before starting, never after: a cap checked afterwards is a cap
        that has already been exceeded.
        """
        return self.left >= machines

    def spend(self, machines: int) -> None:
        self.spent += max(0, int(machines))


def plan(candidates, certified, cap: int, per_candidate: int = MACHINES_PER_CANDIDATE):
    """Split candidates into what to attempt, what to skip, and why.

    Pure, so the arithmetic that decides how much money this spends can be
    tested without a cloud. `certified` is what the portal already builds.
    """
    seen, attempt, skipped = set(), [], []
    budget = Budget(cap)
    for candidate in candidates:
        code = (candidate or "").strip().lower()
        if not code or code in seen:
            continue
        seen.add(code)
        if code in certified:
            skipped.append((code, "already certified"))
        elif not budget.affords(per_candidate):
            skipped.append((code, f"would exceed the cap of {cap} machine(s)"))
        else:
            attempt.append(code)
            budget.spend(per_candidate)
    return {"attempt": attempt, "skipped": skipped, "reserved": budget.spent}


def stop_reason(*, executing: int, proofs_running: int, leaked: list) -> str:
    """Why this must not start, or must not continue. "" means carry on.

    The first two are the check made by hand before every proof this project has
    run. The third is the one that matters overnight: a proof recorded as
    `abandoned` built a machine and could not tear it down, so every further
    candidate would add to a leak nobody is watching until morning.
    """
    if executing:
        return f"{executing} request(s) are executing"
    if proofs_running:
        return f"{proofs_running} proof(s) already running"
    if leaked:
        return (f"a proof was abandoned: {leaked} -- its machine was built and "
                f"the teardown did not take, so something may still be running "
                f"and nothing further is started")
    return ""


def catalogue_entry(code: str, name: str, profile: dict, proof_reference: str) -> dict:
    """The four things a listing needs, derived from what the machine proved.

    Pure, so the text an approver reads can be tested without a cloud. Every
    value comes from the recipe a machine certified or from the proof that
    certified it -- nothing here is recalled, and nothing is guessed.

    THE DELIVERY MODEL IS NOT INFERRED FROM THE CODE NAME. That guess is what
    called OCI's managed PostgreSQL "software"; it is read from the recipe's own
    shape. Everything this tool can prove runs on a machine of its own, because
    the ladder's rungs all end at `oci/service-vm` -- so `software` is a fact
    about the recipe, not a default.
    """
    container = (profile or {}).get("container") or {}
    ports = [str(p) for p in (profile or {}).get("ports") or []]
    where = container.get("image") or ", ".join(
        (profile or {}).get("rhel", {}).get("packages") or []) or "the machine"
    # WHAT IT RUNS COMES BEFORE WHAT PROVED IT, because `note` is String(300)
    # and the tail is what a cut removes. With the image last, memcached's note
    # ended "Runs docker.io/library/memcache" and prometheus's "quay.io/
    # prometheus/promet" -- the image name, which is the one fact a reader
    # cannot reconstruct, truncated mid-word. A proof reference lost to the same
    # cut is still findable: it is in the audit log and in certification_proof.
    note = (f"{name} on a machine of its own"
            + (f", listening on {', '.join(ports)}" if ports else "")
            + f". Runs {where}."
            + f" Proved on a real machine by {proof_reference}: built, verified "
              f"serving, and destroyed.")
    return {
        "code": code,
        "name": name or code,
        "lifecycle_state": "certified",
        "delivery_model": "software",
        "note": note[:300],
    }


def _live_checks(session):  # pragma: no cover - talks to the database
    from sqlalchemy import select

    from db.models import CertificationProof, Request

    executing = len(session.scalars(select(Request).where(
        Request.status.in_(("in-progress", "decommissioning")))).all())
    running = len(session.scalars(select(CertificationProof).where(
        CertificationProof.status == "running")).all())
    return executing, running


def main() -> int:  # pragma: no cover - the half that spends money
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cap", type=int, default=0,
                        help="maximum machines to spend; required with --apply")
    parser.add_argument("--only", default="",
                        help="comma-separated candidates instead of the built-in list")
    parser.add_argument("--apply", action="store_true",
                        help="actually build; without it, nothing is provisioned")
    parser.add_argument("--offer", action="store_true",
                        help="list what certifies, immediately, in the live catalogue")
    args = parser.parse_args()

    from api import ai_blueprint, registry
    from api.main import SessionLocal, _autobuild_component, append_audit
    from db.models import Blueprint

    candidates = ([c.strip() for c in args.only.split(",") if c.strip()]
                  if args.only else list(CANDIDATES))

    with SessionLocal() as session:
        from sqlalchemy import select

        certified = {b.technology_code for b in session.scalars(
            select(Blueprint).where(Blueprint.deployment_target == "oci"))
            if b.status == "certified"}

    cap = args.cap if args.cap > 0 else len(candidates) * MACHINES_PER_CANDIDATE
    out = plan(candidates, certified, cap)

    print(f"candidates        : {len(candidates)}")
    print(f"already certified : {len([1 for c, w in out['skipped'] if 'certified' in w])}")
    print(f"cap               : {cap} machine(s), "
          f"{MACHINES_PER_CANDIDATE} reserved per candidate")
    print(f"would attempt     : {len(out['attempt'])}\n")

    # WHICH ONES THE LADDER CAN EVEN REACH, asked before anything is built. A
    # registry lookup costs a network round trip; a machine costs twenty
    # minutes, so this ordering is the whole reason a dry run is worth having.
    print("resolving each candidate (no machine is built by this):")
    reachable = []
    for code in out["attempt"]:
        try:
            methods = ai_blueprint.install_methods(
                code, find_image=registry.find, search=lambda c, family="rhel": [])
        except Exception as exc:  # noqa: BLE001 - a lookup is never load-bearing
            print(f"  {code:14} could not be resolved: {type(exc).__name__}")
            continue
        if methods:
            reachable.append(code)
            print(f"  {code:14} {methods}")
        else:
            print(f"  {code:14} no way to install it was found -- skipped")

    for code, why in out["skipped"]:
        print(f"  {code:14} skipped: {why}")

    if not args.apply:
        print(f"\nDRY RUN. {len(reachable)} candidate(s) are reachable and would be "
              f"proved, at up to {MACHINES_PER_CANDIDATE} machines each.")
        print("Re-run with --cap N --apply to build.")
        return 0

    if args.cap <= 0:
        print("\nrefusing: --apply needs an explicit --cap. A batch without a "
              "ceiling is a bill without a ceiling.", file=sys.stderr)
        return 2

    budget = Budget(cap)
    results = []
    with SessionLocal() as session:
        # Proofs already on the record are not this batch's leaks.
        floor = _highest_proof_id(session)
    for code in reachable:
        with SessionLocal() as session:
            executing, running = _live_checks(session)
            leaked = _abandoned_since(session, floor)
        stop = stop_reason(executing=executing, proofs_running=running, leaked=leaked)
        if stop:
            print(f"\nSTOPPED before {code}: {stop}")
            break
        if not budget.affords():
            print(f"\nSTOPPED before {code}: {budget.left} machine(s) left, "
                  f"{MACHINES_PER_CANDIDATE} needed")
            break

        print(f"\n--- {code} ---")
        sys.stdout.flush()
        with SessionLocal() as session:
            before = _proof_count(session, code)
            outcome = _autobuild_component(session, code, "oci")
            spent = _proof_count(session, code) - before
            append_audit(session, "autobuild.finished", actor=ACTOR, detail={
                "component": code, "via": "batch prover", "machines": spent,
                **(outcome or {})})
            session.commit()
        budget.spend(spent)
        _status = (outcome or {}).get("status")
        _listed = (_offer(code, CANDIDATES.get(code, code))
                   if args.offer and _status == "published" else "")
        results.append((code, _status, spent,
                        (outcome or {}).get("detail", "")[:200], _listed))
        print(f"  {_status}  ({spent} machine(s), {budget.left} left of {cap})")
        if _listed:
            print(f"  catalogue: {_listed}")

    print("\n================ RESULT ================")
    for code, status, spent, detail, listed in results:
        print(f"  {status or '?':10} {code:14} {spent} machine(s)"
              + (f"   -> {listed}" if listed else ""))
        if status != "published":
            print(f"             {detail}")
    print(f"\nmachines spent: {budget.spent} of {cap}")
    if args.offer:
        print("Listed in the live catalogue as each one certified. db/seed.py "
              "still needs the same rows committed, or a fresh deployment will "
              "not have them.")
    else:
        print("Nothing was added to the catalogue: proving is not offering.")
    return 0


ACTOR = "mohammed.khader@emaratechg.ae"   # PORTAL_BOOTSTRAP_ADMINS


def _offer(code: str, name: str) -> str:  # pragma: no cover - writes the live catalogue
    """List a technology the moment a machine certified it. Returns what happened.

    ALL FOUR THINGS, OR NONE. A listing needs a technology row, a delivery model,
    a note, and one sizing anchor per size. Written before, the first two alone
    produced entries that reached the request form with no group; the missing
    anchors made MySQL price at 0.00 -- a real machine shown to an approver as
    free. Both were caught by a person reading the form, not by the portal.

    It refuses unless the blueprint says `certified`, which is the same gate a
    request passes. Proving is what earns the listing; this only writes it down.
    """
    from datetime import date

    from sqlalchemy import select

    from api.main import SessionLocal, append_audit
    from db.seed import SIZES
    from db.models import (Blueprint, CertificationProof, SizingAnchor, Technology,
                           TechnologyDelivery)

    profile_path = pathlib.Path("/generated/profiles") / f"{code}.json"
    if not profile_path.is_file():
        return f"not listed: no recipe in the store for {code}"

    with SessionLocal() as session:
        blueprint = session.get(Blueprint, (code, "oci"))
        if blueprint is None or blueprint.status != "certified":
            return (f"not listed: blueprint is "
                    f"{blueprint.status if blueprint else 'absent'}, not certified")

        # THE PROOF'S REFERENCE, NOT THE SENTENCE ABOUT IT. This passed
        # `blueprint.notes`, which is a whole certification sentence, into the
        # slot `catalogue_entry` formats as "Proved by {…}". Every note written
        # by the first batches therefore read
        #
        #   Proved by Certified automatically by proof build PROOF-GITEA-…:
        #   built, verified healthy and destroyed.: the portal built it, …
        #
        # -- doubled, ungrammatical, and long enough that the 300-character
        # column then cut the image name off the end.
        #
        # Read from certification_proof, which is where the reference actually
        # lives, rather than recovered from prose by pattern.
        proof = session.scalar(
            select(CertificationProof)
            .where(CertificationProof.technology_code == code,
                   CertificationProof.status == "passed")
            .order_by(CertificationProof.id.desc()))
        entry = catalogue_entry(code, name, json.loads(profile_path.read_text()),
                                (proof.reference if proof is not None
                                 else "a passing proof"))
        tech = session.scalar(select(Technology).where(Technology.code == code))
        if tech is None:
            tech = Technology(code=code, name=entry["name"],
                              lifecycle_state=entry["lifecycle_state"])
            session.add(tech)
            session.flush()
        delivery = session.get(TechnologyDelivery, code)
        if delivery is None:
            session.add(TechnologyDelivery(technology_code=code,
                                           delivery_model=entry["delivery_model"],
                                           note=entry["note"]))
        made = 0
        for size, (vcpu, memory, storage) in SIZES.items():
            exists = session.scalar(select(SizingAnchor).where(
                SizingAnchor.technology_id == tech.id, SizingAnchor.size == size))
            if exists is None:
                session.add(SizingAnchor(technology_id=tech.id, size=size,
                                         vcpu=vcpu, memory_gb=memory,
                                         storage_gb=storage,
                                         effective_from=date(2026, 1, 1), version=1))
                made += 1
        append_audit(session, "catalogue.offered", actor=ACTOR, detail={
            "component": code, "via": "batch prover --offer",
            "name": entry["name"], "delivery_model": entry["delivery_model"],
            "anchors_created": made})
        session.commit()

    # SAID BACK FROM THE DATABASE, not from what was intended: the price is the
    # number an approver will see, and it is the one this has got wrong before.
    from api.pricing import estimate_cost

    with SessionLocal() as session:
        priced = estimate_cost([{"technology_code": code, "size": "small"}],
                               "oci", session)
    total = priced["totals"]["monthly"]
    if not total or float(total) <= 0:
        return (f"listed, BUT IT PRICES AT {total} -- an approver would be shown "
                f"a real machine as free. Check its sizing anchors.")
    return (f"listed as {entry['name']}, grouped as "
            f"{entry['delivery_model']}, {total} "
            f"{priced.get('currency', '')}/month at small")


def _proof_count(session, code: str) -> int:  # pragma: no cover
    from sqlalchemy import func, select

    from db.models import CertificationProof

    return int(session.scalar(
        select(func.count()).select_from(CertificationProof)
        .where(CertificationProof.technology_code == code)) or 0)


def _abandoned_since(session, floor: int) -> list:  # pragma: no cover - reads the DB
    """Proofs since `floor` whose machine was built and could NOT be torn down.

    THE API HOLDS NO CLOUD CREDENTIAL, by design: the orchestrator does, and the
    signed channel between them has no "list every instance" call. The first
    version of this function tried to enumerate the compartment from here and
    could not -- `oci` is not even installed in this image -- so it hit its own
    exception handler and returned "no leak" every single time it ran. A safety
    check that cannot fail is worse than none, because it is believed. It was
    caught before the first batch spent anything, by asking whether `[]` meant
    "nothing leaked" or "I could not look".

    The portal records the fact itself, and better than a sweep of names could.
    `proof.run_proof` writes `abandoned` when a machine was built and the
    teardown did not take -- "Something may still be running under <reference>"
    in its own words -- and `build()` already stops dead on that same signal.

    SINCE `floor`, not ever: one proof was abandoned historically, and stopping
    on it would mean this tool could never start.
    """
    from sqlalchemy import select

    from db.models import CertificationProof

    return [p.reference for p in session.scalars(
        select(CertificationProof)
        .where(CertificationProof.status == "abandoned")
        .where(CertificationProof.id > floor))]


def _highest_proof_id(session) -> int:  # pragma: no cover - reads the DB
    from sqlalchemy import func, select

    from db.models import CertificationProof

    return int(session.scalar(select(func.max(CertificationProof.id))) or 0)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
