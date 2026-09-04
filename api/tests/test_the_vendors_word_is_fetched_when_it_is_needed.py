"""The gate had the right rule and was handed the wrong evidence.

PROOF-MYSQL-20260903T173811 ran through `_autobuild_component` -- the exact
function a requester's approval calls -- with both certification gates deployed
and verified live on the requester's door. The package rung built the
command-line client, its proof passed, and the blueprint was CERTIFIED with
`services: []` and no port behind it. The fix that was built to stop precisely
this did not fire.

It did not fire because it was told the vendor claimed nothing. resolve.py:

    if searched == "" and image_for(code, find_image=find_image) is not None:

The vendor's image is resolved ONLY when the package search finds nothing.
Oracle Linux carries a package called `mysql` -- the client -- so the search
succeeded, the condition short-circuited, `find_image` was never called,
`consulted["image"]` stayed empty, and the gate received None: "no claim to
contradict". It stood down correctly, on wrong evidence. The declaration it
needed is skipped in exactly the case it exists for -- a package rung that
succeeds at installing the wrong thing.

A survey earlier called registry.find() directly and saw 3306. The real path
never fetches it. Two things that looked like the same evidence were not.

THIS TEST DRIVES `ensure`, the request path, with a package search that
succeeds and an image the ladder never asks for. It went RED on the code that
certified the client, before any fix was written.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from api import autobuild
from api.proof import ProofOutcome
from db.seed import seed
from db.session import Base

SERVICE_VM = {
    "ref": "oci/service-vm", "target": "oci",
    "resource_kind": "oci-service-vm", "builds": ["mysql"], "version": "1.0.0",
}

#: Measured from docker.io/library/mysql on 2026-09-03.
MYSQL_IMAGE = {
    "image": "docker.io/library/mysql", "tag": "latest",
    "digest": "sha256:" + "a" * 64, "ports": [3306, 33060],
}


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for key in ("AUTOBUILD_ENABLED", "AUTOBUILD_MAX_ATTEMPTS", "AI_MODE"):
        monkeypatch.delenv(key, raising=False)
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


def _request_path(db, *, packaged: bool):
    """Drive the real ladder as a requester's approval would.

    `packaged=True` is the defect: the OS repositories carry the name, so the
    package rung wins and the ladder never asks the registry anything.
    """
    published, certified, withdrawn, asked_registry = [], [], [], []

    def run_proof(session, manifest):
        return ProofOutcome(f"PROOF-MYSQL-{len(published)}", "passed",
                            "Built, verified, and torn down")

    def find_image(code):
        asked_registry.append(code)
        return dict(MYSQL_IMAGE)

    result = autobuild.ensure(
        "mysql", db, target="oci",
        shipped=lambda c: dict(SERVICE_VM) if published else None,
        run_proof=run_proof,
        publish=lambda files: published.append(files) or list(files),
        certify=lambda m, ref: certified.append(ref),
        withdraw=lambda files: withdrawn.append(files) or list(files),
        search=lambda c, family: ["mysql"] if packaged else [],
        find_image=find_image,
        observe_ports=lambda ref, c: [])
    return result, certified, withdrawn, asked_registry


def test_the_client_is_not_certified_when_the_package_rung_wins(db):
    """THE DEFECT, on the path that matters. The package search finds `mysql`;
    the recipe installs the client and starts nothing; the proof passes. The
    vendor's image says 3306 -- and nothing asked it."""
    result, certified, withdrawn, asked = _request_path(db, packaged=True)

    assert certified == [], (
        "the command-line client was certified as MySQL on the request path")
    assert withdrawn, "the client recipe was left in the store"


def test_the_vendors_word_is_fetched_before_a_silent_recipe_is_kept(db):
    """WHY it fired: the gate can only judge with the declaration in hand, and
    the ladder does not gather it when a package succeeds. It has to be asked
    for at the moment a recipe serving nothing is about to be certified."""
    _, _, _, asked = _request_path(db, packaged=True)

    assert asked, (
        "the registry was never asked what MySQL's image declares, so the gate "
        "had nothing to judge against")


def test_the_gate_adds_no_registry_call_of_its_own(db):
    """When the package search finds nothing, the ladder resolves the image
    itself -- twice, as it happens: once in install_methods and again when the
    container rung publishes it. That is the baseline, and it is not this
    test's to change. What must hold is that the GATE does not add a third:
    it uses the declaration the ladder already gathered rather than paying for
    it again.

    Pinned as a number deliberately. A gate that fetched unconditionally would
    make this three, and that is exactly the plant this catches.
    """
    _, _, _, asked = _request_path(db, packaged=False)

    assert len(asked) == 2, (
        f"the registry was asked {len(asked)} times on the container path; the "
        f"ladder's own two calls are the baseline, so anything more is the gate "
        f"fetching what it was already handed")


# --- the silence check the fetch is gated on ------------------------------------

def _recipe(**fields):
    import json
    return {"generated/profiles/thing.json": json.dumps({"code": "thing", **fields})}


def test_a_recipe_that_serves_a_port_is_not_silent():
    assert autobuild._runs_nothing(_recipe(ports=[3306])) is False


