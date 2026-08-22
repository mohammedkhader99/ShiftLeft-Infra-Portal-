"""No machine should have to disprove the same recipe twice (REQ-2026-0183).

The request asked for "Backup & Recovery". The agent guessed `dnf install
backup`, booted a real VM, and the machine answered "backup NOT INSTALLED". That
was the system working exactly as designed — a guess refuted by evidence, the
profile withdrawn, nothing certified, nothing left running.

It also cost five minutes and a machine to learn that a capability name is not an
RPM, and NOTHING remembered the answer. Six catalogue entries are in that
category (backup, logging, monitoring, api-gateway, service-mesh, k8s), and each
one would have bought the identical answer again on every future request.

The evidence was already there: every failed proof sits in certification_proof
with the machine's exact words. Nothing read it — the same shape as most defects
found in this project, an honest signal the deciding code cannot see.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from api import autobuild, recipe_memory
from api.ai_blueprint import Draft
from api.proof import ProofOutcome
from db.models import RecipeRefutation
from db.seed import seed
from db.session import Base

PACKAGE_GUESS = {"code": "backup", "builds_on": "oci/service-vm",
                 "rhel": {"packages": ["backup"], "services": ["backup"]}}
THE_MACHINE_SAID = ("Built, but did not verify healthy: oci-service-vm: backup "
                    "NOT INSTALLED; backup is inactive")


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("AUTOBUILD_ENABLED", "true")
    monkeypatch.setenv("AI_MODE", "mock")


@pytest.fixture()
def db():
    engine = create_engine("sqlite+pysqlite:///:memory:",
                           connect_args={"check_same_thread": False},
                           poolclass=StaticPool, future=True)
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine, expire_on_commit=False)()
    seed(s)
    s.commit()
    yield s
    s.close()


# --- what counts as the same attempt ------------------------------------------

def test_the_same_recipe_fingerprints_the_same():
    assert recipe_memory.fingerprint(PACKAGE_GUESS) == recipe_memory.fingerprint(
        dict(PACKAGE_GUESS))


def test_a_cosmetic_change_is_not_a_new_idea():
    """A reworded note is not a different way of installing something, and
    treating it as one would burn a machine per rephrasing."""
    reworded = {**PACKAGE_GUESS, "_note": "DRAFT — reworded", "ports": [80]}
    assert recipe_memory.fingerprint(reworded) == recipe_memory.fingerprint(PACKAGE_GUESS)


def test_a_different_package_is_a_different_recipe():
    changed = {**PACKAGE_GUESS, "rhel": {"packages": ["bacula"], "services": ["bacula"]}}
    assert recipe_memory.fingerprint(changed) != recipe_memory.fingerprint(PACKAGE_GUESS)


def test_an_archive_is_not_the_same_attempt_as_a_package_guess():
    """THE case that matters most. keycloak was refuted as `dnf install keycloak`
    on REQ-2026-0177 and then SUCCEEDED as an archive install on 0178. A memory
    keyed on the component rather than the recipe would have prevented that."""
    guess = {"code": "keycloak", "rhel": {"packages": ["keycloak"], "services": ["keycloak"]}}
    archive = {"code": "keycloak",
               "rhel": {"packages": ["java-21-openjdk-headless"], "services": ["keycloak"]},
               "archive": {"url": "https://x/keycloak-26.7.2.tar.gz", "sha256": "a" * 64,
                           "dest": "/opt/keycloak",
                           "unit": {"exec_start": "/opt/keycloak/bin/kc.sh start-dev"}}}
    assert recipe_memory.fingerprint(guess) != recipe_memory.fingerprint(archive)


def test_a_shipped_manifest_fingerprints_on_its_version():
    """It has no per-family blocks. What changes when somebody fixes the module
    is its version, so that is what must change the answer."""
    a = {"ref": "oci/apache-httpd", "version": "1.0.0"}
    b = {"ref": "oci/apache-httpd", "version": "1.0.1"}
    assert recipe_memory.fingerprint(a) != recipe_memory.fingerprint(b)


# --- which failures actually refute a recipe ----------------------------------

def test_a_machine_that_reported_a_failure_refutes_the_recipe():
    assert recipe_memory.refutes("failed", THE_MACHINE_SAID) is True


@pytest.mark.parametrize("status,detail", [
    ("refused", "Proof builds are disabled."),
    ("refused", "Plan prices at 1240.00 monthly, above the 250.00 cap."),
    ("abandoned", "Built and verified=True, but teardown failed."),
    ("passed", "Built, verified, and torn down."),
])
def test_our_own_problems_do_not_refute_a_recipe(status, detail):
    """A cost cap, a disabled runner, a failed teardown — none of these say
    anything about whether the recipe works. Only a machine that was built and
    asked can refute one."""
    assert recipe_memory.refutes(status, detail) is False


def test_a_handoff_failure_does_not_refute_a_recipe():
    """A 403 from the orchestrator is a version skew or a bad reference, not a
    broken recipe. Refusing the recipe on that evidence would make the skew of
    2026-08-21 permanent."""
    assert recipe_memory.refutes(
        "failed", "Provision refused: 403: 'PROOF-X' is not a proof reference.") is False


# --- remembering and recalling ------------------------------------------------

def test_a_refuted_recipe_is_recalled_with_the_machines_words(db):
    recipe_memory.remember(db, "backup", "oci", PACKAGE_GUESS,
                           "PROOF-BACKUP-20260822T152106", THE_MACHINE_SAID)
    db.commit()

    said = recipe_memory.previously_refuted(db, "backup", "oci", PACKAGE_GUESS)
    assert "backup NOT INSTALLED" in said, "the refusal cannot show its evidence"
    assert "PROOF-BACKUP-20260822T152106" in said, "it does not say which machine"
    assert "Change the recipe" in said, "it does not say what would change the answer"


def test_a_different_recipe_is_not_blocked(db):
    recipe_memory.remember(db, "keycloak", "oci",
                           {"code": "keycloak", "rhel": {"packages": ["keycloak"]}},
                           "PROOF-1", "keycloak NOT INSTALLED")
    db.commit()

    archive = {"code": "keycloak",
               "rhel": {"packages": ["java-21-openjdk-headless"], "services": ["keycloak"]},
               "archive": {"url": "https://x/k.tar.gz", "sha256": "a" * 64,
                           "dest": "/opt/keycloak",
                           "unit": {"exec_start": "/opt/keycloak/bin/kc.sh start-dev"}}}
    assert recipe_memory.previously_refuted(db, "keycloak", "oci", archive) == "", (
        "the archive install that actually worked would have been blocked")


def test_a_stale_refutation_is_retried(db):
    """A package absent from Oracle Linux today may be packaged tomorrow.
    Evidence about the world has a shelf life, exactly as a certification does —
    and they expire on the same clock."""
    row = recipe_memory.remember(db, "backup", "oci", PACKAGE_GUESS, "PROOF-OLD",
                                 THE_MACHINE_SAID)
    row.refuted_at = datetime.now(timezone.utc) - timedelta(days=400)
    db.commit()

    assert recipe_memory.previously_refuted(db, "backup", "oci", PACKAGE_GUESS) == ""


def test_a_recent_refutation_still_holds(db):
    row = recipe_memory.remember(db, "backup", "oci", PACKAGE_GUESS, "PROOF-NEW",
                                 THE_MACHINE_SAID)
    row.refuted_at = datetime.now(timezone.utc) - timedelta(days=2)
    db.commit()

    assert recipe_memory.previously_refuted(db, "backup", "oci", PACKAGE_GUESS)


def test_another_target_is_a_separate_question(db):
    recipe_memory.remember(db, "backup", "oci", PACKAGE_GUESS, "P", THE_MACHINE_SAID)
    db.commit()
    assert recipe_memory.previously_refuted(db, "backup", "azure", PACKAGE_GUESS) == ""


# --- the loop: it must not spend a second machine -----------------------------

def _proposal(code, recipe):
    import json
    return Draft(candidate=code, kind="vm-service",
                 files={f"generated/profiles/{code}.json": json.dumps(recipe)})


def test_a_second_request_for_a_refuted_recipe_builds_nothing(db):
    """THE saving. Five minutes and a machine, every time, for an answer already
    in the database."""
    recipe_memory.remember(db, "backup", "oci", PACKAGE_GUESS,
                           "PROOF-BACKUP-20260822T152106", THE_MACHINE_SAID)
    db.commit()
    proved, published = [], []

    result = autobuild._ensure_vm_service(
        "backup", db, _proposal("backup", PACKAGE_GUESS), target="oci",
        shipped=lambda c: {"ref": "oci/service-vm", "target": "oci",
                           "resource_kind": "oci-service-vm"},
        run_proof=lambda s, m: proved.append(1),
        publish=published.append, certify=lambda m, r: None,
        withdraw=lambda f: None)

    assert result.status == "refused"
    assert proved == [], "it booted a machine to be told what it already knew"
    assert published == [], "it published a recipe already disproved"
    assert "backup NOT INSTALLED" in result.detail


def test_the_first_failure_is_recorded_for_the_next_time(db):
    """The loop has to WRITE the memory, not only read it."""
    autobuild._ensure_vm_service(
        "backup", db, _proposal("backup", PACKAGE_GUESS), target="oci",
        shipped=lambda c: {"ref": "oci/service-vm", "target": "oci",
                           "resource_kind": "oci-service-vm"},
        run_proof=lambda s, m: ProofOutcome("PROOF-X", "failed", THE_MACHINE_SAID),
        publish=lambda f: None, certify=lambda m, r: None, withdraw=lambda f: None)

    row = db.scalar(select(RecipeRefutation).where(
        RecipeRefutation.technology_code == "backup"))
    assert row is not None, "the machine's answer was thrown away"
    assert row.proof_reference == "PROOF-X"
    assert "NOT INSTALLED" in row.detail


def test_a_cost_refusal_is_not_remembered_as_a_refutation(db):
    """Being over the cost cap says nothing about whether the recipe works, and
    remembering it as a refutation would take a working component off the menu
    for thirty days."""
    autobuild._ensure_vm_service(
        "backup", db, _proposal("backup", PACKAGE_GUESS), target="oci",
        shipped=lambda c: {"ref": "oci/service-vm", "target": "oci",
                           "resource_kind": "oci-service-vm"},
        run_proof=lambda s, m: ProofOutcome(
            "PROOF-X", "refused", "Plan prices at 1240.00, above the 250.00 cap."),
        publish=lambda f: None, certify=lambda m, r: None, withdraw=lambda f: None)

    assert db.scalar(select(RecipeRefutation)) is None
