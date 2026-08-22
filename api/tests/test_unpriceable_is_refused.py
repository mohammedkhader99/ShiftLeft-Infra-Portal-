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


def test_an_unpriceable_request_is_still_SUBMITTABLE(client):
    """CORRECTED 2026-08-22. This first REFUSED the submission — and that broke
    manual fulfilment, which is a documented feature people rely on: the portal
    validates, prices, approves and audits requests the infrastructure team
    builds by hand, and a component with no certified blueprint is exactly the
    case it serves. Blocking here took kafka, mongodb, vault, elasticsearch and
    every other uncertified technology off the portal.

    The fix for a fictional price is to STOP SHOWING ONE, not to refuse the
    request."""
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

    assert result.status_code != 422, (
        f"manual fulfilment was blocked: {result.text[:300]}")


def test_the_approver_is_warned_that_the_estimate_is_not_the_whole_cost(client):
    """The honest half. The request goes through, but nobody may mistake the
    figure for a complete one — REQ-2026-0176 was approved at 0.00 AED."""
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
    body = client.post(f"/api/requests/{reference}/submit").json()
    warnings = " ".join(body.get("policy_warnings") or [])

    assert "Not costed" in warnings, "the approver was shown a figure with no caveat"
    assert "Keycloak" in warnings
    assert "NOT the whole cost" in warnings


def test_a_priceable_request_carries_no_such_warning(client):
    """The warning must be about not KNOWING the price, never about the price
    being low."""
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
    body = client.post(f"/api/requests/{reference}/submit").json()

    assert "Not costed" not in " ".join(body.get("policy_warnings") or [])


def test_the_form_is_told_which_components_cannot_be_priced(db_session):
    """THE half that was missing. Refusing at submit is not enough — the live
    cost panel showed 0.00 AED while the form was being filled in, so a
    requester learned nothing was priceable only after asking for approval.
    `unpriced` is what lets the panel say "Not priced" instead of a figure.
    """
    est = pricing.estimate_cost(
        [{"technology_code": "keycloak", "size": "small"}], "oci", db_session)

    assert est["unpriced"] == ["Keycloak"], (
        "the cost endpoint does not name what it could not price, so the form "
        "cannot show anything but the total")


def test_manual_fulfilment_survives_for_every_uncertified_technology(client, db_session):
    """THE regression this file caused and now guards. Refusing unpriceable
    submissions removed the whole manual-fulfilment path on a target that uses
    blueprints — which is most of the catalogue."""
    for code in ("kafka", "mongodb", "vault"):
        created = client.post("/api/requests/draft", json={
            "requester": "a@b.com", "request_type": "create",
            "deployment_target": "oci", "environment_tier": "Development",
            "environment_name": f"mf-{code[:4]}", "project_code": "EGATE",
            "cost_centre_code": "IMD-2002", "subsidiary": "EMRTECH",
            "business_justification": "the infrastructure team will build it",
            "required_delivery_date": "2026-12-01", "data_classification": "internal",
            "priority": "low", "business_criticality": "tier4",
            "components": [{"technology_code": code, "size": "small"}],
        })
        assert created.status_code in (200, 201), created.text
        result = client.post(f"/api/requests/{created.json()['reference']}/submit")
        assert result.status_code != 422, f"{code} can no longer be requested: {result.text[:200]}"
