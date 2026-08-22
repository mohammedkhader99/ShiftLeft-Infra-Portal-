"""A price nobody can compute is not a price of zero (REQ-2026-0176).

The portal quoted 0.00 AED for keycloak, a human approved it at that figure, and
the orchestrator then refused to build it: "monthly 90.59 exceeds approved 0.00
by more than 10%". The execution guard was right. What was wrong was that a
fictional price reached an approver at all.

Internally the estimate already knew: the line said resolved=False and the
top-level `unpriced` named the component. Only the TOTAL said 0.00, and the total
is what the form shows and what the approval records — so the honest signal was
carried everywhere except where a decision was made on it.

The standing rule this breaks: validation must tell the truth first, refusing the
request with a reason and guidance rather than accepting it and failing later.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from fastapi.testclient import TestClient

from api import pricing
from api.main import app
from db.seed import seed
from api.main import get_session
from db.models import Blueprint
from db.session import Base


@pytest.fixture()
def db_session():
    engine = create_engine("sqlite+pysqlite:///:memory:",
                           connect_args={"check_same_thread": False},
                           poolclass=StaticPool, future=True)
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine, expire_on_commit=False)()
    seed(s)
    # A catalogue that uses blueprints, as production does. Without any, every
    # target falls back to the VM model and nothing is ever unpriceable — which
    # would make this whole file pass for the wrong reason.
    for code, kind in (("nginx", "oci-service-vm"),
                       ("oci-objectstorage", "oci-bucket")):
        s.add(Blueprint(technology_code=code, deployment_target="oci",
                        blueprint_ref=f"oci/{code}", resource_kind=kind,
                        status="certified", certified_by="test"))
    s.commit()
    yield s
    s.close()


@pytest.fixture()
def client(db_session):
    def override():
        yield db_session
    app.dependency_overrides[get_session] = override
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_an_unpriceable_component_is_named_at_the_top_level(db_session):
    """The signal a decision can actually see. A line-level resolved=False is
    invisible to anything reading totals."""
    est = pricing.estimate_cost(
        [{"technology_code": "keycloak", "size": "small"}], "oci", db_session)

    assert est["unpriced"], "nothing said this could not be priced"
    assert est["totals"]["monthly"] == 0.0


def test_zero_and_unknown_are_distinguishable(db_session):
    """The whole defect in one assertion. Both come back as 0.00; only
    `unpriced` tells them apart, so anything acting on the number must read it."""
    unknown = pricing.estimate_cost(
        [{"technology_code": "keycloak", "size": "small"}], "oci", db_session)
    assert unknown["totals"]["monthly"] == 0.0 and unknown["unpriced"]


def test_submitting_an_unpriceable_request_is_refused_with_a_reason(client):
    """It must not reach an approver. REQ-2026-0176 did, and was approved at a
    price that was never real."""
    created = client.post("/api/requests/draft", json={
        "requester": "a@b.com", "request_type": "create",
        "deployment_target": "oci", "environment_tier": "Development",
        "environment_name": "test-unpriceable", "project_code": "EGATE",
        "cost_centre_code": "IMD-2002", "subsidiary": "EMRTECH",
        "business_justification": "checking the refusal",
        "required_delivery_date": "2026-12-01", "data_classification": "internal",
        "priority": "low", "business_criticality": "tier4",
        "components": [{"technology_code": "keycloak", "size": "small"}],
    })
    assert created.status_code in (200, 201), created.text
    reference = created.json()["reference"]

    result = client.post(f"/api/requests/{reference}/submit")

    assert result.status_code == 422, (
        f"an unpriceable request reached approval: {result.status_code}")
    errors = " ".join(result.json().get("errors") or [])
    assert "cannot be priced" in errors
    assert "Keycloak" in errors or "keycloak" in errors


def test_the_refusal_says_what_would_make_it_priceable(client):
    """A refusal that does not tell you what to do next is half a refusal — the
    standing rule is that the form explains and guides."""
    created = client.post("/api/requests/draft", json={
        "requester": "a@b.com", "request_type": "create",
        "deployment_target": "oci", "environment_tier": "Development",
        "environment_name": "test-unpriceable-2", "project_code": "EGATE",
        "cost_centre_code": "IMD-2002", "subsidiary": "EMRTECH",
        "business_justification": "checking the guidance",
        "required_delivery_date": "2026-12-01", "data_classification": "internal",
        "priority": "low", "business_criticality": "tier4",
        "components": [{"technology_code": "keycloak", "size": "small"}],
    })
    reference = created.json()["reference"]
    errors = " ".join(
        client.post(f"/api/requests/{reference}/submit").json().get("errors") or [])

    assert "certified blueprint" in errors, "it refused without saying why"
    assert "certify it" in errors or "remove it" in errors, (
        "it refused without saying what to do")


def test_a_priceable_request_is_unaffected(client):
    """The refusal must be about not KNOWING the price, never about the price
    being low. nginx prices normally and must still submit."""
    created = client.post("/api/requests/draft", json={
        "requester": "a@b.com", "request_type": "create",
        "deployment_target": "oci", "environment_tier": "Development",
        "environment_name": "test-priceable", "project_code": "EGATE",
        "cost_centre_code": "IMD-2002", "subsidiary": "EMRTECH",
        "business_justification": "the normal path",
        "required_delivery_date": "2026-12-01", "data_classification": "internal",
        "priority": "low", "business_criticality": "tier4",
        "components": [{"technology_code": "nginx", "size": "small"}],
    })
    reference = created.json()["reference"]
    result = client.post(f"/api/requests/{reference}/submit")

    assert result.status_code != 422 or "cannot be priced" not in str(result.json()), (
        f"a priceable request was refused: {result.text[:300]}")
