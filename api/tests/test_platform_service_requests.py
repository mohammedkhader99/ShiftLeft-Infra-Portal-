"""A capability is asked for, not selected from a component list.

The reviewer's question, and it was the right one: why offer a requester
something the portal can never build? "Backup & Recovery", "Centralised
Logging", "Monitoring & Alerting", "API Gateway", "Service Mesh" and generic
"Kubernetes" sat in the component list beside NGINX. A requester could select
one, have it priced, have it approved, wait — and receive a work item instead of
infrastructure. REQ-2026-0183 went further and spent a real machine discovering
that `dnf install backup` finds nothing.

The form already labelled them `manual`, which is honest for kafka ("no
automated build YET") and misleading for these ("there will never be one").

So they leave the component list and keep their route: a `platform-service`
request asks the infrastructure team directly. No sizing, no price, no build path
to fail at — and the justification, the approval and the audit trail, which is the
whole reason to raise it in the portal rather than by email.

WHAT WAS DELIBERATELY NOT REMOVED: the twelve unproven software entries (kafka,
mongodb, vault and their like). Those are how components get certified — apache
and keycloak were both unproven on the morning of 2026-08-22 and certified by the
evening, because somebody selected them. Hiding them would stop that loop, break
manual fulfilment (a documented feature, broken by accident once already that
day), and remove the demand signal _catalogue_gaps exists to read.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from api.validation import validate_submission
from db.seed import seed
from db.session import Base

CAPABILITIES = ["backup", "logging", "monitoring", "api-gateway",
                "service-mesh", "k8s"]


@pytest.fixture()
def session():
    engine = create_engine("sqlite+pysqlite:///:memory:",
                           connect_args={"check_same_thread": False},
                           poolclass=StaticPool, future=True)
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine, expire_on_commit=False)()
    seed(s)
    s.commit()
    yield s
    s.close()


def a_request(code="backup", justification="Nightly backups of the eGate database.",
              components=None):
    return {
        "request_type": "platform-service", "requester": "a@b.com",
        "deployment_target": "oci", "environment_tier": "Development",
        "environment_name": "egate-dev", "project_code": "EGATE",
        "cost_centre_code": "IMD-2002", "subsidiary": "EMRTECH",
        "business_justification": justification,
        "required_delivery_date": "2026-12-01", "data_classification": "internal",
        "priority": "low", "business_criticality": "tier4",
        "components": components if components is not None
                      else [{"technology_code": code}],
    }


# --- capabilities are no longer components ------------------------------------

@pytest.mark.parametrize("code", CAPABILITIES)
def test_a_capability_is_not_offered_as_a_component(session, code):
    """The lookups endpoint feeds the form's component list. A capability in it
    invites a requester to choose between two different kinds of thing without
    telling them so."""
    from fastapi.testclient import TestClient

    from api.main import app, get_session

    def override():
        yield session
    app.dependency_overrides[get_session] = override
    try:
        body = TestClient(app).get("/api/lookups").json()
    finally:
        app.dependency_overrides.clear()

    assert code not in {t["code"] for t in body["technologies"]}, (
        f"{code} can still be selected as a component and can never be built")
    assert code in {p["code"] for p in body["platform_services"]}, (
        f"{code} vanished entirely — a requester now has no way to ask for it")


def test_software_is_still_offered_even_when_unproven(session):
    """THE thing not to break. kafka and vault have no certified blueprint, and
    they must stay selectable: that is how they get proven, and manual
    fulfilment is a documented feature people rely on."""
    from fastapi.testclient import TestClient

    from api.main import app, get_session

    def override():
        yield session
    app.dependency_overrides[get_session] = override
    try:
        body = TestClient(app).get("/api/lookups").json()
    finally:
        app.dependency_overrides.clear()

    offered = {t["code"] for t in body["technologies"]}
    for code in ("kafka", "mongodb", "vault", "nginx", "oracle-db"):
        assert code in offered, f"{code} was removed; nobody can request or prove it"


def test_the_platform_service_list_explains_each_one(session):
    """A requester picking "Service Mesh" should learn what to ask for instead."""
    from fastapi.testclient import TestClient

    from api.main import app, get_session

    def override():
        yield session
    app.dependency_overrides[get_session] = override
    try:
        body = TestClient(app).get("/api/lookups").json()
    finally:
        app.dependency_overrides.clear()

    notes = {p["code"]: p["note"] for p in body["platform_services"]}
    assert "Kubernetes" in notes["service-mesh"]
    assert "oci-oke" in notes["k8s"]
    assert all(notes[c] for c in CAPABILITIES), "a capability with no explanation"


# --- the request type that keeps the route ------------------------------------

@pytest.mark.parametrize("code", CAPABILITIES)
def test_a_capability_can_be_requested(session, code):
    assert validate_submission(a_request(code), session) == {}


def test_it_needs_a_justification_because_that_is_all_it_carries(session):
    """No sizing, no price, no components to describe it. Without the
    justification the team receives a name and no idea what it should achieve."""
    errors = validate_submission(a_request(justification=""), session)
    assert "business_justification" in errors
    assert "what it should protect" in errors["business_justification"]


def test_asking_for_software_through_this_door_is_refused_with_the_other_door(session):
    """nginx is buildable. Routing it here would send a request that could have
    been provisioned to a human instead."""
    errors = validate_submission(a_request("nginx"), session)
    assert "components" in errors
    assert "create request" in errors["components"], (
        "it refused without saying which path would work")


def test_one_capability_at_a_time(session):
    errors = validate_submission(
        a_request(components=[{"technology_code": "backup"},
                              {"technology_code": "logging"}]), session)
    assert "components" in errors


def test_naming_nothing_is_refused_with_the_choices(session):
    errors = validate_submission(a_request(components=[]), session)
    assert "components" in errors
    assert "backup" in errors["components"]


# --- and it never reaches the build path --------------------------------------

def test_a_platform_service_carries_no_price(session):
    """It buys the infrastructure team's time, not a cloud resource. Pricing it
    would produce exactly the fictional 0.00 this portal spent a day removing."""
    import api.main as main
    import inspect

    src = inspect.getsource(main.submit_request)
    assert 'request_type == "platform-service"' in src, (
        "submit still prices a capability")


def test_the_agent_never_drafts_a_recipe_for_one(session):
    """Belt and braces with the catalogue check in autobuild: even reached
    directly, a capability must not become a package guess."""
    from api import ai_blueprint

    for code in CAPABILITIES:
        assert ai_blueprint.classify(code, session)[0] == "capability"
