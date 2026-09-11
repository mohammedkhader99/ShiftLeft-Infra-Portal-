"""P.13's acceptance: the same components, placed differently, plan differently.

The unit tests beside this one check each piece. This one drives /provision the
way the portal does and looks at what Terraform would actually be asked to build
— because every piece can be right while the wiring between them sends the same
plan twice.

Terraform itself is stubbed, and so are the three independent re-checks the
orchestrator makes before it will act (the Jira approval, the OPA policy, the
re-priced cost). What is under test here is the ARGUMENTS: how many workspaces,
which module for each, and what shape. A real `terraform plan` needs cloud
credentials and would be testing OCI, not this.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

import orchestrator.main as omain
from orchestrator.tests.http_stub import stub_http
from orchestrator import provisioner


def _signed(payload: dict):
    from common.signing import sign
    body = json.dumps(payload).encode()
    return body, sign(omain.WEBHOOK_SECRET, body)


class _Resp:
    def __init__(self, data):
        self._data = data

    def json(self):
        return self._data

    def raise_for_status(self):
        return None


@pytest.fixture()
def recorded(monkeypatch, tmp_path):
    """Every terraform_plan call, with the arguments it was given."""
    calls: list[dict] = []

    def fake_plan(reference, name, tags, resource_kind="oci-bucket",
                  sizing=None, workspace=""):
        calls.append({"reference": reference, "name": name,
                      "resource_kind": resource_kind, "workspace": workspace,
                      "ocpus": (sizing or {}).get("ocpus"),
                      "memory_gb": (sizing or {}).get("memory_gb"),
                      "boot_volume_gb": (sizing or {}).get("boot_volume_gb")})
        return {"summary": f"Plan: 1 to add ({workspace or resource_kind})",
                "output": "", "scan": {}}

    # The orchestrator never trusts the handoff: it re-verifies the approval in
    # Jira, re-checks OPA and re-prices. Stubbed so these tests exercise dispatch.
    http = stub_http(monkeypatch, omain)
    http.get = lambda *a, **k: _Resp({"status": "approved"})
    http.post = lambda url, *a, **k: _Resp(
        {"totals": {"monthly": 406.77}} if url.endswith("/api/cost")
        else {"result": {"allow": True}})
    monkeypatch.setattr(provisioner, "terraform_plan", fake_plan)
    monkeypatch.setattr(provisioner, "STATE_ROOT", tmp_path)
    monkeypatch.setattr(provisioner, "provision_mode", lambda: "plan")
    # The per-tier compute subnet map is a separate contract; an unmapped tier
    # refuses the handoff and would stop this before it reached the plan.
    monkeypatch.setattr(omain.oke_networks, "compute_subnet", lambda tier: "")
    omain._provisioned.clear()
    return calls


@pytest.fixture()
def client():
    with TestClient(omain.app) as c:
        yield c


def post(client, payload):
    body, signature = _signed(payload)
    return client.post("/provision", content=body,
                       headers={"X-Signature": signature})


def payload_for(hosts, *, version, option, key):
    return {
        "contract_version": "1.0",
        "idempotency_key": key,
        "jira_key": "INFRA-9500",
        "reference": "REQ-2026-9500",
        "resource_kind": "oci-instance",
        "resource_kinds": ["oci-instance"],
        "approved_monthly": 406.77,
        "policy_input": {
            "request_type": "create",
            "deployment_target": "oci",
            "environment_tier": "dev",
            "environment_name": "egate-dev",
            "data_classification": "internal",
            "components": [{"technology_code": "postgres16", "size": "medium"},
                           {"technology_code": "nodejs20", "size": "small"}],
        },
        "placement": {"version": version, "option_key": option, "hosts": hosts},
    }


def machine(host_id, components, vcpu, memory, storage):
    return {"id": host_id, "host_mode": "vm", "components": components,
            "resource_kind": "oci-instance", "resolved": True,
            "vcpu": vcpu, "memory_gb": memory, "storage_gb": storage}


def consolidated(key="idem-consolidated"):
    return payload_for(
        [machine("host-1", ["postgres16", "nodejs20"], 8, 24, 300)],
        version=1, option="consolidated", key=key)


def separated(key="idem-separated"):
    return payload_for(
        [machine("host-1", ["postgres16"], 5, 20, 240),
         machine("host-2", ["nodejs20"], 3, 5, 60)],
        version=2, option="separated", key=key)


# --- one placement, one plan --------------------------------------------------

def test_consolidated_plans_one_machine(client, recorded):
    response = post(client, consolidated())
    assert response.status_code == 200, response.text

    assert len(recorded) == 1
    assert recorded[0]["ocpus"] == 4          # 8 vCPU is 4 OCPUs on x86 flex
    assert recorded[0]["memory_gb"] == 24
    assert recorded[0]["boot_volume_gb"] == 300


def test_separated_plans_two_machines_at_their_own_shapes(client, recorded):
    response = post(client, separated())
    assert response.status_code == 200, response.text

    assert len(recorded) == 2
    shapes = {(c["ocpus"], c["memory_gb"], c["boot_volume_gb"]) for c in recorded}
    # 5 vCPU is 3 OCPUs, not 2: rounded UP, because 2 OCPUs is 4 vCPUs and the
    # 5 came from a 4 vCPU requirement plus P.6's headroom. Rounding it back down
    # would spend the headroom undoing itself.
    assert shapes == {(3, 20, 240), (2, 5, 60)}


def test_the_two_plans_are_not_the_same_plan(client, recorded):
    """The acceptance sentence, stated as one assertion. Identical components,
    different placement, different Terraform."""
    post(client, consolidated())
    one = [dict(c) for c in recorded]
    recorded.clear()
    omain._provisioned.clear()
    post(client, separated())

    assert len(one) == 1 and len(recorded) == 2
    assert {c["workspace"] for c in one} != {c["workspace"] for c in recorded}
    assert {c["ocpus"] for c in one} != {c["ocpus"] for c in recorded}


def test_each_machine_plans_into_its_own_state(client, recorded):
    """Two machines sharing a workspace share a state file, and the second apply
    would propose destroying the first machine in order to build itself."""
    post(client, separated())
    assert len({c["workspace"] for c in recorded}) == 2


def test_each_machine_is_named_apart(client, recorded):
    post(client, separated())
    assert len({c["name"] for c in recorded}) == 2


def test_the_module_is_still_chosen_by_resource_kind(client, recorded):
    """The workspace is per host; the MODULE is per kind. Both machines are
    oci-instance and both must build from that module."""
    post(client, separated())
    assert {c["resource_kind"] for c in recorded} == {"oci-instance"}


# --- traceability -------------------------------------------------------------

def test_the_response_says_which_placement_produced_the_plan(client, recorded):
    """The second half of the acceptance. Without it a plan is an orphan: the
    requester can change their mind, each change is a new version, and "why does
    this build three machines?" has no answer that can be checked."""
    body = post(client, separated()).json()

    assert body["placement_version"] == 2
    assert body["placement_option"] == "separated"
    assert sorted(body["workspaces"]) == ["oci-instance__host-1",
                                          "oci-instance__host-2"]


