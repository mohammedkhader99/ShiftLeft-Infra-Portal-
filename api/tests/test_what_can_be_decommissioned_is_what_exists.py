"""A machine the portal built and will not let you remove.

REQ-2026-0232 built a real VM, failed its boot verification, and was left at
`verify-failed`. The machine kept running and kept billing — and it did not
appear in the decommission picker, because both layers filtered on STATUS:

    frontend   getRequests({ requester: email, status: 'provisioned' })
    backend    if source.status != "provisioned": "only provisioned requests
               can be decommissioned."

`provisioned` was always a PROXY for "there is something to tear down", and it
is wrong for every request that built machines and then failed: verify-failed,
teardown-failed, a partial apply. The portal had built infrastructure it would
not let anyone remove, which is the worst direction for this kind of error to
point — the other way round merely annoys somebody.

THE FACT WAS ALREADY BEING COMPUTED. `_live_technologies` reads the resource
ledger and answers which of a request's technologies still have an active
resource. It was called a few lines further down, to stop a second teardown of
something already gone. It is now asked first, and the status check is gone.

Nothing here reaches an orchestrator or a cloud.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from api import validation
from db.models import (Approval, Blueprint, ProvisionedResource, Request,
                       RequestComponent)
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
    s.add_all([
        Blueprint(technology_code="nginx", deployment_target="oci",
                  blueprint_ref="oci/service-vm", status="certified",
                  resource_kind="oci-service-vm"),
        Blueprint(technology_code="apache", deployment_target="oci",
                  blueprint_ref="oci/apache-httpd", status="certified",
                  resource_kind="oci-apache"),
    ])
    s.commit()
    yield s
    s.close()


_N = [0]


def source(db, status, *, techs=("nginx",), resources=(("oci-service-vm", "active"),)):
    """A request that reached `status`, with the resources it really has."""
    _N[0] += 1
    ref = f"REQ-2026-{200 + _N[0]:04d}"
    req = Request(reference=ref, requester="a@b.com", request_type="create",
                  status=status, deployment_target="oci", environment_name="test")
    req.components = [RequestComponent(technology_code=t, size="small") for t in techs]
    db.add(req)
    db.flush()
    db.add(Approval(request_id=req.id, jira_key=f"SDIMD-{_N[0]}", status="Approved"))
    for kind, state in resources:
        db.add(ProvisionedResource(reference=ref, kind=kind,
                                   name=f"{ref}-{kind}", details={},
                                   lifecycle_state=state))
    db.commit()
    return ref


def errors_for(db, ref, *techs):
    return validation.validate_submission(
        {"request_type": "decommission", "reference": "REQ-2026-9999",
         "source_reference": ref, "deployment_target": "oci",
         "components": [{"technology_code": t, "size": "small"} for t in techs]},
        db)


# --- the machine that could not be removed --------------------------------------

def test_a_verify_failed_request_with_a_live_machine_can_be_decommissioned(db):
    """THE test. REQ-2026-0232 exactly: built, failed verification, still
    running, still billing, and refused by the portal."""
    ref = source(db, "verify-failed")

    assert "source_reference" not in errors_for(db, ref, "nginx"), (
        "a running machine still cannot be decommissioned")


def test_a_teardown_failed_request_can_be_decommissioned(db):
    """The other way a request keeps resources it cannot shed: a decommission
    that itself failed. Refusing here means the retry is impossible."""
    ref = source(db, "teardown-failed")

    assert "source_reference" not in errors_for(db, ref, "nginx")


def test_an_apply_failed_request_with_a_live_resource_can_be_decommissioned(db):
    """A partial apply leaves real infrastructure behind."""
    ref = source(db, "apply-failed")

    assert "source_reference" not in errors_for(db, ref, "nginx")


# --- and nothing that should be refused becomes possible ------------------------

def test_a_provisioned_request_is_unchanged(db):
    """THE REGRESSION GUARD. The ordinary path must behave exactly as before."""
    ref = source(db, "provisioned")

    assert "source_reference" not in errors_for(db, ref, "nginx")


def test_a_request_whose_resources_are_all_gone_is_refused(db):
    """Already torn down. Asking again would hand the orchestrator a workspace
    that no longer exists."""
    ref = source(db, "decommissioned",
                 resources=(("oci-service-vm", "decommissioned"),))

    errors = errors_for(db, ref, "nginx")
    assert errors, "a request with nothing running was accepted"
    assert "Nothing is left to decommission" in errors.get("components", ""), errors


def test_a_request_that_never_provisioned_is_refused(db):
    """A draft or a rejected request has no resource ledger at all. That is NO
    OPINION, not evidence of absence, so the old status rule still decides —
    and `submitted` is still refused."""
    ref = source(db, "submitted", resources=())

    errors = errors_for(db, ref, "nginx")
    assert "source_reference" in errors
    assert "not currently provisioned" in errors["source_reference"]


def test_a_provisioned_request_with_no_ledger_is_still_allowed(db):
    """MOCK MODE, and the default posture of this whole project. Nothing records
    a resource, so requiring one would take decommissioning away from every
    demo — and from every request provisioned before the ledger existed."""
    ref = source(db, "provisioned", resources=())

    assert "source_reference" not in errors_for(db, ref, "nginx")


def test_the_refusal_still_says_what_the_status_is(db):
    """The status is no longer the RULE, but it is still the useful context for
    somebody wondering why their request is not offered."""
    ref = source(db, "submitted", resources=())

    assert "submitted" in errors_for(db, ref, "nginx")["source_reference"]


def test_an_unknown_reference_is_still_refused(db):
    assert "source_reference" in errors_for(db, "REQ-2026-0000", "nginx")


# --- a partial teardown still only offers what is left --------------------------

def test_only_the_technologies_still_live_can_be_selected(db):
    """After a partial decommission the rest must still be removable, and the
    part already gone must not be selectable again."""
    ref = source(db, "provisioned", techs=("nginx", "apache"),
                 resources=(("oci-service-vm", "active"),
                            ("oci-apache", "decommissioned")))

    assert "components" not in errors_for(db, ref, "nginx")
    assert "components" in errors_for(db, ref, "apache")


# --- and the picker asks the same question the validation does ------------------
#
# Both layers have to agree, or the form offers something the API then refuses —
# or worse, hides something the API would happily accept, which is the failure
# that left REQ-2026-0232's machine running with no way to remove it.

@pytest.fixture()
def client(db):
    from fastapi.testclient import TestClient

    from api.main import app, get_policy_evaluator, get_session

    def override_get_session():
        yield db

    app.dependency_overrides[get_session] = override_get_session
    app.dependency_overrides[get_policy_evaluator] = (
        lambda: (lambda data: {"allow": True, "violations": []}))
    yield TestClient(app)
    app.dependency_overrides.clear()


def _offered(client) -> set[str]:
    r = client.get("/api/requests?decommissionable=true")
    assert r.status_code == 200, r.text
    return {row["reference"] for row in r.json()}


def test_the_endpoint_offers_only_requests_with_something_running(db, client):
    """THE endpoint half. A request whose machine is gone must not be offered;
    one whose machine is running must be, whatever its status is called."""
    running = source(db, "verify-failed")
    gone = source(db, "decommissioned",
                  resources=(("oci-service-vm", "decommissioned"),))
    never = source(db, "submitted", resources=())

    offered = _offered(client)

    assert running in offered, "a running machine is still not offered"
    assert gone not in offered, "a torn-down request is offered"
    assert never not in offered, "a request that never built anything is offered"


def test_the_endpoint_still_offers_ordinary_provisioned_requests(db, client):
    """THE REGRESSION GUARD for the endpoint. The everyday case must be
    unaffected."""
    ordinary = source(db, "provisioned")

    assert ordinary in _offered(client)


def test_without_the_flag_nothing_is_filtered_out(db, client):
    """The new filter must be opt-in: every other caller of this endpoint — My
    requests, the overview drill-down — sees exactly what it did before."""
    gone = source(db, "decommissioned",
                  resources=(("oci-service-vm", "decommissioned"),))

    r = client.get("/api/requests")
    assert r.status_code == 200, r.text
    assert gone in {row["reference"] for row in r.json()}


def test_the_status_filter_still_works_alongside_it(db, client):
    """Both filters compose; neither replaces the other."""
    source(db, "verify-failed")
    ordinary = source(db, "provisioned")

    r = client.get("/api/requests?decommissionable=true&status=provisioned")
    assert r.status_code == 200, r.text
    refs = {row["reference"] for row in r.json()}
    assert refs == {ordinary}, refs


def test_the_endpoint_still_offers_a_provisioned_request_with_no_ledger(db, client):
    """MOCK MODE at the endpoint, and the case the first version of these tests
    missed: `test_the_endpoint_still_offers_ordinary_provisioned_requests` uses a
    request that HAS an active resource, so it is offered by the first clause and
    proves nothing about the fallback. Removing the fallback broke nothing — and
    would have emptied the picker for the whole demo path."""
    no_ledger = source(db, "provisioned", resources=())

    assert no_ledger in _offered(client)


def test_the_endpoint_does_not_offer_an_unprovisioned_request_with_no_ledger(
        db, client):
    """The fallback must stay tied to the status. Without that it would offer
    every draft in the system."""
    draft = source(db, "submitted", resources=())

    assert draft not in _offered(client)
