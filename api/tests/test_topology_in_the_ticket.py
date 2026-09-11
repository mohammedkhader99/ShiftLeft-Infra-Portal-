"""The approver judges configuration and cost together (P.12, F-INT-13).

A component list is not an architecture. "compute-vm, postgres16, nodejs20" is
the same three lines whether it becomes one machine or three, whether the
database is something the team patches or something the cloud runs, and whether
the monthly figure is 406 or 592. The person granting the money was being shown
the list and asked to approve the arrangement.

The second property here matters more than the first, and it is not about
readability at all. `req.estimate` is signed into the orchestrator handoff as
`approved_monthly`, and the orchestrator re-validates the real cost against it
before building. The orchestrator builds the PLACEMENT. So if the ticket showed
the post-placement figure while `req.estimate` kept the per-component one, the
portal would be enforcing a number nobody was ever shown — and a managed layout,
which costs more than the per-component estimate rather than less, would be
refused at execution for a discrepancy the portal created itself.

One figure, everywhere, or the guard is guarding a fiction.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from api.jira import build_topology_summary
from api.pricing import placement_attachment_rows
from api.main import app, get_placement_evaluator, get_session
from db.models import Estimate, Request, RequestPlacement
from db.seed import seed
from db.session import Base

# Raised through the draft endpoint rather than assembled as a Request row.
# Submit validates governance metadata, owners and the delivery date, so a
# hand-built row is refused with 422 before it reaches anything this file is
# about — and the point here is the ticket a REAL submission produces.
COMPONENTS = [
    {"technology_code": "compute-vm", "size": "small"},
    {"technology_code": "postgres16", "size": "medium"},
    {"technology_code": "nodejs20", "size": "small"},
]
DRAFT = {
    "request_type": "create",
    "project_code": "EGATE",
    "cost_centre_code": "IMD-1001",
    # OCI, because the managed option only exists where the cloud runs the
    # component -- and the dearer-layout test depends on that option existing.
    "deployment_target": "oci",
    "environment_name": "egate-dev",
    "environment_tier": "dev",
    "data_classification": "internal",
    "components": COMPONENTS,
    "business_justification": "Needed to run the eGate load tests before go-live.",
    "priority": "high",
    "business_criticality": "tier2",
    "required_delivery_date": (date.today() + timedelta(days=30)).isoformat(),
    "application_owner": "app.owner@emaratechg.ae",
    "technical_owner": "tech.owner@emaratechg.ae",
}


def allow_everything(_topology):
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
        yield session


@pytest.fixture()
def client(db):
    app.dependency_overrides[get_session] = lambda: db
    app.dependency_overrides[get_placement_evaluator] = lambda: allow_everything
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture()
def reference(client):
    r = client.post("/api/requests/draft", json=DRAFT)
    assert r.status_code == 200, r.text
    return r.json()["reference"]


def resolve(client, reference, option_key):
    r = client.post("/api/placement/resolve",
                    json={"reference": reference, "option_key": option_key})
    assert r.status_code == 200, r.text
    return r.json()


def submit(client, reference):
    r = client.post(f"/api/requests/{reference}/submit")
    assert r.status_code == 200, r.text
    return r.json()


def per_component_monthly(client):
    """What the OLD calculation says, for the tests that compare the two."""
    return client.post("/api/cost", json={
        "deployment_target": "oci", "components": COMPONENTS,
    }).json()["totals"]["monthly"]


def stored_estimate(db, reference):
    return db.scalar(select(Estimate).join(Request)
                     .where(Request.reference == reference))


# --- the ticket reads as an architecture -------------------------------------

def test_a_consolidated_request_says_it_is_one_machine(client, reference):
    resolve(client, reference, "consolidated")
    body = submit(client, reference)["approval"]["ticket_body"]

    assert "How it will be built:" in body
    assert "Layout: Consolidated" in body
    assert "1 machine is priced below." in body


def test_the_ticket_says_what_runs_on_each_machine(client, reference):
    resolve(client, reference, "separated")
    body = submit(client, reference)["approval"]["ticket_body"]

    assert "runs: postgres16" in body
    assert "runs: nodejs20" in body
    assert "2 machines are priced below." in body


def test_each_machine_carries_the_shape_it_will_be_built_at(client, reference):
    """The size is the point. An approver asked to sign off "a machine" without
    knowing whether it is 2 vCPU or 32 is not being asked anything."""
    resolve(client, reference, "consolidated")
    body = submit(client, reference)["approval"]["ticket_body"]

    assert "vCPU" in body and "GB RAM" in body and "GB disk" in body
    assert "headroom" in body, "the padding on the shape is stated, not hidden"


def test_a_managed_component_is_named_as_run_by_the_cloud(client, reference):
    """The difference an approver most needs and a component list never shows:
    whether somebody on the team patches this database at 2am."""
    resolve(client, reference, "managed")
    body = submit(client, reference)["approval"]["ticket_body"]

    assert "Run by the cloud: postgres16" in body
    assert "No machine is provisioned" in body


def test_the_layout_is_named_the_same_way_the_requester_saw_it(client, reference):
    """Only the KEY is persisted. If the ticket invented its own wording, the
    approver and the requester would be describing the same layout differently
    and neither would know."""
    from api.placement import OPTION_TITLES

    resolve(client, reference, "managed")
    body = submit(client, reference)["approval"]["ticket_body"]
    assert OPTION_TITLES["managed"] in body


def test_the_placement_version_is_recorded_in_the_ticket(client, reference):
    """Placements are versioned and a requester may change their mind. The
    ticket has to say which one was approved."""
    resolve(client, reference, "consolidated")
    resolve(client, reference, "separated")
    body = submit(client, reference)["approval"]["ticket_body"]

    assert "version 2" in body
    assert "Layout: Separated" in body


# --- and its cost is the post-placement one ----------------------------------

def test_the_ticket_cost_matches_the_recorded_placement(client, reference):
    """The acceptance check for this increment."""
    placed = resolve(client, reference, "consolidated")
    expected = placed["estimate"]["totals"]["monthly"]

    body = submit(client, reference)["approval"]["ticket_body"]
    assert f"monthly {expected:.2f}" in body
    assert "Estimated cost after placement" in body


def test_the_stored_estimate_matches_the_ticket(client, db, reference):
    """The one that would have bitten. `req.estimate` is signed into the handoff
    as `approved_monthly` and the orchestrator holds the build to it — so it must
    be the figure the approver actually saw."""
    placed = resolve(client, reference, "consolidated")
    submit(client, reference)

    stored = stored_estimate(db, reference)
    assert stored is not None
    assert float(stored.monthly) == pytest.approx(
        placed["estimate"]["totals"]["monthly"])


def test_the_dearest_layout_is_still_approved_at_its_own_price(client, db, reference):
    """The swap must follow the placement in EITHER direction.

    Direction is what makes this dangerous. The orchestrator refuses an actual
    cost that exceeds `approved_monthly`, so a placement dearer than the stored
    figure would be refused at execution for a gap the portal itself created —
    the requester was shown one number and the build held to another. A placement
    cheaper than the stored figure fails the other way: the budget guardrail
    reserves money the request never spends.

    With the seeded rates every layout happens to be cheaper than the
    per-component estimate, which prices each component as its own machine. So
    this cannot force the dearer case; what it can prove is that the stored
    figure tracks the CHOSEN layout rather than the old calculation, which is
    the property that makes direction stop mattering.
    """
    per_component = per_component_monthly(client)

    options = client.post("/api/placement/options",
                          json={"reference": reference}).json()["options"]
    priceable = [o for o in options if o["resolved"]]
    dearest = max(priceable, key=lambda o: o["totals"]["monthly"])

    placed = resolve(client, reference, dearest["key"])
    chosen_monthly = placed["estimate"]["totals"]["monthly"]

    submit(client, reference)
    stored = stored_estimate(db, reference)

    assert float(stored.monthly) == pytest.approx(chosen_monthly)
    assert float(stored.monthly) != pytest.approx(per_component), (
        "the two calculations agreed, so this proves nothing about which was used")


def test_the_licence_split_survives_the_swap(client, reference):
    """Every field in the per-component breakdown is in the ticket because an
    approver was once misled without it. Substituting a thinner dict would have
    silently dropped the licence note, the "not the whole cost" caveat and the
    foreign-currency line while still appearing to work."""
    resolve(client, reference, "consolidated")
    body = submit(client, reference)["approval"]["ticket_body"]

    assert "Monthly cost is made up of" in body
    assert "Compute:" in body


def test_the_cost_is_labelled_as_the_placement_calculation(client, reference):
    """Two different figures are derivable from one request. A bare "Estimated
    cost" does not say which one the approver is agreeing to."""
    resolve(client, reference, "consolidated")
    body = submit(client, reference)["approval"]["ticket_body"]

    assert "Estimated cost after placement" in body
    assert "bought once per machine" in body


