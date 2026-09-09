"""The placement travels as data inside the signed handoff (P.13, F-ORC-11).

Placement decides how many machines get built and how big each one is. That has
to reach the orchestrator as a structured list of hosts — not as a sentence in
the ticket for a person to read and act on, and not as a key the far end
interprets for itself.

INSIDE THE SIGNED BODY, which is the part that matters. The signature covers the
whole payload, so a layout cannot be altered between the approval and the build.
Sent alongside it, the number of machines would be the one thing about a request
that nobody had authenticated.

EACH HOST CARRIES ITS RESOURCE KIND, resolved on this side. The blueprint table
is a catalogue fact and the API owns the catalogue (ARCHITECTURE.md P7). Deriving
it at the far end would put two answers in the system that could disagree, and
the far end is the one holding the cloud credentials.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import api.main as main
from api.placement_store import record_placement
from db.models import Blueprint, Request, RequestComponent
from db.seed import seed
from db.session import Base

TOPOLOGY = {
    "environment": "dev", "deployment_target": "oci",
    "hosts": [{"id": "host-1", "host_mode": "vm",
               "components": ["postgres16", "nodejs20"]}],
}
SIZING = {
    "machine_count": 1, "resolved": True, "headroom_percent": 20,
    "hosts": [{"host_id": "host-1", "host_mode": "vm",
               "components": ["postgres16", "nodejs20"], "resolved": True,
               "missing": [], "headroom_percent": 20,
               "vcpu": 8, "memory_gb": 24, "storage_gb": 300, "iops": 0}],
}


@pytest.fixture()
def session():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True,
                           connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    TestSession = sessionmaker(bind=engine, expire_on_commit=False)
    with TestSession() as s:
        seed(s)
        yield s


@pytest.fixture()
def req(session):
    request = Request(reference="REQ-2026-9400", request_type="create",
                      requester="tester@example.com", deployment_target="oci",
                      environment_tier="dev", project_code="EGATE",
                      cost_centre_code="IMD-1001", environment_name="egate-dev")
    session.add(request)
    session.flush()
    for code, size in (("postgres16", "medium"), ("nodejs20", "small")):
        session.add(RequestComponent(request_id=request.id,
                                     technology_code=code, size=size))
    # Blueprints are registered by the catalogue flow, not by the seed, so the
    # kind lookup has nothing to find without these.
    for code, kind in (("postgres16", "oci-instance"), ("nodejs20", "oci-instance")):
        session.add(Blueprint(technology_code=code, deployment_target="oci",
                              status="certified", resource_kind=kind,
                              blueprint_ref=f"oci/{code}"))
    session.commit()
    session.refresh(request)
    return request


def handoff(req, session):
    return main._placement_handoff(session, req)


# --- what the orchestrator receives ------------------------------------------

def test_nothing_placed_means_nothing_sent(req, session):
    """Every request raised before placement existed, and the payload they have
    always had."""
    assert handoff(req, session) is None


def test_the_hosts_carry_their_shape(req, session):
    record_placement(session, req.id, "consolidated", TOPOLOGY, sizing=SIZING)
    session.commit()

    sent = handoff(req, session)
    assert len(sent["hosts"]) == 1
    host = sent["hosts"][0]
    assert (host["vcpu"], host["memory_gb"], host["storage_gb"]) == (8, 24, 300)
    assert host["components"] == ["postgres16", "nodejs20"]


def test_each_host_carries_the_kind_that_builds_it(req, session):
    record_placement(session, req.id, "consolidated", TOPOLOGY, sizing=SIZING)
    session.commit()
    assert handoff(req, session)["hosts"][0]["resource_kind"] == "oci-instance"


def test_a_host_no_blueprint_builds_carries_no_kind(req, session):
    """Null, not a guess. The orchestrator refuses it; inventing a kind here is
    how a request for Python 3.12 once received an empty bucket."""
    sizing = {**SIZING, "hosts": [{**SIZING["hosts"][0],
                                   "components": ["mystery-tech"]}]}
    record_placement(session, req.id, "consolidated", TOPOLOGY, sizing=sizing)
    session.commit()
    assert handoff(req, session)["hosts"][0]["resource_kind"] is None


def test_the_version_travels_so_a_plan_can_be_traced_back(req, session):
    """A requester may change their mind more than once. Without the version,
    "why does this build three machines?" has no answer that can be checked
    against the record."""
    record_placement(session, req.id, "consolidated", TOPOLOGY, sizing=SIZING)
    record_placement(session, req.id, "separated", TOPOLOGY, sizing=SIZING)
    session.commit()

    sent = handoff(req, session)
    assert sent["version"] == 2
    assert sent["option_key"] == "separated"


def test_a_superseded_placement_is_not_sent(req, session):
    """The one in force is the one nothing has superseded — reading the newest
    row would resurrect a layout that had been withdrawn."""
    record_placement(session, req.id, "managed", TOPOLOGY, sizing=SIZING)
    record_placement(session, req.id, "consolidated", TOPOLOGY, sizing=SIZING)
    session.commit()
    assert handoff(req, session)["option_key"] == "consolidated"


def test_an_unsized_host_is_sent_marked_rather_than_dropped(req, session):
    """The orchestrator has to be able to refuse it. Dropping it here would let
    the build proceed with a machine silently missing."""
    sizing = {"machine_count": 0, "resolved": False, "hosts": [
        {"host_id": "host-1", "host_mode": "vm", "components": ["opensearch"],
         "resolved": False, "missing": ["opensearch"], "headroom_percent": 20,
         "vcpu": None, "memory_gb": None, "storage_gb": None}]}
    record_placement(session, req.id, "separated", TOPOLOGY, sizing=sizing)
    session.commit()

    host = handoff(req, session)["hosts"][0]
    assert host["resolved"] is False
    assert host["vcpu"] is None


# --- the cluster the requester was actually authorised for --------------------

def test_the_authorised_cluster_id_is_the_one_sent(req, session):
    """P.9 stamps the authorised cluster over the placeholder host id AFTER
    sizing is computed, so the two lists carry different ids for the same host
    and only the topology's is the one entitlement approved."""
    topology = {"environment": "dev", "deployment_target": "oci",
                "hosts": [{"id": "ocid1.cluster.oc1..aaaa",
                           "host_mode": "container",
                           "components": ["nodejs20"]}]}
    sizing = {"machine_count": 1, "resolved": True, "hosts": [
        {"host_id": "existing-cluster", "host_mode": "container",
         "components": ["nodejs20"], "resolved": True, "missing": [],
         "headroom_percent": 20, "vcpu": 3, "memory_gb": 5, "storage_gb": 60}]}
    record_placement(session, req.id, "existing-cluster", topology, sizing=sizing)
    session.commit()

    assert handoff(req, session)["hosts"][0]["id"] == "ocid1.cluster.oc1..aaaa"


