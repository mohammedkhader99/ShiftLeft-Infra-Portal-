"""Increment 1.1 checks: the schema builds and the seed fills reference data.

These run against an in-memory SQLite database so they're fast and need no
Docker. The models use portable column types, so the same schema works here and
on PostgreSQL.
"""

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from db.models import CostCentre, Project, RateCard, SizingAnchor, Technology
from db.seed import seed
from db.session import Base


@pytest.fixture()
def session():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    TestSession = sessionmaker(bind=engine, expire_on_commit=False)
    with TestSession() as s:
        yield s


def test_seed_populates_reference_data(session):
    seed(session)
    assert session.scalar(select(func.count()).select_from(Project)) == 3
    assert session.scalar(select(func.count()).select_from(CostCentre)) == 3
    assert session.scalar(select(func.count()).select_from(Technology)) == 22
    # 22 technologies * 4 sizes (incl. xlarge) = 88 sizing anchors.
    assert session.scalar(select(func.count()).select_from(SizingAnchor)) == 88
    assert session.scalar(select(func.count()).select_from(RateCard)) == 20  # +3 cloud_aws +3 cloud_gcp


def test_seed_query_returns_named_rows(session):
    """The approval test: a seed query returns the named reference data."""
    seed(session)
    project_names = set(session.scalars(select(Project.name)).all())
    assert "eGate Modernisation" in project_names

    tech = session.scalar(select(Technology).where(Technology.code == "postgres16"))
    assert tech.name == "PostgreSQL 16"
    assert tech.lifecycle_state == "certified"

    rate = session.scalar(select(RateCard).where(RateCard.item == "vcpu"))
    assert rate.kind == "onprem"


def test_seed_is_idempotent(session):
    """Running the seed twice must not create duplicates."""
    seed(session)
    seed(session)
    assert session.scalar(select(func.count()).select_from(Project)) == 3
    assert session.scalar(select(func.count()).select_from(SizingAnchor)) == 88


def test_lifecycle_states_present(session):
    """Technologies carry a lifecycle state (F-CAT-02): preview and deprecated exist."""
    seed(session)
    states = set(session.scalars(select(Technology.lifecycle_state)).all())
    assert {"certified", "preview", "deprecated"} <= states