# --- a request with no placement is untouched --------------------------------

def test_an_unplaced_request_reads_exactly_as_it_always_did(client, reference):
    """Every request raised before this step existed has no placement, and this
    increment must not change a single line of their tickets."""
    body = submit(client, reference)["approval"]["ticket_body"]

    assert "How it will be built:" not in body
    assert "Estimated cost (" in body
    assert "after placement" not in body
    assert "Components:" in body


def test_an_unplaced_request_keeps_the_per_component_figure(client, db, reference):
    per_component = per_component_monthly(client)

    submit(client, reference)
    stored = stored_estimate(db, reference)
    assert float(stored.monthly) == pytest.approx(per_component)


# --- the summary builder on its own ------------------------------------------

def test_no_placement_produces_no_section():
    assert build_topology_summary(None) == []


def test_a_placement_with_no_sized_hosts_produces_no_section():
    """A placement recorded before sizing existed, or one whose sizing was
    empty, must not emit a heading with nothing under it."""
    placement = RequestPlacement(request_id=1, version=1, option_key="managed",
                                 topology={}, sizing={}, estimate={})
    assert build_topology_summary(placement) == []


def test_an_unsized_host_is_named_not_omitted_and_not_shown_as_zero():
    """The failure this project has been bitten by twice, in its third form. The
    machine is in the topology and will be built; what is missing is the
    requirement saying how big. Dropping it would make the total look complete."""
    placement = RequestPlacement(
        request_id=1, version=1, option_key="separated", topology={},
        estimate={},
        sizing={"machine_count": 0, "hosts": [{
            "host_id": "host-1", "host_mode": "vm", "components": ["opensearch"],
            "resolved": False, "missing": ["opensearch"], "headroom_percent": 20,
            "vcpu": None, "memory_gb": None, "storage_gb": None,
        }]})
    text = "\n".join(build_topology_summary(placement))

    assert "SIZE NOT DETERMINED" in text
    assert "opensearch" in text
    assert "not included in the cost below" in text
    assert "0 vCPU" not in text


