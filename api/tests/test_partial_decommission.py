"""Decommissioning one component must not destroy the others.

Reported from use: REQ-2026-0129 named ONE component of a two-component stack and
both machines were torn down. The selection was collected, validated against the
source's stack, priced, and written on the Jira ticket — then discarded at the
last step, because the handoff was built from the SOURCE request (whose kind list
is everything it built) and the orchestrator destroyed every workspace on disk.

Three layers each lost the selection, so the tests check all three:

  * the handoff carries the selected kinds, not the source's;
  * the registry marks only what was actually removed, and leaves the source
    PROVISIONED while anything of it is still running; and
  * a component already torn down cannot be selected again.
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from api import validation
from api.main import _environment_resource_kinds, _mark_component_lifecycle
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
    # Apache and nginx are built by different blueprints, which is what makes a
    # partial teardown meaningful at all.
    s.add_all([
        Blueprint(technology_code="apache", deployment_target="oci",
                  blueprint_ref="oci/apache-httpd", status="certified",
                  resource_kind="oci-apache"),
        Blueprint(technology_code="nginx", deployment_target="oci",
                  blueprint_ref="oci/service-vm", status="certified",
                  resource_kind="oci-service-vm"),
    ])
    source = Request(reference="REQ-2026-0128", requester="a@b.com",
                     request_type="create", status="provisioned",
                     deployment_target="oci", environment_name="test121")
    source.components = [RequestComponent(technology_code="apache", size="small"),
                         RequestComponent(technology_code="nginx", size="small")]
    s.add(source)
    s.flush()
    # The SOURCE carries the approval the handoff is signed with — it is the
    # source's workspaces being destroyed, so it is the source's key.
    s.add(Approval(request_id=source.id, jira_key="SDIMD-76093", status="Approved"))
    s.add_all([
        ProvisionedResource(reference="REQ-2026-0128", kind="oci-apache",
                            name="test121-req-2026-0128-apache",
                            details={}, lifecycle_state="active"),
        ProvisionedResource(reference="REQ-2026-0128", kind="oci-service-vm",
                            name="test121-26-0128-service-vm",
                            details={}, lifecycle_state="active"),
    ])
    s.commit()
    yield s
    s.close()


def _decommission_request(db, *techs) -> Request:
    req = Request(reference="REQ-2026-0129", requester="a@b.com",
                  request_type="decommission", status="approved",
                  source_reference="REQ-2026-0128", deployment_target="oci")
    req.components = [RequestComponent(technology_code=t, size="small") for t in techs]
    db.add(req)
    db.commit()
    return req


# --- The handoff names only what was selected --------------------------------

def test_selecting_one_component_targets_only_its_resource(db):
    """THE bug. The kinds sent to the orchestrator come from the DECOMMISSION
    request's components, not from everything the source built."""
    req = _decommission_request(db, "apache")
    assert _environment_resource_kinds(db, req) == ["oci-apache"]


def test_the_source_still_knows_it_has_two(db):
    """The source is unchanged — it is the request being torn down that narrows."""
    source = db.get(Request, 1)
    assert _environment_resource_kinds(db, source) == ["oci-apache", "oci-service-vm"]


def test_selecting_every_component_targets_everything(db):
    req = _decommission_request(db, "apache", "nginx")
    assert _environment_resource_kinds(db, req) == ["oci-apache", "oci-service-vm"]


# --- A component already gone cannot be selected again -----------------------

def _torn_down(db, kind):
    res = db.scalars(select_resource(db, kind)).one()
    res.lifecycle_state = "decommissioned"
    db.commit()


def select_resource(db, kind):
    from sqlalchemy import select
    return select(ProvisionedResource).where(
        ProvisionedResource.reference == "REQ-2026-0128",
        ProvisionedResource.kind == kind)


def _errors(db, *techs):
    return validation.validate_submission(
        {"request_type": "decommission", "reference": "REQ-2026-0130",
         "source_reference": "REQ-2026-0128", "deployment_target": "oci",
         "components": [{"technology_code": t, "size": "small"} for t in techs]},
        db)


def test_a_decommissioned_component_is_refused(db):
    """Your second requirement: after a partial teardown, the same component must
    not be offered — or accepted — again."""
    _torn_down(db, "oci-apache")
    errors = _errors(db, "apache")
    assert "components" in errors
    assert "already been decommissioned" in errors["components"]
    assert "nginx" in errors["components"], "and say what IS still available"


def test_the_component_still_running_can_be_decommissioned(db):
    _torn_down(db, "oci-apache")
    assert _errors(db, "nginx") == {}


