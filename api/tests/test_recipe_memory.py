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
    treating it as one would burn a machine per rephrasing.

    NARROWED on 2026-08-23. This used to count a changed `ports` list as cosmetic
    too. It is not: every port becomes an http_ check, an `ss` check and a
    firewall check on the machine, and is opened in the firewall. That was
    wishful grouping, and REQ-2026-0185 showed what it costs.
    """
    reworded = {**PACKAGE_GUESS, "_note": "DRAFT — reworded",
                "description": "Backup and recovery tooling"}
    assert recipe_memory.fingerprint(reworded) == recipe_memory.fingerprint(PACKAGE_GUESS)


# --- what the machine is ASKED is part of the recipe (REQ-2026-0185) ----------
#
# Vault installed perfectly from HashiCorp's repository — rpm present, unit
# active, listening on 8200, firewall open — and was refuted anyway, because the
# recipe demanded version "1" of software now on 2.x and a 200 from an API root
# that answers 400. Every install field was right. If the memory cannot tell
# that recipe from its correction, it skips the rung that works for a month and
# the fix, though real, changes nothing.

WORKS = {"code": "vault", "builds_on": "oci/service-vm", "ports": [8200],
         "expects": "1", "version_command": "vault version 2>&1",
         "repo": {"url": "https://rpm.releases.hashicorp.com/RHEL/hashicorp.repo",
                  "gpg_key": "https://rpm.releases.hashicorp.com/gpg"},
         "rhel": {"packages": ["vault"], "services": ["vault"]}}


def test_correcting_a_wrong_version_expectation_is_a_new_attempt():
    """THE REQ-2026-0185 defect. Dropping an `expects` that was recalled rather
    than measured leaves every install field untouched."""
    corrected = {**WORKS, "expects": ""}
    assert recipe_memory.fingerprint(corrected) != recipe_memory.fingerprint(WORKS), (
        "a corrected expectation hashes as the recipe a machine refuted, so it "
        "would be skipped as already-disproved and never re-tried")


def test_asking_the_machine_a_different_question_is_a_new_attempt():
    changed = {**WORKS, "version_command": "vault status 2>&1"}
    assert recipe_memory.fingerprint(changed) != recipe_memory.fingerprint(WORKS)


def test_a_different_port_is_a_new_attempt():
    """A port is opened in the firewall and probed three ways. Changing it
    changes both what is built and what must be proved."""
    changed = {**WORKS, "ports": [8201]}
    assert recipe_memory.fingerprint(changed) != recipe_memory.fingerprint(WORKS)


def test_the_install_fields_alone_no_longer_decide():
    """Stated as its own fact, so the next person to 'simplify' the fingerprint
    back to packages-and-services has to delete a test that says why not."""
    same_install = {**WORKS, "expects": "", "ports": [8201],
                    "version_command": "vault status 2>&1"}
    assert same_install["rhel"] == WORKS["rhel"]
    assert same_install["repo"] == WORKS["repo"]
    assert recipe_memory.fingerprint(same_install) != recipe_memory.fingerprint(WORKS)


def test_pinning_a_module_stream_is_a_new_attempt():
    """The correction that fixed the original Redis defect was `redis:7` and
    nothing else — same package, same service. If that hashes as the recipe the
    machine refuted, the one fix this project is named after gets skipped."""
    base = {"code": "redis7", "ports": [6379],
            "version_command": "redis-server --version",
            "rhel": {"packages": ["redis"], "services": ["redis"]}}
    pinned = {**base, "rhel": {**base["rhel"], "module": "redis:7"}}
    assert recipe_memory.fingerprint(pinned) != recipe_memory.fingerprint(base)


def test_changing_what_the_unit_runs_with_is_a_new_attempt():
    """A service that failed for want of an environment variable is corrected by
    adding one and changing nothing else."""
    base = {"code": "thing", "rhel": {"packages": [], "services": ["thing"]},
            "archive": {"url": "https://x/t.tar.gz", "sha256": "a" * 64,
                        "dest": "/opt/thing", "user": "thing",
                        "unit": {"exec_start": "/opt/thing/run"}}}
    with_env = {**base, "archive": {**base["archive"],
                                    "unit": {"exec_start": "/opt/thing/run",
                                             "environment": {"JAVA_HOME": "/usr"}}}}
    assert recipe_memory.fingerprint(with_env) != recipe_memory.fingerprint(base)


def test_changing_the_user_root_hands_the_software_to_is_a_new_attempt():
    base = {"code": "thing", "rhel": {"packages": [], "services": ["thing"]},
            "archive": {"url": "https://x/t.tar.gz", "sha256": "a" * 64,
                        "dest": "/opt/thing", "user": "thing",
                        "unit": {"exec_start": "/opt/thing/run"}}}
    other = {**base, "archive": {**base["archive"], "user": "keycloak"}}
    assert recipe_memory.fingerprint(other) != recipe_memory.fingerprint(base)


def test_a_shipped_manifest_is_still_identified_by_its_module_and_version():
    """The `asks` block must not be bolted onto a manifest that has no install
    fields — that branch is what tells one shipped module from another, and
    making it unreachable would collapse every manifest onto a single digest."""
    a = {"ref": "oci/service-vm", "target": "oci", "version": "1.0.0"}
    b = {"ref": "oci/service-vm", "target": "oci", "version": "1.1.0"}
    c = {"ref": "oci/bucket", "target": "oci", "version": "1.0.0"}
    assert recipe_memory.fingerprint(a) != recipe_memory.fingerprint(b)
    assert recipe_memory.fingerprint(a) != recipe_memory.fingerprint(c)


def test_a_refutation_written_under_the_old_scheme_no_longer_matches(db):
    """Changing what a fingerprint covers changes every digest. Rows written
    before the change describe recipes under a different definition, so they go
    INERT rather than matching the wrong thing — the safe direction: at worst a
    machine re-proves something, never a working recipe silently skipped."""
    db.add(RecipeRefutation(
        technology_code="vault", deployment_target="oci",
        fingerprint="3147c800559aa195" + "0" * 48,   # a digest from the old scheme
        proof_reference="PROOF-OLD", detail="vault is the wrong version"))
    db.commit()

    assert recipe_memory.previously_refuted(db, "vault", "oci", WORKS) == ""


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