def test_an_unsized_machine_is_not_counted_as_zero_machines():
    """The count only counts what could be SIZED, so it cannot stand alone.
    `machine_count` is incremented per resolved host, so a placement whose
    machines all failed to size printed "0 machines" directly beneath a line
    naming one — a contradiction inside a single paragraph, on the document
    somebody signs."""
    placement = RequestPlacement(
        request_id=1, version=1, option_key="separated", topology={}, estimate={},
        sizing={"machine_count": 0, "hosts": [{
            "host_id": "host-1", "host_mode": "vm", "components": ["opensearch"],
            "resolved": False, "missing": ["opensearch"], "headroom_percent": 20,
            "vcpu": None, "memory_gb": None, "storage_gb": None,
        }]})
    text = "\n".join(build_topology_summary(placement))

    assert "No machines are provisioned by this request." not in text
    assert "PLUS 1 machine whose size could not be determined" in text
    assert "will be built; the cost below EXCLUDES it" in text


def test_a_partly_sized_placement_says_how_many_are_priced_and_how_many_are_not():
    placement = RequestPlacement(
        request_id=1, version=1, option_key="separated", topology={}, estimate={},
        sizing={"machine_count": 1, "hosts": [
            {"host_id": "host-1", "host_mode": "vm", "components": ["nodejs20"],
             "resolved": True, "missing": [], "headroom_percent": 20,
             "vcpu": 3, "memory_gb": 5, "storage_gb": 60},
            {"host_id": "host-2", "host_mode": "vm", "components": ["opensearch"],
             "resolved": False, "missing": ["opensearch"], "headroom_percent": 20,
             "vcpu": None, "memory_gb": None, "storage_gb": None},
        ]})
    text = "\n".join(build_topology_summary(placement))

    assert "1 machine is priced below." in text
    assert "PLUS 1 machine whose size could not be determined" in text


