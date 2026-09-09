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


# --- Scenario B through the endpoint (P.5a) ----------------------------------

def _make_kubernetes_request(db, with_blueprint=True):
    """A request naming OKE.

    The blueprint row matters: it is what says OKE is a CLUSTER. Blueprints are
    registered by the catalogue flow rather than by the seed, so a test database
    has none until one is added — the live database does, which is why the
    Kubernetes options appear there and would not here.
    """
    from db.models import Blueprint, Request as R, RequestComponent as RC
    if with_blueprint:
        db.add(Blueprint(technology_code="oci-oke", deployment_target="oci",
                         blueprint_ref="oci-oke.yaml", resource_kind="oci-oke",
                         status="certified"))
    req = R(reference="REQ-2026-9002", request_type="create",
            requester="tester@example.com", deployment_target="oci",
            environment_tier="dev", project_code="EGATE",
            cost_centre_code="IMD-1001", environment_name="egate-k8s")
    db.add(req)
    db.flush()
    # PostgreSQL is in the selection deliberately: it is the case Scenario B
    # describes, where a stateful workload on a cluster must warn AND be shown
    # the managed alternative. Without it there is nothing to manage and the
    # managed option correctly does not appear.
    for code, size in (("oci-oke", "small"), ("postgres16", "medium"),
                       ("nodejs20", "small")):
        db.add(RC(request_id=req.id, technology_code=code, size=size))
    db.commit()
    return req


def test_selecting_oke_offers_the_cluster_options(client, db):
    _make_kubernetes_request(db)
    got = options(client, "REQ-2026-9002")

    assert [o["key"] for o in got] == ["existing-cluster", "new-cluster", "managed"]


def test_the_cluster_provider_is_read_from_the_blueprint(db):
    """Every OKE row in this database still carries the oci-bucket default, so
    reading Technology.resource_kind would classify a cluster as a bucket."""
    req = _make_kubernetes_request(db)
    facts = {f.code: f for f in main._component_facts(db, req)}

    assert facts["oci-oke"].provides_cluster is True
    assert facts["nodejs20"].provides_cluster is False


def test_the_refusal_reaching_the_browser_is_the_deployment_one(client, db):
    """It used to be the discovery gap, which was the truthful answer while
    deploying into a cluster was assumed possible. It is not: the Kubernetes API
    endpoint is private and the orchestrator has no route to it, so which clusters
    a requester may use does not decide anything yet.

    The end-to-end assertion, because the reason has to survive the API, the
    filter and the JSON — not merely be correct in the resolver.
    """
    _make_kubernetes_request(db)
    existing = next(o for o in options(client, "REQ-2026-9002")
                    if o["key"] == "existing-cluster")

    assert existing["eligible"] is False
    assert len(existing["reasons"]) == 1
    assert "cannot deploy workloads into" in existing["reasons"][0]
    assert "not a statement about your access" not in existing["reasons"][0]
    # And it says what does work instead.
    assert "Ask for the cluster on its own" in existing["reasons"][0]


def test_choosing_an_existing_cluster_without_naming_one_is_refused(client, db):
    _make_kubernetes_request(db)
    r = client.post("/api/placement/resolve",
                    json={"reference": "REQ-2026-9002",
                          "option_key": "existing-cluster"})
    # Refused because the option itself is ineligible while discovery is unbuilt.
    assert r.status_code == 409


def test_naming_a_cluster_on_an_option_that_does_not_use_one_is_refused(client, db):
    """Asserted against `managed`, because the cluster options are now refused
    outright and a resolve never reaches the cluster-id check on them — the 409
    would pass this test for the wrong reason."""
    _make_kubernetes_request(db)
    r = client.post("/api/placement/resolve",
                    json={"reference": "REQ-2026-9002",
                          "option_key": "managed",
                          "cluster_id": "ocid1.cluster.oc1..anything"})
    assert r.status_code == 400
    assert "does not deploy onto one" in r.json()["detail"]


def test_a_cluster_option_cannot_be_resolved_at_all(client, db):
    """Nothing deploys into a cluster, so neither option can be chosen however
    the request body is written — the same property P.9 defends for a
    policy-refused layout."""
    _make_kubernetes_request(db)
    for key in ("existing-cluster", "new-cluster"):
        r = client.post("/api/placement/resolve",
                        json={"reference": "REQ-2026-9002", "option_key": key})
        assert r.status_code == 409, f"{key}: {r.text}"
        assert "cannot deploy workloads into" in r.json()["detail"], key


