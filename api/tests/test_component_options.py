"""The component detail form: version + explicit shape per component.

The form lets a requester type a machine into existence, so the tests that matter
are the ones proving the SERVER decides what is allowed and that the chosen
numbers survive all the way to what gets built. A form that collects detail the
rest of the system ignores is worse than no form: it produces a request, a price
and an approval that describe a machine nobody will build.
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from api import component_options
from api.main import app, get_session
from api.tests.test_requests import VALID_CREATE
from db.seed import seed
from db.session import Base


@pytest.fixture()
def db():
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
    yield session
    session.close()


@pytest.fixture()
def client(db):
    def override_get_session():
        yield db

    app.dependency_overrides[get_session] = override_get_session
    yield TestClient(app)
    app.dependency_overrides.clear()


# --- What the form is offered ------------------------------------------------

def test_the_numeric_options_come_from_the_sizing_anchors(db):
    """All 46 technologies get a working form with no extra catalogue data: the
    offered vCPU values are the values the anchors already define (F-CAT-07)."""
    fields = component_options.options_for(db, "nginx", "oci")["fields"]
    assert [o["value"] for o in fields["vcpu"]["options"]] == ["2", "4", "8", "16"]
    assert [o["value"] for o in fields["memory_gb"]["options"]] == ["4", "16", "64", "128"]
    assert [o["value"] for o in fields["storage_gb"]["options"]] == ["50", "200", "500", "1000"]


def test_presets_come_from_the_server_not_the_browser(db):
    """The size buttons used to fill in a table hard-coded in the browser, which
    could show one shape while the server priced another."""
    assert component_options.presets(db, "nginx")["medium"] == {
        "vcpu": 4, "memory_gb": 16, "storage_gb": 200}


def test_a_technology_with_known_versions_offers_them(db):
    versions = component_options.options_for(db, "nginx")["fields"]["version"]
    assert [o["value"] for o in versions["options"]] == ["1.20", "1.22", "1.24"]
    assert versions["default"] == "1.20"


def test_a_technology_with_no_known_versions_offers_no_version_field(db):
    """THE honesty guard for this form.

    We cannot install a chosen version of Oracle Autonomous Database, so the form
    must not ask. An empty dropdown, or one filled with plausible-looking version
    numbers, would collect an answer the platform silently discards — which is
    how a catalogue entry named "Redis 7" came to deliver Redis 6.2.
    """
    fields = component_options.options_for(db, "oci-adb")["fields"]
    assert "version" not in fields
    # ...and the shape is still offered, so the component is still configurable.
    assert "vcpu" in fields


def test_the_endpoint_serves_what_the_form_needs(client):
    body = client.get("/api/catalogue/component-options",
                      params={"technology": "redis7", "target": "oci"}).json()
    assert body["technology_name"] == "Redis 7"
    assert [o["value"] for o in body["fields"]["version"]["options"]] == ["7"]
    assert body["presets"]["large"]["vcpu"] == 8


# --- The server, not the browser, decides what is allowed --------------------

def test_an_offered_value_is_accepted(db):
    assert component_options.validate(db, "nginx", "oci", {
        "version": "1.24", "vcpu": "8", "memory_gb": "64", "storage_gb": "500"}) == {}


def test_a_value_the_form_never_offered_is_refused(db):
    """The point of the module. A browser can send anything; 256 vCPU is not on
    any dropdown and must not become a machine."""
    errors = component_options.validate(db, "nginx", "oci", {"vcpu": "256"})
    assert "vcpu" in errors
    assert "not offered" in errors["vcpu"]
    # The message names the compliant options rather than just refusing (F-UX-10).
    assert "2, 4, 8, 16" in errors["vcpu"]


def test_a_version_that_cannot_be_installed_is_refused(db):
    """nginx 1.99 does not exist as a module stream. Accepting it would build a
    machine running something other than what was approved."""
    assert "version" in component_options.validate(db, "nginx", "oci", {"version": "1.99"})


def test_a_version_for_a_technology_that_offers_none_is_refused(db):
    errors = component_options.validate(db, "oci-adb", "oci", {"version": "19c"})
    assert "version" in errors
    assert "cannot be chosen" in errors["version"]


def test_unset_fields_are_fine(db):
    """A draft mid-way through the form, and every request raised before the form
    existed, carries no detail values and must still submit."""
    assert component_options.validate(db, "nginx", "oci", {}) == {}
    assert component_options.validate(db, "nginx", "oci",
                                      {"version": "", "vcpu": None}) == {}


def _draft(client, component: dict) -> str:
    """A valid create request carrying one component, saved as a draft."""
    return client.post("/api/requests/draft", json={
        **VALID_CREATE, "deployment_target": "oci", "components": [component],
    }).json()["reference"]


def test_submitting_an_unoffered_shape_is_rejected_by_the_api(client):
    """End to end through validation, because the guard above is only worth
    anything if the submit path actually calls it."""
    ref = _draft(client, {"technology_code": "nginx", "size": "medium", "vcpu": 999})
    resp = client.post(f"/api/requests/{ref}/submit")
    assert resp.status_code == 422
    assert "component_0_vcpu" in resp.json()["errors"]


def test_submitting_an_offered_shape_is_accepted_by_the_api(client):
    """The other half: a guard that refused everything would also pass the test
    above."""
    ref = _draft(client, {"technology_code": "nginx", "size": "medium", "version": "1.24",
                          "vcpu": 8, "memory_gb": 64, "storage_gb": 500})
    assert client.post(f"/api/requests/{ref}/submit").status_code == 200


def test_the_stored_request_keeps_the_chosen_detail(client):
    """It has to survive the round trip, or the approver and the orchestrator
    both see a request without the detail that was collected."""
    ref = _draft(client, {"technology_code": "nginx", "size": "medium", "version": "1.24",
                          "vcpu": 8, "memory_gb": 64, "storage_gb": 500})
    stored = client.get(f"/api/requests/{ref}").json()["components"][0]
    assert stored["version"] == "1.24"
    assert (stored["vcpu"], stored["memory_gb"], stored["storage_gb"]) == (8, 64, 500)


# --- The chosen numbers reach sizing, pricing and the machine ----------------

def test_an_explicit_shape_overrides_the_size_anchor(client):
    body = client.post("/api/sizing", json={"components": [
        {"technology_code": "nginx", "size": "medium", "vcpu": 8, "memory_gb": 64,
         "storage_gb": 500},
    ]}).json()
    row = body["components"][0]
    assert (row["vcpu"], row["memory_gb"], row["storage_gb"]) == (8, 64, 500)
    assert row["custom_shape"] is False, "8/64/500 is exactly the 'large' anchor"
    assert body["totals"] == {"vcpu": 8, "memory_gb": 64, "storage_gb": 500}


def test_a_shape_off_the_anchors_is_flagged_custom(client):
    """Not blocked — surfaced. The approver should see a non-standard machine
    before approving it rather than discover it afterwards (F-CAT-11)."""
    row = client.post("/api/sizing", json={"components": [
        {"technology_code": "nginx", "size": "medium", "vcpu": 16, "memory_gb": 4,
         "storage_gb": 50},
    ]}).json()["components"][0]
    assert row["custom_shape"] is True


def test_no_explicit_values_behaves_exactly_as_before(client):
    """The regression guard for every request that predates this form."""
    row = client.post("/api/sizing", json={"components": [
        {"technology_code": "postgres16", "size": "medium"},
    ]}).json()["components"][0]
    assert (row["vcpu"], row["memory_gb"], row["storage_gb"]) == (4, 16, 200)
    assert row["custom_shape"] is False


def test_the_price_follows_the_chosen_shape_not_the_size(client):
    """Otherwise the approver signs off the price of a machine nobody is building."""
    def monthly(component):
        return client.post("/api/cost", json={
            "deployment_target": "oci", "components": [component],
        }).json()["totals"]["monthly"]

    medium = {"technology_code": "nginx", "size": "medium"}
    bigger = {**medium, "vcpu": 16, "memory_gb": 128, "storage_gb": 1000}
    assert monthly(bigger) > monthly(medium)