def test_an_all_managed_placement_provisions_no_machines():
    placement = RequestPlacement(
        request_id=1, version=1, option_key="managed", topology={}, estimate={},
        sizing={"machine_count": 0, "hosts": [{
            "host_id": "managed-postgres16", "host_mode": "managed",
            "components": ["postgres16"], "resolved": True, "missing": [],
            "headroom_percent": 20, "vcpu": None, "memory_gb": None,
            "storage_gb": None,
        }]})
    text = "\n".join(build_topology_summary(placement))

    assert "No machines are provisioned by this request." in text
    assert "PLUS" not in text


def test_an_unknown_layout_key_is_shown_rather_than_hidden():
    """A ticket naming a layout the code no longer recognises is still telling
    the truth about what was chosen."""
    placement = RequestPlacement(
        request_id=1, version=3, option_key="some-future-layout", topology={},
        estimate={},
        sizing={"machine_count": 1, "hosts": [{
            "host_id": "host-1", "host_mode": "vm", "components": ["nodejs20"],
            "resolved": True, "missing": [], "headroom_percent": 20,
            "vcpu": 3, "memory_gb": 5, "storage_gb": 60,
        }]})
    assert "some-future-layout" in "\n".join(build_topology_summary(placement))


def test_a_cluster_id_reaches_the_approver():
    """Which cluster is one of the things P.12 exists to state. Recorded as the
    id because that is what the placement stores — the cluster list is fetched
    live and never persisted, so resolving a name here could disagree with what
    was actually authorised."""
    placement = RequestPlacement(
        request_id=1, version=1, option_key="existing-cluster",
        topology={"hosts": [{"id": "ocid1.cluster.oc1..aaaa",
                             "host_mode": "container",
                             "components": ["nodejs20"]}]},
        estimate={},
        sizing={"machine_count": 1, "hosts": [{
            "host_id": "ocid1.cluster.oc1..aaaa", "host_mode": "container",
            "components": ["nodejs20"], "resolved": True, "missing": [],
            "headroom_percent": 20, "vcpu": 3, "memory_gb": 5, "storage_gb": 60,
        }]})
    text = "\n".join(build_topology_summary(placement))

    assert "Cluster: ocid1.cluster.oc1..aaaa" in text
    assert "Container host" in text, "a cluster workload is not called a machine"


def test_a_cluster_on_any_cloud_reaches_the_approver():
    """Recognised by the placement's vocabulary, not by the id's format. Matching
    ids that begin "ocid" would pass every OCI cluster and silently drop an AKS
    or GKE one, leaving the approver reading a ticket that never said where the
    workload was going."""
    placement = RequestPlacement(
        request_id=1, version=1, option_key="existing-cluster",
        topology={"hosts": [{"id": "/subscriptions/abc/aks-egate-dev",
                             "host_mode": "container",
                             "components": ["nodejs20"]}]},
        estimate={},
        sizing={"machine_count": 1, "hosts": [{
            "host_id": "/subscriptions/abc/aks-egate-dev", "host_mode": "container",
            "components": ["nodejs20"], "resolved": True, "missing": [],
            "headroom_percent": 20, "vcpu": 3, "memory_gb": 5, "storage_gb": 60,
        }]})
    assert "Cluster: /subscriptions/abc/aks-egate-dev" in "\n".join(
        build_topology_summary(placement))


def test_a_new_cluster_names_no_cluster_because_there_is_not_one_yet():
    """"Provision a new cluster" has nothing to name. Printing the placeholder
    host id as though it were a cluster would invent one."""
    placement = RequestPlacement(
        request_id=1, version=1, option_key="new-cluster",
        topology={"hosts": [{"id": "new-cluster", "host_mode": "container",
                             "components": ["nodejs20"]}]},
        estimate={},
        sizing={"machine_count": 1, "hosts": [{
            "host_id": "new-cluster", "host_mode": "container",
            "components": ["nodejs20"], "resolved": True, "missing": [],
            "headroom_percent": 20, "vcpu": 3, "memory_gb": 5, "storage_gb": 60,
        }]})
    assert "Cluster:" not in "\n".join(build_topology_summary(placement))


