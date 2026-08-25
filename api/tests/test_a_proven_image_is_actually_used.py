"""G2: a captured image becomes a faster provisioning — or costs nothing.

G1 kept the machine that passed. On its own that was a storage bill: every row
said `capturing` and nothing read them. G2 is the other half.

Two properties carry the whole increment, and nearly every test here defends one
of them:

  1. A GOLDEN IMAGE IS A FAST PATH, NEVER A DEPENDENCY. Missing, still building,
     failed, unreachable, ambiguous — every one falls back to installing from
     repositories at first boot, which is what the platform did before any of
     this existed.

  2. SKIPPING THE INSTALL NEVER SKIPS THE PROOF. The boot report still runs the
     same version command against the same ports. An image captured wrong must
     fail visibly on first use, not ship a broken runtime to everyone quietly.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from api import golden
from db.models import GoldenImage
from db.session import Base

NOW = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool, future=True)
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine, expire_on_commit=False)()
    yield s
    s.close()


def add(session, code="dotnet8", *, state="capturing", ocid="ocid1.image.oc1..a",
        target="oci", created=NOW):
    row = GoldenImage(technology_code=code, deployment_target=target,
                      image_ocid=ocid, state=state, created_at=created)
    session.add(row)
    session.commit()
    return row


# --- promotion: only the cloud knows when an image is ready -------------------

def test_an_image_oci_calls_AVAILABLE_becomes_usable(session):
    add(session)
    moved = golden.promote(session, lambda o: {"ocid1.image.oc1..a": "AVAILABLE"}, now=NOW)

    assert [m["state"] for m in moved] == ["available"]
    assert golden.usable_for(session, ["dotnet8"], "oci") == {"dotnet8": "ocid1.image.oc1..a"}


def test_an_image_still_building_is_left_alone(session):
    """Ten to twenty minutes is normal. Condemning it would throw away an image
    that was about to work."""
    add(session)
    assert golden.promote(session, lambda o: {"ocid1.image.oc1..a": "PROVISIONING"},
                          now=NOW) == []
    assert golden.usable_for(session, ["dotnet8"], "oci") == {}


def test_an_image_that_could_not_be_ASKED_about_is_left_alone(session):
    """'I could not reach the orchestrator' and 'the image failed' are different
    facts. Collapsing them would condemn good images on every network hiccup."""
    add(session)
    assert golden.promote(session, lambda o: {}, now=NOW) == []
    assert session.query(GoldenImage).one().state == "capturing", (
        "an unanswered image was condemned")


def test_an_image_oci_calls_dead_is_failed(session):
    add(session)
    moved = golden.promote(session, lambda o: {"ocid1.image.oc1..a": "FAILED"}, now=NOW)
    assert moved[0]["state"] == "failed"


def test_a_row_nobody_can_answer_for_is_eventually_given_up_on(session):
    """Otherwise it is asked about on every poll cycle, forever."""
    add(session, created=NOW - timedelta(hours=3))
    moved = golden.promote(session, lambda o: {}, now=NOW)

    assert moved[0]["state"] == "failed"
    assert "stopped asking" in moved[0]["detail"]
    assert "capturing" not in moved[0]["detail"], (
        "the message reports the state it just overwrote")


def test_giving_up_waits_the_full_window(session):
    add(session, created=NOW - timedelta(hours=1))
    assert golden.promote(session, lambda o: {}, now=NOW) == []


# --- selection: an ordinary "no" -----------------------------------------------

def test_no_image_is_an_ordinary_answer(session):
    assert golden.usable_for(session, ["dotnet8"], "oci") == {}


def test_an_image_for_another_cloud_is_not_offered(session):
    add(session, state="available", target="aws")
    assert golden.usable_for(session, ["dotnet8"], "oci") == {}


def test_a_failed_image_is_never_offered(session):
    add(session, state="failed")
    assert golden.usable_for(session, ["dotnet8"], "oci") == {}


def test_the_newest_proven_image_wins(session):
    add(session, state="available", ocid="ocid1.image.oc1..old", created=NOW - timedelta(days=9))
    add(session, state="available", ocid="ocid1.image.oc1..new", created=NOW)
    assert golden.usable_for(session, ["dotnet8"], "oci") == {"dotnet8": "ocid1.image.oc1..new"}


# --- one machine boots one image ----------------------------------------------

def test_two_technologies_with_images_fall_back_entirely(session):
    """A machine boots exactly ONE image. Taking the first would deliver a
    machine missing software somebody asked for — while the boot script, told
    both were preinstalled, skipped both installs and checked neither."""
    ready = {"dotnet8": "img-a", "nginx": "img-b"}
    components = [{"technology_code": "dotnet8"}, {"technology_code": "nginx"}]

    assert golden.one_image_per_machine(ready, components) == {}, (
        "two golden images were claimed for one machine")


def test_one_technology_with_an_image_alongside_others_is_fine(session):
    """The common stack: one thing has a proven image, the rest install at boot
    on top of it."""
    ready = {"dotnet8": "img-a"}
    components = [{"technology_code": "dotnet8"}, {"technology_code": "nginx"}]

    assert golden.one_image_per_machine(ready, components) == {"dotnet8": "img-a"}


def test_no_components_is_no_image(session):
    assert golden.one_image_per_machine({"dotnet8": "img-a"}, []) == {}


# --- the speed-up must not break what it sits beside --------------------------

def test_the_promotion_sweep_cannot_break_the_certification_sweep():
    """Written after doing exactly this.

    `golden.promote` was first placed INSIDE the certification sweep's try
    block. It calls the orchestrator, so an unreachable orchestrator raised —
    and took the whole certification review down with it. Blueprints that should
    have been suspended stayed certified, silently, which is the precise failure
    C1 exists to prevent.

    Checked structurally, because the behavioural test that caught it did so by
    accident: it happened to run without a reachable orchestrator. An accident
    is not a guard.
    """
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent / "main.py").read_text(
        encoding="utf-8")

    start = src.index("# Certification review (C1)")
    end = src.index('append_audit(session, "certification.sweep.error"', start)

    assert "golden.promote" not in src[start:end], (
        "the golden-image sweep is inside the certification sweep's try block, "
        "so a failure promoting an image withdraws no certification")
    assert "golden.promote" in src, "the sweep is not wired in at all"


# --- the deployment shape this was NOT designed for ---------------------------

def test_a_stand_in_OCID_is_never_handed_to_terraform(session):
    """Found on deploy, 2026-08-25, and it would have broken real requests.

    This deployment runs PROVISION_MODE=apply with CLOUD_STATE_MODE=mock — real
    provisioning, mock cloud reads — a combination the capture path had taken
    for one setting. The proof built a REAL machine, the capture returned a fake
    OCID, the promotion sweep called that fake available, and the next real
    request would have booted `ocid1.image.mock....` and failed an apply that
    had already been approved.

    This is the last check before Terraform, so it does not care how the value
    got here."""
    add(session, state="available", ocid="ocid1.image.mock.golden-dotnet8-proof")

    assert golden.usable_for(session, ["dotnet8"], "oci") == {}, (
        "a stand-in image OCID would be handed to a real apply")


@pytest.mark.parametrize("ocid", [
    "", "new", "img-a", "ocid1.instance.oc1..abc", "ocid1.image.mock.x", "latest",
])
def test_only_something_that_could_boot_is_offered(session, ocid):
    add(session, state="available", ocid=ocid)
    assert golden.usable_for(session, ["dotnet8"], "oci") == {}
