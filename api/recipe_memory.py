"""What a machine already disproved, so no machine has to disprove it twice.

REQ-2026-0183 asked for "Backup & Recovery". The agent guessed `dnf install
backup`, booted a real VM, and the machine answered "backup NOT INSTALLED". That
was the system working — a guess refuted by evidence, the profile withdrawn,
nothing certified, nothing left running. It also cost five minutes and a machine
to learn that a capability name is not an RPM, and the next request for the same
thing would have spent another machine on the identical guess.

The evidence was already being recorded: every failed proof sits in
certification_proof with the machine's exact words. Nothing read it. That is the
shape of most defects found in this project — an honest signal that the thing
making the decision cannot see.

THREE JUDGEMENTS THIS MODULE MAKES, EACH DELIBERATE.

*What* was disproved is a RECIPE, not a component. keycloak was refuted as a
package guess and then succeeded as an archive install the next day; a
component-level memory would have prevented that. Changing the recipe must be
allowed to change the answer.

*Which* failures count. Only a machine's verdict refutes a recipe. A proof that
was refused before building (over the cost cap, sandbox unset), or abandoned
because teardown failed, says nothing about whether the recipe works — those are
our problems, not the recipe's.

*How long* it holds. A package absent from Oracle Linux today may be packaged
tomorrow, so a refutation expires on the same clock as a certification. Evidence
about the world has a shelf life exactly as evidence about a recipe does.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from api import proof
from db.models import RecipeRefutation

# Only these fields decide whether two recipes are the same attempt. A reworded
# `_note` or a changed description is not a different way of installing
# something, and treating it as one would let the agent burn a machine per
# rephrasing.
_INSTALL_FIELDS = ("packages", "services")
_ARCHIVE_FIELDS = ("url", "sha256", "dest")


def fingerprint(recipe: dict | None) -> str:
    """A stable digest of how a recipe installs something.

    Built from the install-relevant fields ONLY, in sorted order, so the same
    attempt always hashes the same and a cosmetic edit never looks like a new
    idea worth another machine.

    A shipped manifest (no per-family blocks) fingerprints on its ref and
    version, which is what changes when somebody fixes the module.
    """
    if not isinstance(recipe, dict):
        return ""
    material: dict = {}

    for family in ("rhel", "debian", "suse"):
        block = recipe.get(family)
        if isinstance(block, dict):
            material[family] = {
                field: sorted(str(v) for v in (block.get(field) or []))
                for field in _INSTALL_FIELDS
            }

    archive = recipe.get("archive")
    if isinstance(archive, dict):
        material["archive"] = {f: str(archive.get(f) or "") for f in _ARCHIVE_FIELDS}
        unit = archive.get("unit")
        if isinstance(unit, dict):
            material["archive"]["exec_start"] = str(unit.get("exec_start") or "")

    if not material:
        # A shipped manifest rather than a profile: what identifies the attempt
        # is which module was built and at what version.
        material = {"ref": str(recipe.get("ref") or ""),
                    "version": str(recipe.get("version") or "")}

    return hashlib.sha256(
        json.dumps(material, sort_keys=True).encode()).hexdigest()


def refutes(outcome_status: str, detail: str) -> bool:
    """Whether this proof outcome says anything about the RECIPE.

    `refused` means a gate stopped it before building — the cost cap, a missing
    sandbox tier, a contract skew. `abandoned` means it built and could not be
    torn down, which is our problem and a loud one, but not the recipe's.

    Only a machine that was built and asked can refute a recipe. That is the same
    rule configure.REFUTED states for the hand-curated record it keeps: "a guess
    belongs in a comment, not here."
    """
    if outcome_status != "failed":
        return False
    # A failure before the machine ever reported is about the handoff, not the
    # recipe: refusing a recipe on the strength of a 403 would be exactly the
    # version-skew confusion of 2026-08-21 made permanent.
    return "Provision refused" not in (detail or "")


def remember(session: Session, code: str, target: str, recipe: dict | None,
             proof_reference: str, detail: str) -> RecipeRefutation | None:
    """Record that a machine disproved this recipe. Caller commits."""
    digest = fingerprint(recipe)
    if not digest:
        return None
    row = RecipeRefutation(
        technology_code=(code or "").strip(), deployment_target=(target or "").strip(),
        fingerprint=digest, proof_reference=proof_reference or "",
        detail=(detail or "")[:1000])
    session.add(row)
    return row


def previously_refuted(session: Session, code: str, target: str,
                       recipe: dict | None, now: datetime | None = None) -> str:
    """The machine's own words if this exact recipe was already disproved, else "".

    Returns a sentence fit to show a requester: what was tried, which machine
    said no, when, and what it said. A refusal that cannot show its evidence is
    indistinguishable from an opinion.
    """
    digest = fingerprint(recipe)
    if not digest:
        return ""
    row = session.scalar(
        select(RecipeRefutation)
        .where(RecipeRefutation.technology_code == (code or "").strip(),
               RecipeRefutation.deployment_target == (target or "").strip(),
               RecipeRefutation.fingerprint == digest)
        .order_by(RecipeRefutation.id.desc()))
    if row is None:
        return ""

    now = now or datetime.now(timezone.utc)
    when = row.refuted_at
    if when is not None and when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    if when is not None and (now - when) > timedelta(days=proof.validity_days()):
        # Stale. A package absent yesterday may be packaged today, and holding a
        # refutation for ever would freeze the catalogue against a world that
        # moves.
        return ""

    stamp = when.strftime("%Y-%m-%d") if when else "earlier"
    return (f"This exact recipe was already disproved on {stamp}: a machine was "
            f"built for {row.proof_reference or 'an earlier proof'} and reported "
            f"— {row.detail[:400]} — so building another one would spend a "
            f"machine to be told the same thing. Change the recipe and it will "
            f"be tried again.")
