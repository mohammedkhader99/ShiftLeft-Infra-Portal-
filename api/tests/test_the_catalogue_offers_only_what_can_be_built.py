"""A catalogue entry nothing can build is a promise nobody can keep.

Withdrawn 2026-08-31, at the platform owner's instruction: any OCI component
that needs a Terraform module nobody has written comes off the catalogue.

TWO QUALIFY, ON EVIDENCE.

    oci-functions   The agent tried three times for REQ-2026-0246 and gave up:
                    "the module declares a placeholder resource type rather than
                    a real one, so it applies cleanly and creates nothing." An
                    earlier attempt PASSED a proof on precisely that basis, before
                    the module review existed to catch it.
    oci-adb         Oracle Autonomous Database. A managed service with no module
                    in the repository and no attempt ever made. The agent writes
                    modules for MACHINES; a managed service is a product Oracle
                    sells, not something to compose by guessing at provider
                    resources.

AND TWO THAT LOOK IDENTICAL FROM THE OUTSIDE MUST STAY. Both lack a certified
blueprint, which is the obvious rule to reach for and the wrong one:

    oci-oke         `oci/oke` is in the repository and reviewed. Its proof failed
                    on a NAME -- the node pool name overshot OCI's 32-character
                    cap -- which is fixed. Nine request components use it.
    postgres16      `oci/postgres` is in the repository and reviewed. It needs its
                    two settings and a proof, not a module. SEVENTEEN request
                    components use it -- the most-used uncertified entry there is.

Withdrawing on "no certified blueprint" would have taken both, and taken the
most-requested database in the catalogue with them.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from db.models import Technology, TechnologyDelivery
from db.seed import seed
from db.session import Base

#: Withdrawn because the portal has no Terraform for them and cannot write it.
NEEDS_A_HUMAN_MODULE = ("oci-adb", "oci-functions")

#: Uncertified, but a reviewed module for them exists in the repository.
HAS_A_REVIEWED_MODULE = ("oci-oke", "postgres16")


@pytest.fixture()
def db():
    engine = create_engine("sqlite+pysqlite:///:memory:",
                           connect_args={"check_same_thread": False},
                           poolclass=StaticPool, future=True)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    seed(session)
    session.commit()
    yield session
    session.close()


@pytest.mark.parametrize("code", NEEDS_A_HUMAN_MODULE)
def test_a_component_nothing_can_build_is_withdrawn(db, code):
    tech = db.scalar(select(Technology).where(Technology.code == code))

    assert tech is not None, f"{code} vanished from the catalogue entirely"
    assert tech.lifecycle_state == "eol", (
        f"{code} is offered again, and the portal still has no Terraform for it")


@pytest.mark.parametrize("code", HAS_A_REVIEWED_MODULE)
def test_a_component_with_a_reviewed_module_is_kept(db, code):
    """The trap. Both of these lack a CERTIFIED blueprint, so the obvious rule
    would withdraw them -- including the most-requested database here."""
    tech = db.scalar(select(Technology).where(Technology.code == code))

    assert tech.lifecycle_state != "eol", (
        f"{code} was withdrawn, but a reviewed module for it exists; it needs "
        f"proving or configuring, not removing")


@pytest.mark.parametrize("code", HAS_A_REVIEWED_MODULE)
def test_the_module_those_two_rely_on_really_exists(code):
    """If the module were deleted, the test above would be defending nothing."""
    builds = " ".join(
        p.read_text(encoding="utf-8")
        for p in Path("orchestrator/blueprints").glob("*.yaml"))

    assert code in builds, (
        f"no blueprint claims to build {code}; the reason for keeping it is gone")


@pytest.mark.parametrize("code", NEEDS_A_HUMAN_MODULE)
def test_a_withdrawn_component_says_why(db, code):
    """A REFUSAL MUST EXPLAIN AND GUIDE. "End-of-life" would be the wrong reason
    as well as an unhelpful one -- OCI Functions is very much alive at Oracle,
    and a requester told the wrong reason goes and argues with the wrong people.
    """
    note = (db.get(TechnologyDelivery, code).note or "")

    assert "withdrawn" in note.lower(), f"{code} does not say it was withdrawn"
    assert "infrastructure team" in note.lower(), (
        f"{code} does not say who could make it available again")


def test_the_refusal_carries_that_reason(db):
    """The note is worth nothing if the message a requester sees ignores it."""
    from api import validation

    source = Path("api/validation.py").read_text(encoding="utf-8")
    at = source.index('elif tech.lifecycle_state == "eol":')
    block = source[at:at + 1200]

    assert "TechnologyDelivery" in block, (
        "the end-of-life refusal does not read the recorded reason")
    assert "is end-of-life and can no longer be requested" not in source, (
        "the refusal still calls everything end-of-life, including things "
        "withdrawn because this portal cannot build them")
    assert validation is not None