def test_a_kubernetes_request_is_still_usable_without_placement(client, db):
    """THE PORTAL MUST REMAIN USABLE FOR KUBERNETES REQUESTS, and it is: placement
    is optional (P.11), so an OKE request that never opens the placement step
    provisions the cluster exactly as every one has to date. What is withdrawn is
    the claim that the portal would also deploy the workloads onto it.

    Asserted by submitting without resolving anything.
    """
    _make_kubernetes_request(db)
    r = client.post("/api/requests/REQ-2026-9002/submit")
    # Whatever the submission validation says, it must not be blocked by
    # placement — nothing was placed.
    assert r.status_code in (200, 422), r.text
    if r.status_code == 422:
        assert "placement" not in r.text.lower()



def test_without_a_blueprint_oke_is_not_recognised_as_a_cluster(db):
    """A known limitation, recorded rather than hidden.

    The blueprint is the only authority that says a component is a cluster —
    Technology.resource_kind is documented as wrong for most components, and
    every OKE row in the live database still carries the oci-bucket default. So
    a Kubernetes technology with no registered blueprint silently gets the
    machine-shaped options instead of the cluster ones.

    That cannot happen for anything a requester can actually order, because the
    certification gate will not offer a technology the platform has no blueprint
    for. This test exists so the behaviour is known if that ever stops being
    true.
    """
    req = _make_kubernetes_request(db, with_blueprint=False)
    facts = {f.code: f for f in main._component_facts(db, req)}

    assert facts["oci-oke"].provides_cluster is False


def test_a_database_on_the_cluster_warns_and_offers_the_managed_alternative(client, db):
    _make_kubernetes_request(db)
    got = options(client, "REQ-2026-9002")

    existing = next(o for o in got if o["key"] == "existing-cluster")
    assert any("postgres16 keeps data" in w for w in existing["warnings"])
    assert any("Managed where available" in w for w in existing["warnings"])
    # and the alternative the warning names is actually on the screen
    assert any(o["key"] == "managed" for o in got)


# --- a requester may change their mind (P.11) ---------------------------------
#
# Found by building the wizard step. The constraint that stops a follow-up
# re-hosting a built environment was being read from THIS request's own last
# placement, which made the first click in the wizard the only click. Comparing
# two options is the entire purpose of the screen.

def test_choosing_one_option_does_not_lock_out_the_others(client):
    """The regression. Resolve 'managed', then ask again: every layout must
    still be on offer, because nothing has been built."""
    before = {o["key"] for o in options(client) if o["eligible"]}
    assert {"managed", "consolidated", "separated"} <= before

    r = client.post("/api/placement/resolve",
                    json={"reference": "REQ-2026-9001", "option_key": "managed"})
    assert r.status_code == 200, r.text

    after = {o["key"] for o in options(client) if o["eligible"]}
    assert after == before, "resolving a placement narrowed the later choices"


def test_switching_back_and_forth_is_allowed_and_recorded(client, db):
    """Each change is a new version, and the history keeps all of them — the
    requester is free, and the record still says what was chosen when."""
    for key in ("managed", "consolidated", "managed", "separated"):
        r = client.post("/api/placement/resolve",
                        json={"reference": "REQ-2026-9001", "option_key": key})
        assert r.status_code == 200, f"{key}: {r.text}"

    history = placement_history(db, 1)
    assert [p.option_key for p in history] == ["managed", "consolidated",
                                               "managed", "separated"]
    assert [p.version for p in history] == [1, 2, 3, 4]
    assert current_placement(db, 1).option_key == "separated"


def test_a_provisioned_request_is_held_to_what_was_built(client, db):
    """The constraint still bites where it was meant to. Once machines exist,
    how the environment is hosted is a fact and not a preference.

    A database on its own, because that is the only way to get a placement made
    ENTIRELY of managed hosts. The three-component fixture's managed option also
    carries a VM for the runtime, so its topology legitimately permits both modes
    and nothing would be refused — which is correct behaviour and the wrong test.
    """
    req = Request(reference="REQ-2026-9003", request_type="create",
                  requester="tester@example.com", deployment_target="oci",
                  environment_tier="dev", project_code="EGATE",
                  cost_centre_code="IMD-1001", environment_name="egate-dev")
    db.add(req)
    db.flush()
    db.add(RequestComponent(request_id=req.id, technology_code="postgres16",
                            size="medium"))
    db.commit()

    keys = {o["key"] for o in options(client, "REQ-2026-9003")}
    assert keys == {"managed", "separated"}, keys

    r = client.post("/api/placement/resolve",
                    json={"reference": "REQ-2026-9003", "option_key": "managed"})
    assert r.status_code == 200, r.text
    req.status = "provisioned"
    db.commit()

    after = {o["key"]: o for o in options(client, "REQ-2026-9003")}
    separated = after["separated"]
    assert separated["eligible"] is False, (
        "a provisioned managed database still offered a VM layout")
    assert any("built on managed" in reason for reason in separated["reasons"])
    assert any("rebuild, not a resize" in reason for reason in separated["reasons"])
    # and it is refused, not removed — the requester can see why
    assert "separated" in after
