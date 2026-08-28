"""The registry records what was destroyed, not what we asked to destroy.

REQ-2026-0226 decommissioned REQ-2026-0225 and left it reading `provisioned`,
showing a machine that no longer existed at 790.59 AED a month in My
Environments and in the cost report.

TWO INDEPENDENT DERIVATIONS OF ONE FACT, and they disagreed:

    portal asked for   ['oci-bucket']       the catalogue default that 41 of
                                            46 technologies still carry
    workspace on disk  'oci-service-vm'
    orchestrator did   'oci-service-vm: Destroy complete! Resources: 3 destroyed.'

A FULL teardown deliberately sweeps up workspaces the portal did not name — so
that a stack whose kinds changed since it was built does not leave the old
resource running and billing. That sweep is why the right machine went. But the
portal then marked its ledger from its OWN list, matched nothing, found a row
still active, and left the source provisioned.

The executing layer is the authority on what it executed. It now says so as
DATA rather than as prose inside `summary`, and the portal believes it.

Nothing here reaches an orchestrator or a machine.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import api.main as main
from db.models import (Approval, AuditLog, Blueprint, ProvisionedResource,
                       Request, RequestComponent)
from db.seed import seed
from db.session import Base


class _Resp:
    """What `_post_to_orchestrator` hands back."""

    status_code = 200

    def __init__(self, body):
        self._body = body
        self.text = str(body)

    def json(self):
        return self._body


@pytest.fixture()
def db():
    engine = create_engine("sqlite+pysqlite:///:memory:",
                           connect_args={"check_same_thread": False},
                           poolclass=StaticPool, future=True)
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine, expire_on_commit=False)()
    seed(s)

    # mssql with NO certified blueprint, so `_environment_resource_kinds` falls
    # back to the catalogue default — which is how the wrong list arose.
    source = Request(reference="REQ-2026-0225", requester="a@b.com",
                     request_type="create", status="provisioned",
                     deployment_target="oci", environment_name="test")
    source.components = [RequestComponent(technology_code="mssql", size="small")]
    s.add(source)
    s.flush()
    s.add(Approval(request_id=source.id, jira_key="SDIMD-81178", status="Approved"))

    # What was actually built: a service VM, not a bucket.
    s.add(ProvisionedResource(reference="REQ-2026-0225", kind="oci-service-vm",
                              name="test-req-2026-0225-service-vm",
                              details={}, lifecycle_state="active"))
    s.commit()
    yield s
    s.close()


@pytest.fixture()
def quiet(monkeypatch):
    """The Jira and comment seams; this file is about the ledger."""
    monkeypatch.setattr(main, "_transition_jira", lambda *a, **k: None)
    monkeypatch.setattr(main, "add_comment", lambda *a, **k: None)


def _decommission_request(db):
    req = Request(reference="REQ-2026-0226", requester="a@b.com",
                  request_type="decommission", status="approved",
                  source_reference="REQ-2026-0225", deployment_target="oci")
    req.components = [RequestComponent(technology_code="mssql", size="small")]
    db.add(req)
    db.flush()
    db.add(Approval(request_id=req.id, jira_key="SDIMD-81179", status="Approved"))
    db.commit()
    return req


def _run(db, monkeypatch, body):
    req = _decommission_request(db)
    monkeypatch.setattr(main, "_post_to_orchestrator",
                        lambda *a, **k: (_Resp(body), None))
    result = main._decommission(db, req, actor="approver")
    db.commit()
    return req, result


def _resource(db):
    return db.scalar(select(ProvisionedResource).where(
        ProvisionedResource.reference == "REQ-2026-0225"))


def _source(db):
    return db.scalar(select(Request).where(Request.reference == "REQ-2026-0225"))


DESTROYED = {"destroyed": True, "reference": "REQ-2026-0225",
             "kinds": ["oci-service-vm"],
             "summary": "oci-service-vm: Destroy complete! Resources: 3 destroyed."}


# --- what the portal asked for was wrong, and it no longer matters ------------

def test_the_portal_asks_for_the_wrong_kind(db):
    """Not a hypothetical. With no certified blueprint the kind list falls back
    to `Technology.resource_kind`, and for mssql that is `oci-bucket` — as it is
    for 41 of the 46 technologies in the catalogue."""
    req = _decommission_request(db)

    assert main._environment_resource_kinds(db, req) == ["oci-bucket"]


def test_the_resource_that_was_destroyed_is_marked_destroyed(db, monkeypatch, quiet):
    """THE test. The row is `oci-service-vm`; the portal asked about
    `oci-bucket`; the orchestrator reports what it actually destroyed."""
    _run(db, monkeypatch, DESTROYED)

    assert _resource(db).lifecycle_state == "decommissioned", (
        "a destroyed machine is still recorded as active and billing")


def test_the_source_is_finished_when_nothing_of_it_is_left(db, monkeypatch, quiet):
    _run(db, monkeypatch, DESTROYED)

    assert _source(db).status == "decommissioned", (
        "the source still reads as a running environment")


def test_the_audit_records_both_lists_so_a_disagreement_is_visible(db, monkeypatch, quiet):
    """The two lists disagreeing is the defect. Recording only one of them is
    how it went unnoticed — the trail said `kinds: ['oci-bucket']` and the
    summary said a service VM was destroyed, and nothing compared them."""
    _run(db, monkeypatch, DESTROYED)

    entry = db.scalar(select(AuditLog).where(AuditLog.event == "decommissioned"))
    assert entry is not None
    assert entry.detail.get("asked") == ["oci-bucket"]
    assert entry.detail.get("destroyed") == ["oci-service-vm"]


# --- and the old behaviour is intact ------------------------------------------

def test_an_orchestrator_that_names_nothing_falls_back_to_what_we_asked(
        db, monkeypatch, quiet):
    """An older orchestrator returns no `kinds`. It must behave exactly as it
    did — which for this data means marking nothing, because that WAS the old
    behaviour and inventing a better one here would hide the upgrade."""
    _run(db, monkeypatch, {"destroyed": True, "summary": "done"})

    assert _resource(db).lifecycle_state == "active"
    assert _source(db).status == "provisioned"


def test_only_what_was_destroyed_is_marked(db, monkeypatch, quiet):
    """A partial teardown must still leave the rest running and the source
    provisioned. Believing the orchestrator must not become believing that
    everything went."""
    db.add(ProvisionedResource(reference="REQ-2026-0225", kind="oci-apache",
                               name="test-req-2026-0225-apache",
                               details={}, lifecycle_state="active"))
    db.commit()

    _run(db, monkeypatch, DESTROYED)

    kinds = {r.kind: r.lifecycle_state for r in db.scalars(
        select(ProvisionedResource).where(
            ProvisionedResource.reference == "REQ-2026-0225"))}
    assert kinds == {"oci-service-vm": "decommissioned", "oci-apache": "active"}
    assert _source(db).status == "provisioned", (
        "a partial teardown reported the whole environment as gone")


def test_a_failed_teardown_marks_nothing(db, monkeypatch, quiet):
    """The ledger must never move on a teardown that did not happen."""
    req = _decommission_request(db)
    monkeypatch.setattr(main, "_post_to_orchestrator", lambda *a, **k: (None, "boom"))

    main._decommission(db, req, actor="approver")
    db.commit()

    assert _resource(db).lifecycle_state == "active"
    assert _source(db).status == "provisioned"
    assert req.status == "teardown-failed"


def test_a_kinds_list_that_is_not_strings_is_ignored(db, monkeypatch, quiet):
    """It arrives over the wire from another service. A malformed value must
    fall back rather than reach a SQL IN clause."""
    _run(db, monkeypatch, {"destroyed": True, "kinds": [{"nope": 1}, None],
                           "summary": "done"})

    assert _resource(db).lifecycle_state == "active"