def test_both_selectable_while_both_are_alive(db):
    assert _errors(db, "apache", "nginx") == {}


def test_nothing_left_says_so(db):
    _torn_down(db, "oci-apache")
    _torn_down(db, "oci-service-vm")
    assert "Nothing is left" in _errors(db, "apache")["components"]


# --- The form is told which components are still alive -----------------------

def test_the_api_flags_which_components_are_still_running(db):
    """Drives the form: only live components appear in the decommission list."""
    from api.main import RequestOut
    _torn_down(db, "oci-apache")
    source = db.get(Request, 1)
    out = RequestOut.model_validate(source)
    _mark_component_lifecycle(db, source, out)
    flags = {c.technology_code: c.active for c in out.components}
    assert flags == {"apache": False, "nginx": True}


# --- The whole teardown, driven end to end -----------------------------------
#
# Everything above tests the pieces. Two of the four faults lived INSIDE
# _decommission — the handoff it builds and the rows it marks — and neither was
# caught until these ran the function itself. That is the third time today a
# correct helper sat behind wiring nothing exercised.

def _run_decommission(db, monkeypatch, *techs):
    """Call _decommission with the orchestrator and Jira stubbed, and return the
    payload it would have sent."""
    import json
    import api.main as main

    req = _decommission_request(db, *techs)
    db.add(Approval(request_id=req.id, jira_key="SDIMD-99999", status="Approved"))
    db.commit()

    sent: dict = {}

    class Resp:
        status_code = 200
        text = "{}"

        def json(self):
            return {"destroyed": True, "summary": "destroyed"}

    def fake_post(body, signature, path=""):
        sent.update(json.loads(body))
        return Resp(), None

    monkeypatch.setattr(main, "_post_to_orchestrator", fake_post)
    monkeypatch.setattr(main, "_transition_jira", lambda *a, **k: None)
    monkeypatch.setattr(main, "add_comment", lambda *a, **k: None)
    main._decommission(db, req, "tester@example.com")
    return sent


def test_the_handoff_names_only_the_selected_kinds(db, monkeypatch):
    """The original bug: the payload was built from the SOURCE, whose kind list
    is everything it built, so the orchestrator tore down both machines."""
    sent = _run_decommission(db, monkeypatch, "apache")
    assert sent["resource_kinds"] == ["oci-apache"]
    assert sent["partial_destroy"] is True
    # ...and it still targets the SOURCE, whose workspaces and names these are.
    assert sent["reference"] == "REQ-2026-0128"


def test_removing_everything_is_not_flagged_partial(db, monkeypatch):
    """A full teardown must keep sweeping untracked workspaces, so it must not
    claim to be partial."""
    sent = _run_decommission(db, monkeypatch, "apache", "nginx")
    assert sorted(sent["resource_kinds"]) == ["oci-apache", "oci-service-vm"]
    assert sent["partial_destroy"] is False


def test_only_the_removed_resource_is_marked_decommissioned(db, monkeypatch):
    """Marking every row dead told the portal a machine was gone while it was
    still running and still billing — the worst kind of registry error."""
    _run_decommission(db, monkeypatch, "apache")
    states = {r.kind: r.lifecycle_state for r in db.scalars(
        __import__("sqlalchemy").select(ProvisionedResource).where(
            ProvisionedResource.reference == "REQ-2026-0128"))}
    assert states == {"oci-apache": "decommissioned", "oci-service-vm": "active"}


def test_the_source_stays_provisioned_while_anything_remains(db, monkeypatch):
    """So it still appears in My Environments, can be decommissioned again for
    what is left, and still reports its real cost."""
    _run_decommission(db, monkeypatch, "apache")
    assert db.get(Request, 1).status == "provisioned"


def test_the_source_is_decommissioned_once_nothing_is_left(db, monkeypatch):
    _run_decommission(db, monkeypatch, "apache", "nginx")
    assert db.get(Request, 1).status == "decommissioned"


def test_a_draft_reports_unknown_rather_than_dead(db):
    """Nothing provisioned yet is not the same as everything torn down, and a
    form that treated them alike would offer nothing on a fresh request."""
    from api.main import RequestOut
    draft = Request(reference="REQ-2026-0200", requester="a@b.com",
                    request_type="create", status="draft", deployment_target="oci")
    draft.components = [RequestComponent(technology_code="nginx", size="small")]
    db.add(draft)
    db.commit()
    out = RequestOut.model_validate(draft)
    _mark_component_lifecycle(db, draft, out)
    assert out.components[0].active is None