def test_an_unnamed_existing_cluster_is_not_reported_as_one():
    """The placeholder id survives only if nothing was authorised, which the API
    refuses — but the ticket must not turn the placeholder into a cluster name if
    it ever does."""
    placement = RequestPlacement(
        request_id=1, version=1, option_key="existing-cluster",
        topology={"hosts": [{"id": "existing-cluster", "host_mode": "container",
                             "components": ["nodejs20"]}]},
        estimate={},
        sizing={"machine_count": 1, "hosts": [{
            "host_id": "existing-cluster", "host_mode": "container",
            "components": ["nodejs20"], "resolved": True, "missing": [],
            "headroom_percent": 20, "vcpu": 3, "memory_gb": 5, "storage_gb": 60,
        }]})
    assert "Cluster:" not in "\n".join(build_topology_summary(placement))


# --- the approver's attachments describe what was priced ---------------------
#
# `build_request_pdf` and `build_cost_sheet_xlsx` pair sizing["components"][i]
# with breakdown["lines"][i] BY POSITION. That was safe while a component and a
# priced line were the same thing; a placement prices MACHINES, so three
# components on one host become one line. Left alone the PDF would have printed
# a machine's monthly figure beside a component's name — a per-component price
# nobody calculated, in the document handed to the person approving the money.

CONSOLIDATED_SIZING = {
    "machine_count": 1, "resolved": True, "headroom_percent": 20,
    "hosts": [{
        "host_id": "host-1", "host_mode": "vm",
        "components": ["postgres16", "nodejs20"], "resolved": True,
        "missing": [], "headroom_percent": 20,
        "vcpu": 8, "memory_gb": 24, "storage_gb": 300,
    }],
}
MANAGED_SIZING = {
    "machine_count": 1, "resolved": True, "headroom_percent": 20,
    "hosts": [
        {"host_id": "managed-postgres16", "host_mode": "managed",
         "components": ["postgres16"], "resolved": True, "missing": [],
         "headroom_percent": 20, "vcpu": None, "memory_gb": None,
         "storage_gb": None},
        {"host_id": "host-1", "host_mode": "vm", "components": ["nodejs20"],
         "resolved": True, "missing": [], "headroom_percent": 20,
         "vcpu": 3, "memory_gb": 5, "storage_gb": 60},
    ],
}


def test_one_machine_is_one_row_not_one_row_per_component():
    """The mispairing, stated as the property that prevents it."""
    rows = placement_attachment_rows(CONSOLIDATED_SIZING)["components"]

    assert len(rows) == 1, "two components on one host are one machine"
    assert rows[0]["vcpu"] == 8
    assert "postgres16" in rows[0]["technology_name"]
    assert "nodejs20" in rows[0]["technology_name"]


def test_the_row_order_matches_the_order_the_lines_were_priced_in():
    """Machines first, then managed — the order estimate_placement_cost merges
    its lines in. The positional pairing is only safe because of this."""
    rows = placement_attachment_rows(MANAGED_SIZING)["components"]

    assert [r["technology_code"] for r in rows] == ["compute-vm", "postgres16"]
    assert "run by the cloud" in rows[1]["technology_name"]


def test_an_unsized_machine_is_listed_last_and_carries_no_shape():
    """Included, because it will be built and dropping it would make the table
    look like the whole request. Last, so it cannot shift the rows above out of
    step with the prices. With no shape, because the shape is what is missing."""
    sizing = {"machine_count": 1, "resolved": False, "hosts": [
        dict(CONSOLIDATED_SIZING["hosts"][0]),
        {"host_id": "host-2", "host_mode": "vm", "components": ["opensearch"],
         "resolved": False, "missing": ["opensearch"], "headroom_percent": 20,
         "vcpu": None, "memory_gb": None, "storage_gb": None},
    ]}
    rows = placement_attachment_rows(sizing)["components"]

    assert len(rows) == 2
    assert rows[0]["vcpu"] == 8, "the priced machine keeps position 0"
    assert "SIZE NOT DETERMINED" in rows[1]["technology_name"]
    assert rows[1]["vcpu"] is None
    assert rows[1]["resolved"] is False


