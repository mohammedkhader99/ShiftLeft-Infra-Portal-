"""G3: an image stops being offered, and then stops existing.

G2 made images usable. Nothing made them stop. Left alone, every
re-certification added another ~50 GB to the tenancy and nothing removed the one
it replaced — a feature whose cost grows and never falls.

Three steps, deliberately separate:

    SUPERSEDE   a newer proven image exists, so stop offering this one
    EXPIRE      the proof behind it is older than a certification lasts
    REAP        nothing can select it any more, and the grace period has passed

Retiring is instant and reversible in effect; deleting is neither. Nearly every
test below is about what the delete path REFUSES to do, because this is the only
code in the platform that removes something from the cloud on its own schedule.
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


def add(session, code="dotnet8", *, state="available", ocid=None, target="oci",
        created=NOW, retired=None):
    row = GoldenImage(
        technology_code=code, deployment_target=target,
        image_ocid=ocid if ocid is not None else f"ocid1.image.oc1..{code}-{state}",
        state=state, created_at=created, retired_at=retired)
    session.add(row)
    session.commit()
    return row


def when(value):
    """SQLite hands back naive datetimes where Postgres returns aware ones. The
    code normalises for exactly this (see `reap`); the assertions must too, or
    they would pass on one database and fail on the other."""
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def deleter(fail=False):
    seen: list[str] = []

    def delete(ocid):
        seen.append(ocid)
        return (False, "quota") if fail else (True, f"Deleted {ocid}.")
    delete.seen = seen
    return delete


# --- supersede ----------------------------------------------------------------

def test_only_the_newest_proven_image_stays_available(session):
    add(session, ocid="old", created=NOW - timedelta(days=5))
    add(session, ocid="new", created=NOW)

    changed = golden.supersede(session, now=NOW)

    assert [c["image_ocid"] for c in changed] == ["old"]
    assert golden.usable_for(session, ["dotnet8"], "oci") == {"dotnet8": "new"}


def test_a_lone_image_is_never_superseded(session):
    add(session, ocid="only")
    assert golden.supersede(session, now=NOW) == []


def test_images_for_different_technologies_do_not_supersede_each_other(session):
    add(session, "dotnet8", ocid="a")
    add(session, "nginx", ocid="b")
    assert golden.supersede(session, now=NOW) == []


def test_images_for_different_clouds_do_not_supersede_each_other(session):
    add(session, ocid="a", target="oci")
    add(session, ocid="b", target="aws")
    assert golden.supersede(session, now=NOW) == []


def test_superseding_starts_the_deletion_clock(session):
    """Not `created_at`. An image created sixty days ago and superseded today
    would otherwise be deleted instantly — the precise case the grace period
    exists for."""
    add(session, ocid="old", created=NOW - timedelta(days=60))
    add(session, ocid="new", created=NOW)

    golden.supersede(session, now=NOW)
    old = session.query(GoldenImage).filter_by(image_ocid="old").one()
    assert when(old.retired_at) == NOW


# --- expire -------------------------------------------------------------------

def test_an_image_older_than_the_certification_it_stands_on_expires(session, monkeypatch):
    monkeypatch.setenv("CERTIFICATION_VALIDITY_DAYS", "30")
    add(session, ocid="stale", created=NOW - timedelta(days=31))

    changed = golden.expire(session, now=NOW)

    assert [c["state"] for c in changed] == ["expired"]
    assert golden.usable_for(session, ["dotnet8"], "oci") == {}


def test_a_fresh_image_does_not_expire(session, monkeypatch):
    monkeypatch.setenv("CERTIFICATION_VALIDITY_DAYS", "30")
    add(session, ocid="fresh", created=NOW - timedelta(days=29))
    assert golden.expire(session, now=NOW) == []


def test_expiry_follows_the_SAME_rule_as_certification(session, monkeypatch):
    """Asked of `proof.validity_days()`, not restated. An image is trustworthy
    only because a proof vouched for it, so it cannot outlive the vouching — and
    two copies of "30" would drift the first time either was tuned."""
    monkeypatch.setenv("CERTIFICATION_VALIDITY_DAYS", "5")
    add(session, ocid="stale", created=NOW - timedelta(days=6))

    assert len(golden.expire(session, now=NOW)) == 1, (
        "expiry ignored the configured certification validity")


# --- reap: what it refuses to do ----------------------------------------------

def test_an_AVAILABLE_image_is_never_deleted(session):
    """THE test in this file. An image a request could still be handed must
    never be a deletion candidate, whatever its age."""
    add(session, ocid="live", created=NOW - timedelta(days=999),
        retired=NOW - timedelta(days=999))
    d = deleter()

    assert golden.reap(session, d, now=NOW) == []
    assert d.seen == [], "a usable image was deleted"
    assert "available" not in golden.REAPABLE


def test_a_capturing_image_is_never_deleted(session):
    add(session, state="capturing", retired=NOW - timedelta(days=99))
    d = deleter()
    golden.reap(session, d, now=NOW)
    assert d.seen == []


def test_a_retired_image_inside_the_grace_period_survives(session):
    add(session, state="superseded", ocid="recent", retired=NOW - timedelta(days=2))
    d = deleter()

    assert golden.reap(session, d, now=NOW) == []
    assert d.seen == [], "an image was deleted while a request could still hold it"


def test_a_retired_image_past_the_grace_period_is_deleted(session):
    add(session, state="superseded", ocid="stale", retired=NOW - timedelta(days=8))
    d = deleter()

    done = golden.reap(session, d, now=NOW)

    assert d.seen == ["stale"]
    assert done[0]["state"] == "deleted"
    assert session.query(GoldenImage).one().state == "deleted"


def test_a_row_with_no_retired_at_starts_its_clock_rather_than_dying(session):
    add(session, state="superseded", ocid="orphan", retired=None)
    d = deleter()

    assert golden.reap(session, d, now=NOW) == []
    assert d.seen == []
    assert when(session.query(GoldenImage).one().retired_at) == NOW


def test_a_FAILED_delete_leaves_the_row_alone_to_retry(session):
    """A row marked deleted for an image still in the tenancy is a cost nobody
    can find again."""
    add(session, state="superseded", ocid="stubborn", retired=NOW - timedelta(days=9))

    done = golden.reap(session, deleter(fail=True), now=NOW)

    assert done == []
    row = session.query(GoldenImage).one()
    assert row.state == "superseded", "the row was marked deleted anyway"
    assert "will retry" in row.detail


def test_a_delete_that_EXPLODES_is_not_fatal(session):
    def boom(ocid):
        raise RuntimeError("the SDK fell over")

    add(session, state="superseded", ocid="x", retired=NOW - timedelta(days=9))
    assert golden.reap(session, boom, now=NOW) == []
    assert session.query(GoldenImage).one().state == "superseded"


def test_a_capture_that_never_reached_the_cloud_needs_no_delete(session):
    """A failed capture has no OCID. Calling the cloud about it would be asking
    to delete the empty string."""
    add(session, state="failed", ocid="", retired=NOW - timedelta(days=9))
    d = deleter()

    done = golden.reap(session, d, now=NOW)

    assert d.seen == [], "the cloud was asked to delete nothing"
    assert done[0]["state"] == "deleted"


def test_the_grace_period_can_never_be_zero(session, monkeypatch):
    monkeypatch.setenv("GOLDEN_IMAGE_RETAIN_DAYS", "0")
    assert golden.retain_days() >= 1
    monkeypatch.setenv("GOLDEN_IMAGE_RETAIN_DAYS", "nonsense")
    assert golden.retain_days() == 7


# --- the whole lifecycle ------------------------------------------------------

def test_a_replaced_image_is_retired_then_later_deleted(session):
    """End to end, in the order the sweep runs it: a new proof arrives, the old
    image stops being offered immediately, and is deleted a week later."""
    add(session, ocid="v1", created=NOW - timedelta(days=1))
    add(session, ocid="v2", created=NOW)
    d = deleter()

    golden.supersede(session, now=NOW)
    assert golden.usable_for(session, ["dotnet8"], "oci") == {"dotnet8": "v2"}
    assert golden.reap(session, d, now=NOW) == []          # still in grace

    later = NOW + timedelta(days=8)
    golden.reap(session, d, now=later)

    assert d.seen == ["v1"]
    assert golden.usable_for(session, ["dotnet8"], "oci") == {"dotnet8": "v2"}, (
        "the surviving image stopped being usable")
