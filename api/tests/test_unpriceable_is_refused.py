"""A price nobody can compute is not a price of zero (REQ-2026-0176).

RE-POINTED 2026-08-31, and the reason is worth reading: the example moved
because the entry it used was WITHDRAWN. `oci-adb` — Oracle Autonomous Database
— came off the catalogue with `oci-functions`, at the platform owner's
instruction, because the portal has no Terraform for either and the agent writes
modules for machines, not for managed services a cloud sells.

That left this file testing a path nothing could reach. The property it exists
to hold is unchanged and still matters: an unpriceable component must not stop a
request, and must not let a figure reach an approver without saying what it
leaves out.

`oci-oke` replaces it, and is a better example than the one it replaces. It is
real, still offered, and unpriceable for exactly the reason this file is about —
no certified blueprint says what it builds. It is also the case that prompted
this: REQ-2026-0246 quoted 90.59 for OKE + OpenSearch + OCI Functions, which is
the price of ONE of the three, and the Jira ticket the approver reads carried
that total with no caveat at all until today.

UPDATED 2026-08-22. These used keycloak as the example, and keycloak is no
longer unpriceable: the agent's classifier knows it would be built as software
on a machine, so pricing projects that model and returns a real figure marked
PROVISIONAL (see test_provisional_pricing). What remains genuinely unknowable is
a cloud-managed service with no blueprint — nothing can say whether it is a
bucket, a cluster or a database, and those differ by 100x. That is the case
these now use.

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
        [{"technology_code": "oci-oke", "size": "small"}], "oci", db_session)

    assert est["unpriced"], "nothing said this could not be priced"
    assert est["totals"]["monthly"] == 0.0


def test_zero_and_unknown_are_distinguishable(db_session):
    """The whole defect in one assertion. Both come back as 0.00; only
    `unpriced` tells them apart, so anything acting on the number must read it."""
    unknown = pricing.estimate_cost(
        [{"technology_code": "oci-oke", "size": "small"}], "oci", db_session)
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
        "environment_name": "unpriced1", "project_code": "EGATE",
        "cost_centre_code": "IMD-2002", "subsidiary": "EMRTECH",
        "business_justification": "checking the refusal",
        "required_delivery_date": "2026-12-01", "data_classification": "internal",
        "priority": "low", "business_criticality": "tier4",
        "components": [{"technology_code": "oci-oke", "size": "small"}],
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
        "environment_name": "unpriced2", "project_code": "EGATE",
        "cost_centre_code": "IMD-2002", "subsidiary": "EMRTECH",
        "business_justification": "checking the guidance",
        "required_delivery_date": "2026-12-01", "data_classification": "internal",
        "priority": "low", "business_criticality": "tier4",
        "components": [{"technology_code": "oci-oke", "size": "small"}],
    })
    reference = created.json()["reference"]
    body = client.post(f"/api/requests/{reference}/submit").json()
    warnings = " ".join(body.get("policy_warnings") or [])

    assert "Not costed" in warnings, "the approver was shown a figure with no caveat"
    assert "OCI Container Engine (OKE)" in warnings
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
        [{"technology_code": "oci-oke", "size": "small"}], "oci", db_session)

    assert est["unpriced"] == ["OCI Container Engine (OKE)"], (
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


# --- and the person granting the money reads the TICKET, not the form ------------

def test_the_ticket_the_approver_reads_says_what_the_figure_leaves_out(db_session):
    """THE GAP THIS FILE DID NOT COVER.

    Everything above proves the caveat reaches the SUBMIT RESPONSE, and the
    requester's form shows it. The approver reads neither. They read the Jira
    ticket, and until today that carried the total alone:

        Estimated cost (AED): one-time 0.00, monthly 90.59, annual 1087.04

    for REQ-2026-0246, which asked for OKE, OpenSearch and OCI Functions. 90.59
    is the price of ONE of the three. Nothing in the ticket said so.

    Same reasoning as the licence note beside it: a number an approver cannot
    take apart is a number they cannot judge. REQ-2026-0176 is what that costs —
    0.00 AED quoted, approved by a person, and the build then refused.
    """
    from api import jira
    from db.models import Request, RequestComponent

    req = Request(reference="REQ-2026-9998", requester="a@b.c",
                  request_type="create", deployment_target="oci",
                  environment_name="tkt", project_code="EGATE",
                  cost_centre_code="IMD-2002", data_classification="internal")
    req.components = [RequestComponent(technology_code="oci-oke", size="small")]

    estimate = pricing.estimate_cost(
        [{"technology_code": "oci-oke", "size": "small"}], "oci", db_session)
    assert estimate["unpriced"], "the fixture no longer produces an unpriced line"

    body = jira.build_ticket_body(req, estimate, plan_preview="")

    assert "NOT THE WHOLE COST" in body, (
        "the ticket shows a total with nothing to say what it excludes")
    assert "OCI Container Engine (OKE)" in body, (
        "the ticket does not name what was left out of the figure")


def test_a_fully_priced_ticket_carries_no_such_note(db_session):
    """Only when something is actually missing. A caveat on every ticket is a
    caveat nobody reads."""
    from api import jira
    from db.models import Request, RequestComponent

    req = Request(reference="REQ-2026-9997", requester="a@b.c",
                  request_type="create", deployment_target="oci",
                  environment_name="tkt2", project_code="EGATE",
                  cost_centre_code="IMD-2002", data_classification="internal")
    req.components = [RequestComponent(technology_code="nginx", size="small")]

    estimate = pricing.estimate_cost(
        [{"technology_code": "nginx", "size": "small"}], "oci", db_session)
    assert not estimate["unpriced"]

    body = jira.build_ticket_body(req, estimate, plan_preview="")

    assert "NOT THE WHOLE COST" not in body