def test_the_pdf_prices_each_machine_at_its_own_line(client, reference):
    """End to end: the rows and the prices line up on a real submission."""
    from api.pricing import estimate_placement_cost
    from api.sizing import load_requirements, size_hosts

    placed = resolve(client, reference, "consolidated")
    sizing = placed["sizing"]
    rows = placement_attachment_rows(sizing)["components"]
    lines = placed["estimate"]["lines"]

    assert len(rows) == len(lines), (
        "the PDF reads lines[i] for row i; a length mismatch is a mispairing")
    assert lines[0]["monthly"] > 0


def test_the_attachment_total_agrees_with_the_ticket(client, reference, db):
    """The PDF prints breakdown["totals"], the ticket prints the same dict. Two
    documents in front of one approver must not carry two different figures."""
    placed = resolve(client, reference, "consolidated")
    body = submit(client, reference)["approval"]["ticket_body"]

    monthly = placed["estimate"]["totals"]["monthly"]
    assert f"monthly {monthly:.2f}" in body
    assert float(stored_estimate(db, reference).monthly) == pytest.approx(monthly)


def test_an_unplaced_request_still_gets_per_component_rows(db):
    """The change is opt-in. With no placement the submit path calls
    resolve_components, which is one row per component and always has been."""
    from api.sizing import resolve_components

    rows = resolve_components(COMPONENTS, db)["components"]
    assert len(rows) == len(COMPONENTS)
    assert [r["technology_code"] for r in rows] == [
        c["technology_code"] for c in COMPONENTS]


# --- a pod is not a machine, on the document somebody signs -------------------


def _summary(machine_count: int, container_count: int, hosts: list[dict]) -> str:
    """The ticket lines for a recorded placement, without raising a request.

    `build_topology_summary` reads only `option_key`, `version` and `sizing`, so
    the wording can be tested directly. The end-to-end tests above cover the
    path that produces one.
    """
    from types import SimpleNamespace

    from api import jira

    placement = SimpleNamespace(
        option_key="new-cluster", version=1, created_at=None,
        sizing={"machine_count": machine_count,
                "container_count": container_count, "hosts": hosts})
    return "\n".join(jira.build_topology_summary(placement))


def _pod(host_id: str, component: str) -> dict:
    return {"host_id": host_id, "host_mode": "container", "components": [component],
            "resolved": True, "vcpu": 4, "memory_gb": 16, "storage_gb": 200,
            "headroom_percent": 20}


def test_the_ticket_counts_pods_as_containers_not_machines():
    """It said "3 machines are priced below" for three pods on a cluster.

    Counting them correctly and changing nothing else would have been worse:
    "0 machines are priced below" printed directly above three priced pods,
    which is the contradiction this file already fought once.
    """
    text = _summary(0, 2, [
        {"host_id": "new-cluster", "host_mode": "managed",
         "components": ["oci-oke"], "resolved": True},
        _pod("cluster-postgres16", "postgres16"),
        _pod("cluster-vault", "vault"),
    ])

    assert "2 containers are priced below" in text, text
    assert "machines are priced" not in text, "a pod is not a machine"
    assert "No machine is provisioned for it" in text, "and it says why"


def test_machines_and_containers_are_both_named_when_both_are_built():
    text = _summary(1, 1, [
        {"host_id": "host-1", "host_mode": "vm", "components": ["nginx"],
         "resolved": True, "vcpu": 2, "memory_gb": 4, "storage_gb": 50,
         "headroom_percent": 20},
        _pod("cluster-vault", "vault"),
    ])

    # A compound subject takes a plural verb, however small the numbers.
    assert "1 machine and 1 container are priced below" in text, text


def test_a_machine_only_layout_reads_exactly_as_it_always_did():
    """The wording changed for clusters. It must not have changed for the
    layouts every request has used until now."""
    text = _summary(2, 0, [
        {"host_id": "host-1", "host_mode": "vm", "components": ["nginx"],
         "resolved": True, "vcpu": 2, "memory_gb": 4, "storage_gb": 50,
         "headroom_percent": 20},
        {"host_id": "host-2", "host_mode": "vm", "components": ["postgres16"],
         "resolved": True, "vcpu": 4, "memory_gb": 16, "storage_gb": 200,
         "headroom_percent": 20},
    ])

    assert "2 machines are priced below" in text, text
    assert "container" not in text.lower(), "nothing about containers on a VM layout"
