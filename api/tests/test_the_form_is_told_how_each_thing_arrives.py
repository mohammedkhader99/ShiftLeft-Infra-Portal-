"""The catalogue knew how each thing is delivered, and the form never asked.

`GET /api/catalogue/delivery` has grouped the catalogue since S2 -- managed
services, software on a machine, bare machines, capabilities -- from the same
source the agent uses to decide what to build. That endpoint was written so the
grouping a requester sees and the decision the agent makes could never disagree
again, after they did, about `postgres16`.

The request form has never called it. It fetches `/api/lookups` and renders one
flat list of every technology on the selected cloud, so a requester choosing
between OCI's managed PostgreSQL and PostgreSQL installed on a VM sees two
names and nothing to tell them apart -- which is the difference between a
database Oracle patches and one they patch.

So `lookups` now carries the same field, read from the same function. Not
computed here: a second source is what produced the postgres16 disagreement,
and the endpoint's own docstring says so.

WHAT "" MEANS, and it is not "software". It means the catalogue does not RECORD
how this arrives, and the grouped view has always reported those separately
rather than guessing. Eight technologies were in that state until 2026-09-05 --
six of them CERTIFIED and buildable, reaching the form with no group at all --
which is what prompted this.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

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
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def technologies(client) -> dict:
    body = client.get("/api/lookups").json()
    return {t["code"]: t for t in body["technologies"]}


def test_the_form_is_told_how_each_thing_arrives(client):
    """THE DEFECT. Without this the picker cannot group, and did not."""
    got = technologies(client)

    assert got["postgres16"]["delivery_model"] == "managed"
    assert got["mysql"]["delivery_model"] == "software"
    assert got["rhel9"]["delivery_model"] == "machine"


def test_the_two_postgres_entries_can_be_told_apart(client):
    """The disagreement the shared source exists to prevent, seen from the
    requester's side: OCI's managed database and PostgreSQL on a machine are
    different products with the same word in their name."""
    got = technologies(client)

    assert got["postgres16"]["delivery_model"] == "managed"
    assert got["oracle-free"]["delivery_model"] == "software"


def test_every_technology_the_form_offers_has_a_group(client):
    """THE GUARD. Six CERTIFIED technologies reached the form ungrouped because
    nothing checked. A technology added tomorrow and left out of DELIVERY would
    do the same, and the form would show it under whatever heading a front end
    invents for "" -- which is the guessing this table exists to stop."""
    ungrouped = sorted(code for code, t in technologies(client).items()
                       if not t["delivery_model"])

    assert ungrouped == [], (
        f"these would reach the request form with no group: {ungrouped}")


def test_a_technology_the_catalogue_does_not_record_is_not_guessed_at(db, client):
    """"" is honest and must stay reachable: the field says the catalogue does
    not know, and the form is expected to say so rather than pick a group.

    Asserted by ADDING one, because every real technology is now classified --
    the same reason the fallback test in test_delivery_model.py keeps an
    invented code."""
    db.add(Technology(code="a-technology-nobody-has-classified", name="Unknown",
                      lifecycle_state="certified", targets="oci"))
    db.commit()

    got = technologies(client)

    assert got["a-technology-nobody-has-classified"]["delivery_model"] == ""


def test_the_group_comes_from_the_same_source_as_the_grouped_view(client):
    """ONE SOURCE, checked rather than trusted. `/api/catalogue/delivery` and
    `/api/lookups` must not drift, because they did once and disagreed about
    postgres16."""
    grouped = client.get("/api/catalogue/delivery?target=oci").json()
    from_view = {entry["code"]: model
                 for model, items in grouped["groups"].items()
                 for entry in items}
    from_form = {code: t["delivery_model"]
                 for code, t in technologies(client).items()
                 if "oci" in t["targets"] and t["delivery_model"]}

    disagree = {code: (from_form[code], from_view.get(code))
                for code in from_form
                if code in from_view and from_view[code] != from_form[code]}
    assert disagree == {}, f"the form and the grouped view disagree: {disagree}"
