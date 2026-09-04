"""A golden image outlives the verdict on the recipe it was captured from.

PROOF-MYSQL-20260904T005319-DBF8CE passed on the machine, was captured as a
golden image (audit 12767, `golden.image.available`, 05:09:26), and was refused
by the gate three minutes later: the recipe was the command-line client. The
image stayed `available`. `usable_for` offers the newest available image for a
technology, whatever proof it came from -- so once a real MySQL is certified,
a request would boot the client's image the first time the real one's capture
failed. Four of five MySQL captures have.

Two facts the golden module did not honour:

  * a refusal names the proof it refuses, and the proof names its image;
  * a refutation row is on the record for exactly that proof reference.

So a withdrawal retires the images of the proof it refuses, and the fast path
never offers an image whose proof has been refuted -- whichever of the two
happens first.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from api import golden, recipe_memory
from db.models import GoldenImage
from db.session import Base

NOW = datetime(2026, 9, 4, 1, 12, tzinfo=timezone.utc)
CLIENT = "ocid1.image.oc1.me-dubai-1.aaaaaaaak6rlx7z2bri4mqgbksp4lbep3p4lg7hal2oomrwxhgpovvm7lxyq"
SERVER = "ocid1.image.oc1.me-dubai-1.aaaaaaaadv6tlgp7ztms7zltdww2d3hl2niscgymw24ym25on75mwfmno4pq"
REFUSED = "PROOF-MYSQL-20260904T005319-DBF8CE"
CLIENT_RECIPE = {"code": "mysql", "rhel": {"packages": ["mysql"], "services": []}}


@pytest.fixture
def session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool, future=True)
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine, expire_on_commit=False)()
    yield s
    s.close()


def add(session, *, proof, ocid, state="available", created=NOW, code="mysql"):
    row = GoldenImage(technology_code=code, deployment_target="oci",
                      image_ocid=ocid, proof_reference=proof, state=state,
                      created_at=created)
    session.add(row)
    session.commit()
    return row


def refute(session, proof):
    recipe_memory.remember(session, "mysql", "oci", CLIENT_RECIPE, proof,
                           "mysql installs but runs nothing")
    session.commit()


# --- the fast path ----------------------------------------------------------------

def test_an_image_whose_proof_was_refuted_is_not_offered(session):
    """The DBF8CE shape: the client's image is the newest available one."""
    add(session, proof="PROOF-MYSQL-OLDER", ocid=SERVER, created=NOW - timedelta(days=1))
    add(session, proof=REFUSED, ocid=CLIENT, created=NOW)
    refute(session, REFUSED)

    chosen = golden.usable_for(session, ["mysql"], "oci")

    assert chosen.get("mysql") == SERVER, (
        f"the fast path offered {chosen} -- the image of a recipe the gate refused")


def test_with_only_a_refuted_image_the_answer_is_install_at_boot(session):
    add(session, proof=REFUSED, ocid=CLIENT)
    refute(session, REFUSED)

    assert golden.usable_for(session, ["mysql"], "oci") == {}


def test_an_unrefuted_image_is_still_offered(session):
    """The rule must not take the fast path away from everything."""
    add(session, proof="PROOF-MYSQL-FINE", ocid=SERVER)

    assert golden.usable_for(session, ["mysql"], "oci") == {"mysql": SERVER}


# --- the withdrawal ---------------------------------------------------------------

def test_withdraw_retires_every_image_of_the_proof_it_names(session):
    """Available or still capturing: a capture OCI has not finished yet will
    finish, and it must not finish into `available`."""
    add(session, proof=REFUSED, ocid=CLIENT)
    add(session, proof=REFUSED, ocid="", state="capturing")
    other = add(session, proof="PROOF-MYSQL-FINE", ocid=SERVER)

    gone = golden.withdraw(session, REFUSED, "mysql installs but runs nothing", now=NOW)
    session.commit()

    assert len(gone) == 2
    for row in session.query(GoldenImage).filter_by(proof_reference=REFUSED):
        assert row.state == "withdrawn"
        assert row.retired_at is not None
        assert "runs nothing" in row.detail
    assert session.get(GoldenImage, other.id).state == "available"


def test_withdrawing_a_proof_with_no_image_is_nothing(session):
    assert golden.withdraw(session, "PROOF-NOBODY", "why", now=NOW) == []


def test_a_capture_that_finishes_after_the_withdrawal_stays_withdrawn(session):
    """`promote` moves `capturing` rows on the cloud's word. A row already
    withdrawn is not `capturing`, so the cloud finishing the image cannot
    revive it -- pinned, because that is the race the mysql proof would run."""
    row = add(session, proof=REFUSED, ocid=CLIENT, state="capturing")
    golden.withdraw(session, REFUSED, "refused", now=NOW)
    session.commit()

    golden.promote(session, lambda ocids: {CLIENT: "AVAILABLE"}, now=NOW)
    session.commit()

    assert session.get(GoldenImage, row.id).state == "withdrawn"


# --- and it stops costing money ---------------------------------------------------

def test_a_withdrawn_image_is_reaped_after_the_grace(session):
    """Retired is not deleted; `reap` deletes after the retention window, and
    it only ever looks at states it has been told are safe to delete."""
    row = add(session, proof=REFUSED, ocid=CLIENT)
    golden.withdraw(session, REFUSED, "refused", now=NOW - timedelta(days=30))
    session.commit()

    deleted = []
    done = golden.reap(session, lambda ocid: deleted.append(ocid) or (True, "gone"),
                       now=NOW)

    assert deleted == [CLIENT], "a withdrawn image is never deleted from the cloud"
    assert session.get(GoldenImage, row.id).state == "deleted"
    assert done and done[0]["state"] == "deleted"


def test_a_withdrawn_image_inside_the_grace_is_kept(session):
    row = add(session, proof=REFUSED, ocid=CLIENT)
    golden.withdraw(session, REFUSED, "refused", now=NOW - timedelta(days=1))
    session.commit()

    deleted = []
    golden.reap(session, lambda ocid: deleted.append(ocid) or (True, "gone"), now=NOW)

    assert deleted == []
    assert session.get(GoldenImage, row.id).state == "withdrawn"
