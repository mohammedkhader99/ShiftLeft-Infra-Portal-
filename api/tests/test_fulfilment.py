"""Catalogue honesty: which technologies are genuinely automated (F-CAT).

The answer now comes from ONE place — the blueprint registry. Certifying a
blueprint is what makes a technology automated, so the badge must follow it.

That single source is the point of these tests: when the badge was computed from
rules in code AND blueprints were certified in a table, the two drifted — a
blueprint was certified on the Blueprints page and the request form still said
"manual".
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from api import fulfilment
from api.main import app, get_session
from db.models import Blueprint, Technology
from db.seed import seed
from db.session import Base


@pytest.fixture()
def _db():
    engine = create_engine("sqlite+pysqlite:///:memory:", connect_args={"check_same_thread": False},
                           poolclass=StaticPool, future=True)
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine, expire_on_commit=False)()
    seed(s)
    yield s
    s.close()


@pytest.fixture()
def session(_db):
    return _db


@pytest.fixture()
def client(_db):
    def override():
        yield _db
    app.dependency_overrides[get_session] = override
    yield TestClient(app)
    app.dependency_overrides.clear()


def _certify(session, code, target, ref="x/y", status="certified"):
    session.merge(Blueprint(technology_code=code, deployment_target=target,
                            blueprint_ref=ref, version="1.0.0", status=status))
    session.commit()
    fulfilment.invalidate_cache()


def _tech(session, code) -> Technology:
    return session.scalar(select(Technology).where(Technology.code == code))


# --- The registry decides ----------------------------------------------------

def test_a_certified_blueprint_makes_it_automated(session):
    pairs = fulfilment.certified_pairs(session)
    assert fulfilment.fulfilment_for(_tech(session, "apache"), "oci", pairs)["mode"] == "manual"

    _certify(session, "apache", "oci", "oci/apache-httpd")
    pairs = fulfilment.certified_pairs(session)
    out = fulfilment.fulfilment_for(_tech(session, "apache"), "oci", pairs)
    assert out["mode"] == "automated"
    assert "certified blueprint" in out["reason"]


def test_certification_is_per_cloud(session):
    """Certified on OCI must not make it automated on Azure."""
    _certify(session, "apache", "oci")
    pairs = fulfilment.certified_pairs(session)
    tech = _tech(session, "apache")
    assert fulfilment.fulfilment_for(tech, "oci", pairs)["mode"] == "automated"
    for other in ("azure", "aws", "gcp", "onprem"):
        assert fulfilment.fulfilment_for(tech, other, pairs)["mode"] == "manual", other


def test_a_draft_blueprint_is_not_automated(session):
    """Shipping a recipe is not approving it. Only 'certified' counts."""
    _certify(session, "apache", "oci", status="draft")
    pairs = fulfilment.certified_pairs(session)
    assert fulfilment.fulfilment_for(_tech(session, "apache"), "oci", pairs)["mode"] == "manual"


def test_withdrawing_certification_returns_it_to_manual(session):
    _certify(session, "apache", "oci")
    assert fulfilment.fulfilment_for(_tech(session, "apache"), "oci",
                                     fulfilment.certified_pairs(session))["mode"] == "automated"
    session.delete(session.get(Blueprint, ("apache", "oci")))
    session.commit()
    fulfilment.invalidate_cache()
    assert fulfilment.fulfilment_for(_tech(session, "apache"), "oci",
                                     fulfilment.certified_pairs(session))["mode"] == "manual"


def test_nothing_certified_means_nothing_automated(session):
    """The honest starting position: an empty registry is a fully manual portal."""
    pairs = fulfilment.certified_pairs(session)
    assert pairs == set()
    techs = session.scalars(select(Technology)).all()
    assert not [t for t in techs
                if fulfilment.automated_targets(t, ["onprem", "azure", "oci", "aws", "gcp"], pairs)]


# --- Reasons -----------------------------------------------------------------

def test_a_technology_with_a_config_template_says_why_it_is_still_manual(session):
    out = fulfilment.fulfilment_for(_tech(session, "nginx"), "oci",
                                    fulfilment.certified_pairs(session))
    assert out["mode"] == "manual"
    assert "first-boot configuration exists" in out["reason"]


def test_a_technology_with_no_recipe_at_all_says_so_plainly(session):
    out = fulfilment.fulfilment_for(_tech(session, "kafka"), "oci",
                                    fulfilment.certified_pairs(session))
    assert out["mode"] == "manual"
    assert "infrastructure team provisions it" in out["reason"]


# --- automated_targets -------------------------------------------------------

def test_automated_targets_filters_to_certified_clouds(session):
    _certify(session, "postgres16", "oci")
    pairs = fulfilment.certified_pairs(session)
    pg = _tech(session, "postgres16")
    assert fulfilment.automated_targets(pg, ["onprem", "azure", "oci", "aws", "gcp"], pairs) == ["oci"]
    assert fulfilment.automated_targets(_tech(session, "kafka"), ["oci", "aws"], pairs) == []


def test_unknown_target_or_missing_attrs_do_not_raise():
    class Bare:
        code = ""
    assert fulfilment.fulfilment_for(Bare(), None, set())["mode"] == "manual"
    assert fulfilment.fulfilment_for(Bare(), "nonsense", set())["mode"] == "manual"


# --- Robustness --------------------------------------------------------------

def test_a_lookup_failure_never_blanks_the_catalogue(monkeypatch):
    """An empty set would silently mark every technology manual. On error the last
    known answer is kept instead."""
    fulfilment._cache["at"] = 0.0
    fulfilment._cache["pairs"] = {("apache", "oci")}

    import db.session as dbs

    def _boom():
        raise RuntimeError("db down")
    monkeypatch.setattr(dbs, "SessionLocal", _boom)
    assert fulfilment.certified_pairs() == {("apache", "oci")}


# --- Exposed through the API -------------------------------------------------

def test_lookups_follows_certification(client, session):
    by_code = {t["code"]: t for t in client.get("/api/lookups").json()["technologies"]}
    assert by_code["apache"]["automated_targets"] == []

    _certify(session, "apache", "oci", "oci/apache-httpd")
    by_code = {t["code"]: t for t in client.get("/api/lookups").json()["technologies"]}
    assert by_code["apache"]["automated_targets"] == ["oci"]
    # Its other targets are untouched.
    assert len(by_code["apache"]["targets"]) == 5


# --- Drift guard -------------------------------------------------------------

def test_configurable_codes_match_the_orchestrators_templates():
    """The catalogue's idea of what has a first-boot configuration must match the
    orchestrator's actual templates."""
    from orchestrator import configure
    assert fulfilment.CONFIGURABLE_CODES == set(configure.TEMPLATES)
    assert fulfilment.CONFIG_VERIFIED_CODES == configure.VERIFIED_CODES, (
        "the catalogue's copy of what has been proven has drifted from the "
        "orchestrator's record — update api/fulfilment.CONFIG_VERIFIED_CODES")