# --- nothing is built at a shape nobody approved ------------------------------

def test_an_unsized_machine_stops_the_handoff(client, recorded):
    """Refused, not planned at a default. It was excluded from the approved cost,
    so building it would provision something nobody approved."""
    unsized = payload_for(
        [{"id": "host-1", "host_mode": "vm", "components": ["opensearch"],
          "resource_kind": "oci-instance", "resolved": False,
          "vcpu": None, "memory_gb": None, "storage_gb": None}],
        version=3, option="separated", key="idem-unsized")

    response = post(client, unsized)
    assert response.status_code == 400
    assert "no resolved size" in response.json()["detail"]
    assert recorded == [], "nothing may be planned once the layout is refused"


def test_a_host_no_blueprint_builds_stops_the_handoff(client, recorded):
    unmapped = payload_for(
        [{"id": "host-1", "host_mode": "vm", "components": ["mystery"],
          "resource_kind": None, "resolved": True,
          "vcpu": 2, "memory_gb": 4, "storage_gb": 50}],
        version=4, option="separated", key="idem-unmapped")

    response = post(client, unmapped)
    assert response.status_code == 400
    assert "no certified blueprint builds" in response.json()["detail"]
    assert recorded == []


# --- a request with no placement is untouched ---------------------------------

def test_a_payload_with_no_placement_plans_exactly_as_it_used_to(client, recorded):
    """Every request raised before placement existed goes down the old path."""
    legacy = {k: v for k, v in consolidated("idem-legacy").items()
              if k != "placement"}

    assert post(client, legacy).status_code == 200
    assert len(recorded) == 1
    assert recorded[0]["workspace"] == "", "the old path names no workspace"
    assert recorded[0]["resource_kind"] == "oci-instance"


def test_the_old_path_response_carries_no_placement_version(client, recorded):
    legacy = {k: v for k, v in consolidated("idem-legacy-2").items()
              if k != "placement"}
    assert "placement_version" not in post(client, legacy).json()
