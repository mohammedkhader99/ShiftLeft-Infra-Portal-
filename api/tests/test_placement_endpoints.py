"""The API decides placement; the browser only asks (P.9).

ARCHITECTURE.md §14 decision 10: the API is the authority and the BFF filters for
display only. This API listens on its own port, so anything checked only in the
BFF is bypassable by whoever can reach 8081 — which is everyone inside the
network. These tests therefore attack the endpoint directly, the way something
that has bypassed the BFF would.

The property under test is not "the right options come back". It is that a
request body cannot describe a placement into existence: the option key selects
among layouts the SERVER computed, and an option the server refused cannot be
chosen however the body is written.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import api.main as main
from api.main import app, get_placement_evaluator, get_session
from api.placement_store import current_placement, placement_history
from db.models import AuditLog, Request, RequestComponent
from db.seed import seed
from db.session import Base


def allow_everything(_topology):
    return {"allow": True, "violations": []}


def refuse_consolidation(topology):
    """The real prod rule, in miniature: one host with more than one component
    carrying a database is refused."""
    for host in topology.get("hosts", []):
        if len(host.get("components", [])) > 1 and "postgres16" in host["components"]:
            return {"allow": False,
                    "violations": ["postgres16 may not share a host in prod. "
                                   "A production database shares a host with "
                                   "nothing else."]}
    return {"allow": True, "violations": []}


@pytest.fixture()
def db():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True,
                           connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    TestSession = sessionmaker(bind=engine, expire_on_commit=False)
    with TestSession() as session:
        seed(session)
        req = Request(reference="REQ-2026-9001", request_type="create",
                      requester="tester@example.com", deployment_target="oci",
                      environment_tier="prod", project_code="EGATE",
                      cost_centre_code="IMD-1001", environment_name="egate-prod")
        session.add(req)
        session.flush()
        for code, size in (("compute-vm", "small"), ("postgres16", "medium"),
                           ("nodejs20", "small")):
            session.add(RequestComponent(request_id=req.id, technology_code=code,
                                         size=size))
        session.commit()
        yield session


@pytest.fixture()
def client(db):
    app.dependency_overrides[get_session] = lambda: db
    app.dependency_overrides[get_placement_evaluator] = lambda: allow_everything
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def options(client, reference="REQ-2026-9001"):
    r = client.post("/api/placement/options", json={"reference": reference})
    assert r.status_code == 200, r.text
    return r.json()["options"]


# --- what the options endpoint returns ---------------------------------------

def test_the_three_layouts_come_back_with_costs(client):
    got = options(client)
    assert [o["key"] for o in got] == ["managed", "consolidated", "separated"]
    assert all(o["estimate"]["totals"]["monthly"] > 0 for o in got)
    assert all(o["sizing"]["machine_count"] >= 0 for o in got)


def test_the_cheapest_option_is_marked(client):
    assert any(o["cheapest"] for o in options(client))


def test_asking_for_options_persists_nothing(client, db):
    """A requester must be able to explore without committing to anything."""
    options(client)
    assert placement_history(db, 1) == []


def test_an_unknown_request_is_404(client):
    r = client.post("/api/placement/options", json={"reference": "REQ-NOPE"})
    assert r.status_code == 404


# --- a refused option is returned, with its reason ---------------------------

def test_a_refused_option_comes_back_with_the_policy_sentence(db):
    app.dependency_overrides[get_session] = lambda: db
    app.dependency_overrides[get_placement_evaluator] = lambda: refuse_consolidation
    with TestClient(app) as c:
        got = options(c)
    app.dependency_overrides.clear()

    consolidated = next(o for o in got if o["key"] == "consolidated")
    assert consolidated["eligible"] is False
    assert "may not share a host in prod" in consolidated["reasons"][0]
    # and the others survive: one layout is refused, not the request
    assert any(o["eligible"] for o in got)


# --- the body is an assertion, never a fact ----------------------------------

def test_choosing_an_option_the_server_did_not_produce_is_refused(client):
    r = client.post("/api/placement/resolve",
                    json={"reference": "REQ-2026-9001",
                          "option_key": "whatever-i-like"})
    assert r.status_code == 400
    assert "not a placement option" in r.json()["detail"]


def test_choosing_an_option_the_policy_refused_is_refused(db):
    """The heart of it. The browser may well have shown this option before the
    policy changed; the API decides again, at the moment it matters."""
    app.dependency_overrides[get_session] = lambda: db
    app.dependency_overrides[get_placement_evaluator] = lambda: refuse_consolidation
    with TestClient(app) as c:
        r = c.post("/api/placement/resolve",
                   json={"reference": "REQ-2026-9001",
                         "option_key": "consolidated"})
    app.dependency_overrides.clear()

    assert r.status_code == 409
    assert "may not share a host in prod" in r.json()["detail"]


def test_a_refused_choice_writes_no_placement(db):
    app.dependency_overrides[get_session] = lambda: db
    app.dependency_overrides[get_placement_evaluator] = lambda: refuse_consolidation
    with TestClient(app) as c:
        c.post("/api/placement/resolve",
               json={"reference": "REQ-2026-9001", "option_key": "consolidated"})
    app.dependency_overrides.clear()

    assert current_placement(db, 1) is None


# --- recording a decision -----------------------------------------------------

def test_resolving_records_a_versioned_placement(client, db):
    r = client.post("/api/placement/resolve",
                    json={"reference": "REQ-2026-9001", "option_key": "separated"})
    assert r.status_code == 200, r.text
    body = r.json()

    assert body["version"] == 1
    assert body["option_key"] == "separated"
    assert body["topology"]["hosts"]
    assert body["estimate"]["totals"]["monthly"] > 0

    stored = current_placement(db, 1)
    assert stored.option_key == "separated"


def test_the_stored_estimate_is_the_one_that_was_returned(client, db):
    """What reaches Jira must be the post-placement figure, and it must be the
    figure that was actually shown."""
    shown = client.post("/api/placement/resolve",
                        json={"reference": "REQ-2026-9001",
                              "option_key": "separated"}).json()
    stored = current_placement(db, 1)

    assert stored.estimate["totals"]["monthly"] == shown["estimate"]["totals"]["monthly"]


def test_changing_the_placement_supersedes_rather_than_overwrites(client, db):
    client.post("/api/placement/resolve",
                json={"reference": "REQ-2026-9001", "option_key": "separated"})
    client.post("/api/placement/resolve",
                json={"reference": "REQ-2026-9001", "option_key": "managed"})

    history = placement_history(db, 1)
    assert [p.version for p in history] == [1, 2]
    assert history[0].option_key == "separated"
    assert history[0].superseded_at is not None
    assert current_placement(db, 1).option_key == "managed"


def test_the_decision_is_audited(client, db):
    client.post("/api/placement/resolve",
                json={"reference": "REQ-2026-9001", "option_key": "separated"})

    entry = db.scalar(select(AuditLog).where(AuditLog.event == "placement.resolved"))
    assert entry is not None
    assert entry.reference == "REQ-2026-9001"
    assert entry.detail["option"] == "separated"
    assert entry.detail["monthly"] > 0


# --- catalogue facts, not guesses ---------------------------------------------

def test_host_modes_come_from_the_catalogue_not_the_component_code(db):
    facts = {f.code: f for f in main._component_facts(db, db.scalar(select(Request)))}

    assert facts["postgres16"].host_modes == {"vm", "container", "managed"}
    assert facts["nodejs20"].host_modes == {"vm", "container"}
    # The VM is the host its neighbours land on, not a workload on someone else's.
    assert facts["compute-vm"].is_host is True


def test_a_component_with_no_recorded_host_mode_is_placed_nowhere(db):
    """Better an option that does not appear than a machine built on a guess."""
    req = db.scalar(select(Request))
    db.add(RequestComponent(request_id=req.id, technology_code="mystery",
                            size="small"))
    db.flush()
    facts = {f.code: f for f in main._component_facts(db, req)}

    assert facts["mystery"].host_modes == frozenset()
