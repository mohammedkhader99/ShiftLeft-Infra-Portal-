"""The drafter proposes a recipe; the linter decides whether it is worth reading (C4).

ARCHITECTURE.md §7 (amended 2026-08-21) lets an agent draft a Terraform module
and blueprint. It applies nothing and triggers nothing. What makes that safe is
not the prompt — four of the eight defects found on 2026-08-17 were written by an
AI with the provider documentation open — so every draft, model-written or not,
goes through a deterministic linter carrying those exact defects.

These tests are mostly that linter. If it stops catching what this project has
already paid to learn, the drafter is a liability.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from api import ai_blueprint as ab
from api.main import app, get_session
from db.models import Technology
from db.seed import seed
from db.session import Base


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


@pytest.fixture()
def client(db):
    def override():
        yield db
    app.dependency_overrides[get_session] = override
    yield TestClient(app)
    app.dependency_overrides.clear()


# --- The linter: every defect this project has already paid for --------------

@pytest.mark.parametrize("code,rule", [
    ('assign_public_ip = true', "public-ip"),
    ('is_regionally_durable = true', "regional-durability"),
    ('image_id = sources[length(sources) - 1].image_id', "list-position"),
    ('kubernetes_version = "v1.29.1"', "pinned-version"),
    ('db_version = "14"', "pinned-version"),
    ('iops = 75000', "expensive-default"),
    ('password = "hunter2please"', "inline-secret"),
    ('shape = "VM.Standard.E4.Flex"', "hard-coded-shape"),
])
def test_the_linter_catches_what_already_cost_us(code, rule):
    """Each of these reached a real request. REQ-2026-0148 through 0159."""
    found = ab.review_draft({"main.tf": code})
    assert rule in [f.rule for f in found], f"{rule} slipped through: {code}"


def test_a_finding_says_what_it_cost_not_just_what_it_is():
    """A reviewer needs to know whether a finding is pedantry or the reason four
    builds failed."""
    found = ab.review_draft({"main.tf": "assign_public_ip = true"})
    detail = next(f.detail for f in found if f.rule == "public-ip")
    assert "refuses the VNIC" in detail
    assert "nodes register timeout" in detail


def test_comments_are_not_code():
    """Every module in this repo documents the defect it used to have. Flagging
    those comments would make the linter cry wolf on exactly the files that were
    fixed most carefully."""
    documented = (
        "# This used to say assign_public_ip = true, and OCI refused the VNIC.\n"
        "# is_regionally_durable = true was hard-coded here.\n"
        "  assign_public_ip = false\n"
    )
    assert ab.review_draft({"bastion.tf": documented}) == []


def test_an_incomplete_manifest_is_a_blocker():
    """A manifest the orchestrator cannot discover means the portal offers a
    technology nothing can build — the oci-adb situation, which is how a
    'database' becomes an object-storage bucket."""
    found = ab.review_draft({"x.yaml": "ref: oci/x\ntarget: oci\n"})
    rules = {f.rule for f in found}
    assert "manifest-incomplete" in rules
    assert all(f.severity == "blocker" for f in found)


def test_a_clean_module_produces_no_findings():
    """The linter has to be quiet when there is nothing to say, or it gets
    ignored."""
    clean = (
        'resource "oci_core_instance" "env" {\n'
        '  shape = local.resolved_shape\n'
        '  create_vnic_details {\n'
        '    assign_public_ip = false\n'
        '  }\n'
        '}\n'
    )
    assert ab.review_draft({"main.tf": clean}) == []


# --- Classifying, and not reaching for a model unnecessarily -----------------

def test_a_version_bump_is_worked_out_not_generated(db):
    """postgres18 needs NO Terraform: the module reads the version from the
    catalogue name and checks it against the cloud. Calling a model to increment
    a number would add cost, latency and a failure mode for nothing."""
    proposal = ab.draft("postgres18", db)

    assert proposal.kind == "version-bump"
    assert proposal.source == "deterministic"
    assert proposal.files == {}, "a version bump should need no files at all"
    assert proposal.catalogue_rows[0]["code"] == "postgres18"
    assert proposal.catalogue_rows[0]["name"] == "PostgreSQL 18"
    # It must inherit the sibling's wiring, not invent it.
    assert proposal.catalogue_rows[0]["resource_kind"] == "oci-postgres"
    assert proposal.catalogue_rows[0]["lifecycle_state"] == "draft"


def test_a_version_bump_explains_why_there_is_no_terraform(db):
    proposal = ab.draft("postgres18", db)
    assert "No Terraform is needed" in proposal.reasoning
    assert "proof build" in proposal.reasoning


def test_an_unknown_family_is_a_new_service(db):
    kind, sibling = ab.classify("cassandra5", db)
    assert kind == "new-service" and sibling is None


def test_a_new_service_scaffold_refuses_to_guess(db, monkeypatch):
    """A confident-looking wrong module is more dangerous than an obviously
    unfinished one. This project lost four builds to values that looked right."""
    monkeypatch.setenv("AI_MODE", "mock")
    proposal = ab.draft("cassandra5", db)

    assert proposal.kind == "new-service"
    tf = next(v for k, v in proposal.files.items() if k.endswith(".tf"))
    assert "TODO" in tf, "the scaffold invented details instead of leaving them open"
    assert "DRAFT" in tf
    # And it must not have quietly produced any of the known defects.
    assert [f for f in proposal.findings if f.severity == "blocker"] == []


def test_the_scaffolds_manifest_is_complete_enough_to_be_discovered(db, monkeypatch):
    monkeypatch.setenv("AI_MODE", "mock")
    proposal = ab.draft("cassandra5", db)
    manifest = next(v for k, v in proposal.files.items() if k.endswith(".yaml"))
    for key in ("ref:", "target:", "resource_kind:", "builds:"):
        assert key in manifest


# --- Diagnosing a failed proof ------------------------------------------------

@pytest.mark.parametrize("detail,rule", [
    ("Public IP addresses are prohibited in this subnet {ocid...}", "public-ip"),
    ("2 nodes(s) register timeout. First, confirm that network prerequisites",
     "worker-image"),
    ("isRegionallyDurable is set to true but the region doesn't support it",
     "regional-durability"),
    ("400-InvalidParameter, Invalid kubernetes version v1.29.1", "pinned-version"),
])
def test_a_failure_is_matched_to_what_caused_it(detail, rule):
    """Each of these is a real message from this week. The point is to save the
    hours it took to work out what they meant the first time."""
    assert rule in [f.rule for f in ab.diagnose(detail, {})]


def test_an_unrecognised_failure_says_so_rather_than_inventing_a_cause():
    """The worst outcome is a confident wrong diagnosis: three builds were spent
    checking network prerequisites because the error said to."""
    found = ab.diagnose("something nobody has seen before", {})
    assert [f.rule for f in found] == ["undiagnosed"]
    assert "read it before changing anything" in found[0].detail


# --- Through the endpoint -----------------------------------------------------

def test_the_endpoint_applies_nothing(client):
    body = client.post("/api/catalogue/draft",
                       json={"candidate": "postgres18"}).json()
    assert body["applied"] is False
    assert "Nothing has been written to the repository" in body["note"]
    assert body["kind"] == "version-bump"


def test_a_blocked_draft_is_still_returned_with_its_findings(client, monkeypatch):
    """A blocked draft is not rejected — the findings are the most useful part of
    it, and hiding them would leave a reviewer with nothing to act on."""
    monkeypatch.setattr(ab, "_scaffold_draft", lambda c, t="oci": ab.Draft(
        candidate=c, kind="new-service",
        files={"main.tf": "assign_public_ip = true"}))
    monkeypatch.setenv("AI_MODE", "mock")
    body = client.post("/api/catalogue/draft",
                       json={"candidate": "cassandra5"}).json()

    assert body["blocked"] is True
    assert body["files"], "the draft was withheld instead of shown"
    assert "public-ip" in [f["rule"] for f in body["findings"]]


def test_drafting_is_audited(client, db):
    from sqlalchemy import select as _select

    from db.models import AuditLog

    client.post("/api/catalogue/draft", json={"candidate": "postgres18"})
    events = [a.event for a in db.scalars(_select(AuditLog)).all()]
    assert "blueprint.drafted" in events
