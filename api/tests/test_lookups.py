"""Increment 1.2 checks: /api/lookups returns the seeded reference data.

Runs against a fast in-memory database seeded with the real seed function, via a
dependency override — no Docker needed.
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from api.main import app, get_session
from db.seed import seed
from db.session import Base


@pytest.fixture()
def client():
    # StaticPool + check_same_thread=False so the one in-memory database is
    # shared across the request thread TestClient runs the endpoint in.
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(engine)
    TestSession = sessionmaker(bind=engine, expire_on_commit=False)
    session = TestSession()
    seed(session)

    def override_get_session():
        yield session

    app.dependency_overrides[get_session] = override_get_session
    yield TestClient(app)
    app.dependency_overrides.clear()
    session.close()


def test_lookups_returns_all_categories(client):
    response = client.get("/api/lookups")
    assert response.status_code == 200
    data = response.json()
    assert len(data["projects"]) == 3
    assert len(data["cost_centres"]) == 3
    # SIX capabilities are offered separately (2026-08-22). "Backup & Recovery"
    # and its like have no package, no archive and no cloud resource — listing
    # them as components let a requester select infrastructure and receive a work
    # item, and REQ-2026-0183 spent a real machine discovering that
    # `dnf install backup` finds nothing.
    assert len(data["platform_services"]) == 6

    # DERIVED, NOT COUNTED. This asserted 40 and 46, and the numbers were the
    # point of the test only by accident: the line beneath them says what it is
    # really for — "an entry went missing rather than moving". Hard counts
    # answer that question badly. They break whenever the catalogue changes for
    # a good reason (two entries were withdrawn on 2026-08-31 because nothing
    # can build them), and they would still pass if an entry vanished while
    # another was added.
    #
    # The invariant is exact and maintains itself: every technology the
    # catalogue still offers appears in one list or the other, and none appears
    # in neither.
    from sqlalchemy import select

    from db.models import Technology

    session = next(iter(app.dependency_overrides[get_session]()))
    offered = {
        t.code for t in session.scalars(
            select(Technology).where(Technology.lifecycle_state != "eol")).all()}
    listed = ({t["code"] for t in data["technologies"]}
              | {t["code"] for t in data["platform_services"]})

    assert listed == offered, (
        "an entry went missing rather than moving between the two lists: "
        f"absent {sorted(offered - listed)}, unexpected {sorted(listed - offered)}")
    assert len(data["technologies"]) > 30, (
        "the component list has collapsed; the filter is removing too much")
    assert len(data["environments"]) == 2


def test_lookups_contain_named_values(client):
    data = client.get("/api/lookups").json()
    project_names = {p["name"] for p in data["projects"]}
    assert "eGate Modernisation" in project_names

    tech_codes = {t["code"] for t in data["technologies"]}
    assert "postgres16" in tech_codes
    # Lifecycle state is carried through for the dropdown label (F-CAT-02).
    postgres = next(t for t in data["technologies"] if t["code"] == "postgres16")
    assert postgres["lifecycle_state"] == "certified"
