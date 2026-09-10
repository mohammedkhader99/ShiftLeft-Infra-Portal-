"""The browser may propose a layout; the server decides whether it exists (P.15).

Dragging a component from one machine to another is a real thing to want, and
P.9's rule — that a request body cannot describe a placement into existence —
was written before there was any way to want it. This endpoint is how both hold:
the ARRANGEMENT comes from the browser, and every fact about whether it is
buildable is re-derived here.

The security-relevant test in this file is
`test_a_layout_cannot_smuggle_in_a_component_nobody_asked_for`. An unchecked
proposal could place Oracle into a request for nginx, and everything downstream —
sizing, cost, the approval ticket, Terraform — would faithfully build what the
body said. A layout may rearrange what was requested. It may never change it.

The rest are about being useful: a refused layout has to say what is wrong with
it, in terms of the thing the requester just did, so the diagram can put the
message next to the component they moved.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import api.main as main
from db.models import Blueprint
from db.seed import seed
from db.session import Base

COMPONENTS = [
    {"technology_code": "compute-vm", "size": "small"},
    {"technology_code": "postgres16", "size": "medium"},
    {"technology_code": "nodejs20", "size": "small"},
]
DRAFT = {
    "request_type": "create", "project_code": "EGATE",
    "cost_centre_code": "IMD-1001", "deployment_target": "oci",
    "environment_name": "egate-drag", "environment_tier": "dev",
    "data_classification": "internal", "components": COMPONENTS,
    "business_justification": "Checking a layout the requester arranged.",
    "priority": "high", "business_criticality": "tier2",
    "required_delivery_date": (date.today() + timedelta(days=30)).isoformat(),
    "application_owner": "app.owner@emaratechg.ae",
    "technical_owner": "tech.owner@emaratechg.ae",
}


def allow_everything(_topology):
    return {"allow": True, "violations": []}


def refuse_databases_sharing(topology):
    """The real prod rule in miniature, so a policy refusal can be told apart
    from a malformed proposal."""
    for host in topology.get("hosts", []):
        if len(host.get("components", [])) > 1 and "postgres16" in host["components"]:
            return {"allow": False,
                    "violations": ["postgres16 may not share a host in prod."]}
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
        for code in ("compute-vm", "postgres16", "nodejs20"):
            session.add(Blueprint(technology_code=code, deployment_target="oci",
                                  status="certified", resource_kind="oci-instance",
                                  blueprint_ref=f"oci/{code}"))
        session.commit()
        yield session


@pytest.fixture()
def client(db):
    main.app.dependency_overrides[main.get_session] = lambda: db
    main.app.dependency_overrides[main.get_placement_evaluator] = lambda: allow_everything
    with TestClient(main.app) as c:
        yield c
    main.app.dependency_overrides.clear()


@pytest.fixture()
def reference(client):
    r = client.post("/api/requests/draft", json=DRAFT)
    assert r.status_code == 200, r.text
    return r.json()["reference"]


def evaluate(client, reference, hosts):
    r = client.post("/api/placement/evaluate",
                    json={"reference": reference, "hosts": hosts})
    assert r.status_code == 200, r.text
    return r.json()


# The two arrangements the requester can reach by dragging: everything together,
# or a machine each.
TOGETHER = [{"id": "host-1", "host_mode": "vm",
             "components": ["postgres16", "nodejs20"]}]
APART = [{"id": "host-1", "host_mode": "vm", "components": ["postgres16"]},
         {"id": "host-2", "host_mode": "vm", "components": ["nodejs20"]}]


# --- a layout the requester arranged is judged and priced ---------------------

def test_a_valid_layout_comes_back_priced(client, reference):
    answer = evaluate(client, reference, TOGETHER)

    assert answer["eligible"] is True
    assert answer["reasons"] == []
    assert answer["resolved"] is True
    assert answer["totals"]["monthly"] > 0


def test_the_shape_is_the_sum_of_what_shares_the_machine(client, reference):
    """The same arithmetic the enumerated options get — 4+2 vCPU and 16+4 GB,
    plus 20% headroom rounded up. A custom layout priced by a different rule
    would be cheaper on screen than in the bill."""
    host = evaluate(client, reference, TOGETHER)["sizing"]["hosts"][0]

    assert (host["vcpu"], host["memory_gb"], host["storage_gb"]) == (8, 24, 300)


def test_moving_them_apart_changes_the_price(client, reference):
    """The whole point of letting someone rearrange it."""
    together = evaluate(client, reference, TOGETHER)
    apart = evaluate(client, reference, APART)

    assert together["sizing"]["machine_count"] == 1
    assert apart["sizing"]["machine_count"] == 2
    assert together["totals"]["monthly"] != apart["totals"]["monthly"]


def test_it_says_what_the_platform_would_have_charged(client, reference):
    """A requester rearranging a topology is trading something for something.
    Without the comparison they are only trading."""
    answer = evaluate(client, reference, APART)

    assert answer["cheapest_offered"] > 0
    assert answer["monthly_delta"] == pytest.approx(
        round(answer["totals"]["monthly"] - answer["cheapest_offered"], 2))


def test_nothing_is_persisted(client, reference, db):
    """Read-only by construction. A layout is recorded by /resolve, which
    re-derives it again at the moment it matters."""
    from api.placement_store import placement_history
    from db.models import Request
    from sqlalchemy import select

    evaluate(client, reference, TOGETHER)
    req = db.scalar(select(Request).where(Request.reference == reference))
    assert placement_history(db, req.id) == []


# --- the security property ----------------------------------------------------

def test_a_layout_cannot_smuggle_in_a_component_nobody_asked_for(client, reference):
    """THE ONE THAT MATTERS. Everything downstream would faithfully build what
    the body said — sizing, cost, the ticket, Terraform. A layout may rearrange
    what was requested; it may never change it."""
    answer = evaluate(client, reference, [
        {"id": "host-1", "host_mode": "vm",
         "components": ["postgres16", "nodejs20", "oracle-db"]}])

    assert answer["eligible"] is False
    assert any("oracle-db" in r for r in answer["reasons"])
    assert any("did not ask for" in r for r in answer["reasons"])


def test_a_layout_cannot_quietly_drop_a_component(client, reference):
    """The other direction, and quieter. A requester who asked for a database
    and gets an environment without one has been under-served silently."""
    answer = evaluate(client, reference, [
        {"id": "host-1", "host_mode": "vm", "components": ["nodejs20"]}])

    assert answer["eligible"] is False
    assert any("postgres16" in r and "needs somewhere to run" in r
               for r in answer["reasons"])


def test_a_component_cannot_be_built_twice(client, reference):
    answer = evaluate(client, reference, [
        {"id": "host-1", "host_mode": "vm", "components": ["postgres16", "nodejs20"]},
        {"id": "host-2", "host_mode": "vm", "components": ["postgres16"]}])

    assert answer["eligible"] is False
    assert any("more than one host" in r for r in answer["reasons"])


def test_a_machine_is_not_something_to_place(client, reference):
    """compute-vm IS the host. Requiring it to be placed on a host would ask the
    requester to put a machine on a machine."""
    answer = evaluate(client, reference, TOGETHER)
    assert answer["eligible"] is True, answer["reasons"]


# --- refusals a dragging requester will actually hit --------------------------

def test_dropping_a_component_where_it_cannot_run_is_refused(client, reference):
    """The defect that produced the screen this feature was asked for: Apache and
    SQL Server are `vm` in this catalogue, and putting one on a cluster used to
    be reported as a missing sizing row rather than as an impossible layout."""
    answer = evaluate(client, reference, [
        {"id": "cluster-1", "host_mode": "container", "components": ["postgres16"]},
        {"id": "host-1", "host_mode": "vm", "components": ["nodejs20"]}])

    # postgres16 DOES offer container, so this one is allowed — the refusal is
    # asserted against a component that does not, below.
    assert answer["eligible"] is True, answer["reasons"]


def test_a_vm_only_component_on_a_cluster_names_what_it_can_be(client, reference, db):
    from db.models import TechnologyHostMode
    from sqlalchemy import delete

    # Make postgres16 vm-only for this request, the way Apache and SQL Server are.
    db.execute(delete(TechnologyHostMode).where(
        TechnologyHostMode.technology_code == "postgres16",
        TechnologyHostMode.host_mode == "container"))
    db.commit()

    answer = evaluate(client, reference, [
        {"id": "cluster-1", "host_mode": "container", "components": ["postgres16"]},
        {"id": "host-1", "host_mode": "vm", "components": ["nodejs20"]}])

    assert answer["eligible"] is False
    assert any("cannot run as container" in r for r in answer["reasons"])
    assert any("offers it as" in r for r in answer["reasons"]), (
        "a refusal that does not say what it CAN be leaves the requester guessing")


def test_an_empty_host_is_refused(client, reference):
    """A machine nobody asked for that would still be built and billed."""
    answer = evaluate(client, reference, [
        {"id": "host-1", "host_mode": "vm", "components": ["postgres16", "nodejs20"]},
        {"id": "host-2", "host_mode": "vm", "components": []}])

    assert answer["eligible"] is False
    assert any("carries nothing" in r for r in answer["reasons"])


def test_two_hosts_cannot_share_an_id(client, reference):
    """They would be indistinguishable in the topology and in the Terraform
    state, which is where it stops being a display problem."""
    answer = evaluate(client, reference, [
        {"id": "host-1", "host_mode": "vm", "components": ["postgres16"]},
        {"id": "host-1", "host_mode": "vm", "components": ["nodejs20"]}])

    assert answer["eligible"] is False
    assert any("share the id" in r for r in answer["reasons"])


def test_an_unknown_host_mode_is_refused(client, reference):
    answer = evaluate(client, reference, [
        {"id": "host-1", "host_mode": "bare-metal",
         "components": ["postgres16", "nodejs20"]}])

    assert answer["eligible"] is False
    assert any("not a host mode" in r for r in answer["reasons"])


def test_every_complaint_comes_back_not_just_the_first(client, reference):
    """A requester who moves two things and is told about one of them fixes it,
    resubmits, and is told about the other."""
    answer = evaluate(client, reference, [
        {"id": "host-1", "host_mode": "vm", "components": ["oracle-db"]},
        {"id": "host-1", "host_mode": "vm", "components": []}])

    assert len(answer["reasons"]) >= 3, answer["reasons"]


# --- policy still owns what policy owns ---------------------------------------

def test_a_policy_refusal_reaches_the_requester(client, reference):
    """Co-residency is OPA's, not this endpoint's. A custom layout gets the same
    evaluation an enumerated one does.

    The evaluator is swapped on the EXISTING client rather than by opening a
    second one: two blocking portals over the same app deadlock, and the symptom
    is a test file that never finishes rather than one that fails.
    """
    main.app.dependency_overrides[main.get_placement_evaluator] = lambda: refuse_databases_sharing
    answer = evaluate(client, reference, TOGETHER)

    assert answer["eligible"] is False
    assert any("may not share a host in prod" in r for r in answer["reasons"])


def test_a_malformed_layout_is_not_sent_to_the_policy(client, reference):
    """Asking OPA about a topology that cannot exist gets an answer about
    something imaginary, and the reply reads as a policy verdict rather than as
    the malformed proposal it is."""
    asked = []

    def record(topology):
        asked.append(topology)
        return {"allow": True, "violations": []}

    main.app.dependency_overrides[main.get_placement_evaluator] = lambda: record
    evaluate(client, reference, [{"id": "h", "host_mode": "vm",
                                  "components": ["oracle-db"]}])

    # NOT "the policy was never asked". It is asked several times, and rightly:
    # the response also carries what the platform's own layouts cost, and each of
    # those is evaluated exactly as /options evaluates it. The property is
    # narrower and is the one that matters — the IMPOSSIBLE layout is not among
    # the things asked about.
    proposed = [c for topology in asked
                for host in topology.get("hosts", [])
                for c in host.get("components", [])]
    assert "oracle-db" not in proposed, (
        "the policy was asked about a layout placing a component nobody "
        "requested; its verdict would read as a policy decision rather than as "
        "the malformed proposal it is")
    assert asked, "the enumerated options are still evaluated"


def test_an_unknown_request_is_404(client):
    r = client.post("/api/placement/evaluate",
                    json={"reference": "REQ-NOPE", "hosts": []})
    assert r.status_code == 404


# --- choosing the layout you arranged (D.3) -----------------------------------
#
# /evaluate answers while the requester drags. This is the write, and the point
# of separating them is that the second one re-judges: minutes may have passed,
# the policy may have changed, and the browser was never the authority. An
# enumerated option is re-decided rather than trusted from the list it was picked
# from, and a custom layout gets exactly the same treatment.

def resolve_custom(client, reference, hosts):
    return client.post("/api/placement/resolve",
                       json={"reference": reference, "option_key": "custom",
                             "hosts": hosts})


def test_a_layout_you_arranged_can_be_chosen(client, reference, db):
    from api.placement_store import current_placement
    from db.models import Request
    from sqlalchemy import select

    r = resolve_custom(client, reference, APART)
    assert r.status_code == 200, r.text
    assert r.json()["option_key"] == "custom"

    req = db.scalar(select(Request).where(Request.reference == reference))
    recorded = current_placement(db, req.id)
    assert recorded is not None
    assert recorded.sizing["machine_count"] == 2


def test_it_is_recorded_and_audited_like_any_other_decision(client, reference, db):
    """No second, quieter way into the record. A custom layout is versioned and
    audited exactly as an offered one is."""
    from db.models import AuditLog
    from sqlalchemy import select

    resolve_custom(client, reference, TOGETHER)
    events = [a.event for a in db.scalars(select(AuditLog)).all()]
    assert "placement.resolved" in events


def test_the_same_judgement_decides_the_drawing_and_the_record(client, reference):
    """Both paths call one function. A slightly different check on the write path
    is how a layout passes on screen and is refused on save — or worse, the
    other way round."""
    shown = evaluate(client, reference, APART)
    written = resolve_custom(client, reference, APART)

    assert shown["eligible"] is True
    assert written.status_code == 200
    assert written.json()["estimate"]["totals"]["monthly"] == pytest.approx(
        shown["totals"]["monthly"])


def test_an_arrangement_the_policy_refuses_cannot_be_recorded(db, reference, client):
    """The re-judgement doing its job. The diagram may have shown this as fine
    before the rule changed."""
    main.app.dependency_overrides[main.get_placement_evaluator] = lambda: refuse_databases_sharing

    r = resolve_custom(client, reference, TOGETHER)
    assert r.status_code == 409
    assert "may not share a host in prod" in r.json()["detail"]


def test_a_smuggled_component_cannot_be_recorded_either(client, reference, db):
    """THE SECURITY PROPERTY, ON THE WRITE PATH. /evaluate refusing it is not
    enough — nothing forces a browser to call /evaluate first."""
    from api.placement_store import placement_history
    from db.models import Request
    from sqlalchemy import select

    r = resolve_custom(client, reference, [
        {"id": "host-1", "host_mode": "vm",
         "components": ["postgres16", "nodejs20", "oracle-db"]}])

    assert r.status_code == 409
    assert "did not ask for" in r.json()["detail"]
    req = db.scalar(select(Request).where(Request.reference == reference))
    assert placement_history(db, req.id) == [], "a refused layout was still written"


def test_choosing_custom_without_a_layout_is_refused(client, reference):
    r = client.post("/api/placement/resolve",
                    json={"reference": reference, "option_key": "custom"})
    assert r.status_code == 400
    assert "has to say what goes where" in r.json()["detail"]


def test_an_offered_option_still_resolves_by_key_alone(client, reference):
    """The existing contract is untouched: every key other than `custom` still
    selects among layouts the server produced, and needs no hosts."""
    r = client.post("/api/placement/resolve",
                    json={"reference": reference, "option_key": "consolidated"})
    assert r.status_code == 200, r.text
    assert r.json()["option_key"] == "consolidated"