def test_a_length_mismatch_falls_back_to_the_sizings_own_id(req, session):
    """Pairing by position is sound only because both lists come from the same
    option in the same order. If that stops being true, pairing the wrong ones
    together would attach a workload to a cluster nobody authorised — so it falls
    back instead."""
    topology = {"hosts": [{"id": "ocid1.cluster.oc1..aaaa"},
                          {"id": "an-extra-host"}]}
    record_placement(session, req.id, "existing-cluster", topology, sizing=SIZING)
    session.commit()

    assert handoff(req, session)["hosts"][0]["id"] == "host-1"


# --- and it is inside the signature ------------------------------------------

def test_the_placement_is_inside_the_signed_body(req, session):
    """Not alongside it. The signature covers the whole payload, so the number of
    machines cannot be altered between approval and build."""
    from db.models import Approval, Estimate

    record_placement(session, req.id, "consolidated", TOPOLOGY, sizing=SIZING)
    req.approval = Approval(jira_key="INFRA-9400", status="approved")
    req.estimate = Estimate(deployment_target="oci", currency="AED",
                            one_time=0, monthly=406.77, annual=4881.24,
                            breakdown={})
    session.commit()

    body, _signature = main._handoff_payload(req)
    payload = json.loads(body)

    assert "placement" in payload, "the layout was not signed"
    assert payload["placement"]["version"] == 1
    assert payload["placement"]["hosts"][0]["vcpu"] == 8


def test_a_request_with_no_placement_signs_the_payload_it_always_did(req, session):
    from db.models import Approval, Estimate

    req.approval = Approval(jira_key="INFRA-9401", status="approved")
    req.estimate = Estimate(deployment_target="oci", currency="AED",
                            one_time=0, monthly=1.0, annual=12.0, breakdown={})
    session.commit()

    payload = json.loads(main._handoff_payload(req)[0])
    assert "placement" not in payload