def test_a_recipe_that_starts_a_unit_is_not_silent():
    """A package recipe that starts a service is running even before its ports
    are narrowed. Treating it as silent would buy a registry call for nothing."""
    assert autobuild._runs_nothing(
        _recipe(ports=[], rhel={"packages": ["x"], "services": ["x"]})) is False


def test_a_recipe_with_neither_is_silent():
    """The client, exactly."""
    assert autobuild._runs_nothing(
        _recipe(ports=[], version_command="x --version",
                rhel={"packages": ["x"], "services": []})) is True


def test_a_draft_with_no_profile_is_not_silent():
    """Not this gate's business; a Terraform module is judged by its build."""
    assert autobuild._runs_nothing({"generated/terraform/main.tf": "x"}) is False
    assert autobuild._runs_nothing({}) is False


# --- the fetch policy itself, on the door, with the declaration controlled --------
#
# The ladder-driven tests above cannot see whether the GATE fetched: on the
# container path the certification point with may_certify=True is never
# reached in that harness, so a gate that fetched unconditionally looked
# identical to one that fetched only when needed. A plant proved it. These call
# the door directly with the declaration under control.

import json as _json


class _Draft:
    def __init__(self, profile):
        self.candidate = profile["code"]
        self.kind = "vm-service"
        self.files = {"generated/profiles/%s.json" % profile["code"]: _json.dumps(profile)}
        self.findings = []
        self.reasoning = ""


def _door(db, profile, *, image_declares, may_certify=True):
    certified, asked = [], []

    def find_image(code):
        asked.append(code)
        return dict(MYSQL_IMAGE)

    result = autobuild._ensure_vm_service(
        profile["code"], db, _Draft(profile), target="oci",
        shipped=lambda c: dict(SERVICE_VM),
        run_proof=lambda s, m: ProofOutcome("PROOF-X-1", "passed", "Built"),
        publish=lambda files: list(files),
        certify=lambda m, ref: certified.append(ref),
        withdraw=lambda files: list(files),
        may_certify=may_certify, image_declares=image_declares,
        find_image=find_image)
    return result, certified, asked


SERVED = {"code": "mysql", "builds_on": "oci/service-vm", "ports": [3306],
          "rhel": {"packages": ["mysql-server"], "services": ["mysqld"]}}
SILENT = {"code": "mysql", "builds_on": "oci/service-vm", "ports": [],
          "version_command": "mysql --version 2>&1",
          "rhel": {"packages": ["mysql"], "services": []}}


def test_a_recipe_that_serves_is_not_worth_a_registry_call(db):
    """The declaration is in hand and the recipe serves: nothing to fetch."""
    result, certified, asked = _door(db, SERVED, image_declares=[3306, 33060])

    assert certified, result.detail
    assert asked == [], "the gate paid for a declaration it already held"


def test_a_silent_recipe_with_no_declaration_is_asked_about_exactly_once(db):
    """The defect's shape: nobody fetched, the recipe runs nothing, and the
    answer changes the verdict. One call, then refuse."""
    result, certified, asked = _door(db, SILENT, image_declares=None)

    assert asked == ["mysql"], asked
    assert certified == [], "the client was certified"
    assert result.status == "failed"


def test_a_declaration_the_ladder_gathered_is_not_fetched_again(db):
    """[] means the ladder asked and the vendor declares nothing -- a runtime.
    That answer stands; asking again would both cost a call and, for a vendor
    whose image happens to declare ports, contradict the ladder."""
    result, certified, asked = _door(db, SILENT, image_declares=[])

    assert asked == [], "the gate re-fetched a declaration the ladder already had"
    # And with the vendor declaring nothing, a silent recipe that can be asked
    # its version is the dotnet8 shape -- a runtime -- and is certified. The
    # first draft of this test asserted "failed" here, which was the test being
    # wrong about a runtime, not the code being wrong about the client.
    assert certified, result.detail
    assert result.status == "published"


# --- a refusal is a verdict: it makes the ladder climb, and it is remembered ------

def test_a_refusal_is_a_verdict_the_ladder_can_climb_on(db):
    """The ladder climbs only on a machine's verdict -- deliberately, because
    cost caps and failed teardowns also come back "failed" and it was buying
    the same refusal at every rung. A gate refusal IS a verdict: a real machine
    built this recipe and it ran nothing. Without the flag the ladder stopped
    at the refused package and the discovered rung -- the package plus the
    unit the machine actually started -- was never reached."""
    result, certified, _ = _door(db, SILENT, image_declares=None)

    assert certified == []
    assert result.machine_refuted is True, (
        "the refusal is not marked a verdict, so the ladder treats it like a "
        "cost cap and stops instead of trying the next method")


def test_a_refusal_is_remembered_so_the_next_request_does_not_pay_for_it(db):
    """`recipe_memory` exists so a recipe a machine has already refuted is not
    rebuilt for the next requester. A gate refusal is exactly that kind of
    fact, and it was not being recorded."""
    from sqlalchemy import select
    from db.models import RecipeRefutation

    _door(db, SILENT, image_declares=None)

    rows = db.scalars(select(RecipeRefutation).where(
        RecipeRefutation.technology_code == "mysql",
        RecipeRefutation.deployment_target == "oci")).all()

    assert rows, "the refusal was not remembered; the next request would rebuy it"
    assert rows[-1].proof_reference == "PROOF-X-1"
    assert "runs nothing" in rows[-1].detail
