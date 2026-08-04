"""A request is a stack, not a single resource (F-CAT-10).

REQ-2026-0094 asked for Apache and a compute VM. It built Apache, dropped the VM
without a word, and closed its Jira ticket as Resolved. The resource kinds were
collapsed to one by sorting them and taking the first, so "Apache" won on
alphabetical order over "instance".

These tests cover the provisioning half: every kind is planned, applied,
destroyed and drift-checked. The two that matter most are the workspace-layout
ones — a workspace that cannot find its state believes its resource does not
exist, which means real infrastructure left running and unmanaged.
"""

import json

import orchestrator.main as omain
from orchestrator import provisioner


# --- Reading the stack off the handoff ---------------------------------------

def test_every_requested_kind_is_read_from_the_payload():
    payload = {"resource_kind": "oci-apache",
               "resource_kinds": ["oci-apache", "oci-instance"]}
    assert omain._resource_kinds(payload) == ["oci-apache", "oci-instance"]
    # The primary is still available for single-resource operations.
    assert omain._resource_kind(payload) == "oci-apache"


def test_an_older_portal_that_sends_one_kind_still_works():
    """The contract has to stay backward compatible: refusing the handoff would
    break provisioning outright during a rolling deploy."""
    assert omain._resource_kinds({"resource_kind": "oci-bucket"}) == ["oci-bucket"]
    assert omain._resource_kinds({}) == ["oci-bucket"]
    assert omain._resource_kinds({"resource_kinds": []}) == ["oci-bucket"]


# --- Naming ------------------------------------------------------------------

def test_a_stack_names_its_resources_apart(tmp_path, monkeypatch):
    monkeypatch.setattr(provisioner, "STATE_ROOT", tmp_path)
    assert omain._resource_name("r1", "oci-apache", "REQ-N", "oci-apache") == "r1-apache"
    assert omain._resource_name("r1", "oci-instance", "REQ-N", "oci-apache") == "r1-instance"


def test_a_resource_already_built_keeps_the_name_it_was_built_with(tmp_path, monkeypatch):
    """REQ-2026-0094 was applied when it derived ONE kind, so its instance is
    named without a suffix. It now derives two. Keying the name off today's kind
    count proposed renaming a RUNNING VM — and a hostname change can force
    Terraform to destroy and rebuild it."""
    monkeypatch.setattr(provisioner, "STATE_ROOT", tmp_path)
    legacy = tmp_path / "REQ-OLD"
    legacy.mkdir()
    (legacy / "terraform.tfstate").write_text("{}", encoding="utf-8")

    # The resource that exists keeps its bare name...
    assert omain._resource_name("r1", "oci-apache", "REQ-OLD", "oci-apache") == "r1"
    # ...while a kind added to the same request later is new, and is named apart
    # so it cannot collide with it.
    assert omain._resource_name("r1", "oci-instance", "REQ-OLD", "oci-apache") == "r1-instance"


# --- Workspace layout: the part that can strand real infrastructure ----------

def test_each_resource_gets_its_own_workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(provisioner, "STATE_ROOT", tmp_path)
    a = provisioner.workspace_path("REQ-1", "oci-apache")
    b = provisioner.workspace_path("REQ-1", "oci-instance")
    assert a != b
    assert a.parent == b.parent == tmp_path / "REQ-1"


def test_a_workspace_provisioned_before_this_change_keeps_its_flat_path(tmp_path, monkeypatch):
    """The state file is in the flat directory. Moving to a nested layout would
    hide it, and Terraform would plan to create a SECOND resource while the first
    kept running with nothing tracking it."""
    monkeypatch.setattr(provisioner, "STATE_ROOT", tmp_path)
    legacy = tmp_path / "REQ-OLD"
    legacy.mkdir()
    (legacy / "terraform.tfstate").write_text("{}", encoding="utf-8")
    assert provisioner.workspace_path("REQ-OLD", "oci-apache") == legacy
    assert provisioner.workspace_path("REQ-OLD", "oci-instance") == legacy


def test_existing_workspaces_finds_every_resource_actually_built(tmp_path, monkeypatch):
    monkeypatch.setattr(provisioner, "STATE_ROOT", tmp_path)
    root = tmp_path / "REQ-2"
    for kind in ("oci-apache", "oci-instance"):
        d = root / kind
        d.mkdir(parents=True)
        (d / "terraform.tfstate").write_text("{}", encoding="utf-8")
    # A planned-but-never-applied workspace has no state and is not "built".
    (root / "oci-bucket").mkdir()
    found = provisioner.existing_workspaces("REQ-2")
    assert set(found) == {"oci-apache", "oci-instance"}


def test_existing_workspaces_reports_the_legacy_layout_distinctly(tmp_path, monkeypatch):
    monkeypatch.setattr(provisioner, "STATE_ROOT", tmp_path)
    legacy = tmp_path / "REQ-OLD"
    legacy.mkdir()
    (legacy / "terraform.tfstate").write_text("{}", encoding="utf-8")
    assert provisioner.existing_workspaces("REQ-OLD") == {"": legacy}


def test_nothing_provisioned_is_not_an_error(tmp_path, monkeypatch):
    monkeypatch.setattr(provisioner, "STATE_ROOT", tmp_path)
    assert provisioner.existing_workspaces("REQ-NEVER") == {}


# --- Plan and apply cover the whole stack ------------------------------------

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


def _authorised(monkeypatch):
    """The orchestrator re-verifies the approval, policy and cost independently
    (it never trusts the handoff). Stub those so these tests exercise dispatch."""
    monkeypatch.setattr(omain.httpx, "get", lambda *a, **k: _Resp({"status": "approved"}))
    monkeypatch.setattr(omain.httpx, "post", lambda url, *a, **k: _Resp(
        {"totals": {"monthly": 100.0}} if url.endswith("/api/cost")
        else {"result": {"allow": True}}))


def _stack_payload(**extra):
    return {"contract_version": "1.0",
            "reference": "REQ-STACK", "jira_key": "K-1", "idempotency_key": "idem-stack",
            "resource_kind": "oci-apache",
            "resource_kinds": ["oci-apache", "oci-instance"],
            "policy_input": {"request_type": "create", "deployment_target": "oci",
                             "components": [{"technology_code": "apache", "size": "small"},
                                            {"technology_code": "compute-vm", "size": "small"}]},
            "approved_monthly": 100.0,
            **extra}


def test_planning_covers_every_resource_in_the_stack(monkeypatch):
    planned = []

    def fake_plan(reference, name, tags, kind, sizing):
        planned.append((kind, name))
        return {"summary": f"Plan: 1 to add ({kind})", "output": "", "scan": {}}

    _authorised(monkeypatch)
    monkeypatch.setattr(provisioner, "terraform_plan", fake_plan)
    monkeypatch.setattr(provisioner, "provision_mode", lambda: "plan")
    omain._provisioned.clear()

    from fastapi.testclient import TestClient
    body, sig = _signed(_stack_payload())
    r = TestClient(omain.app).post("/provision", content=body, headers={"X-Signature": sig})
    assert r.status_code == 200, r.text
    assert [k for k, _n in planned] == ["oci-apache", "oci-instance"]
    # ...each under its own name, so they cannot collide in the cloud.
    names = [n for _k, n in planned]
    assert names[0].endswith("-apache") and names[1].endswith("-instance")
    assert len(set(names)) == 2
    assert r.json()["resource_kinds"] == ["oci-apache", "oci-instance"]


def test_a_stack_that_half_plans_is_not_reported_as_ready(monkeypatch):
    """Half a plan is not a plan: letting it through would apply the part that
    worked and leave the request looking successful."""
    def fake_plan(reference, name, tags, kind, sizing):
        if kind == "oci-instance":
            raise provisioner.ProvisionError("subnet is full")
        return {"summary": "Plan: 1 to add", "output": "", "scan": {}}

    _authorised(monkeypatch)
    monkeypatch.setattr(provisioner, "terraform_plan", fake_plan)
    monkeypatch.setattr(provisioner, "provision_mode", lambda: "plan")
    omain._provisioned.clear()

    from fastapi.testclient import TestClient
    body, sig = _signed(_stack_payload(idempotency_key="idem-half"))
    r = TestClient(omain.app).post("/provision", content=body, headers={"X-Signature": sig})
    assert r.status_code == 400
    assert "oci-instance" in r.json()["detail"] and "subnet is full" in r.json()["detail"]


def test_apply_reports_every_resource_it_created(monkeypatch):
    def fake_apply(reference, name, tags, kind, sizing):
        return {"summary": f"Apply complete ({kind})", "outputs": {"name": name}}

    _authorised(monkeypatch)
    monkeypatch.setattr(provisioner, "terraform_apply", fake_apply)
    monkeypatch.setattr(provisioner, "provision_mode", lambda: "apply")
    omain._provisioned.clear()

    from fastapi.testclient import TestClient
    body, sig = _signed(_stack_payload(idempotency_key="idem-apply"))
    r = TestClient(omain.app).post("/apply", content=body, headers={"X-Signature": sig})
    assert r.status_code == 200, r.text
    resources = r.json()["resources"]
    assert [x["kind"] for x in resources] == ["oci-apache", "oci-instance"]
    # The single-resource field still points at the primary for older callers.
    assert r.json()["resource"]["kind"] == "oci-apache"


def test_a_failed_second_apply_still_reports_what_is_already_live(monkeypatch):
    """The first resource is REAL by then. An error that does not name it leaves
    billable infrastructure running with nobody aware it exists."""
    def fake_apply(reference, name, tags, kind, sizing):
        if kind == "oci-instance":
            raise provisioner.ProvisionError("out of capacity")
        return {"summary": "Apply complete", "outputs": {}}

    _authorised(monkeypatch)
    monkeypatch.setattr(provisioner, "terraform_apply", fake_apply)
    monkeypatch.setattr(provisioner, "provision_mode", lambda: "apply")
    omain._provisioned.clear()

    from fastapi.testclient import TestClient
    body, sig = _signed(_stack_payload(idempotency_key="idem-partial"))
    r = TestClient(omain.app).post("/apply", content=body, headers={"X-Signature": sig})
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert "WERE created" in detail and "oci-apache" in detail
    assert "-apache" in detail  # named, so the live resource can be found and cleaned up


# --- Scans across a stack ----------------------------------------------------

def test_a_high_finding_on_any_resource_reaches_the_gate():
    merged = omain._merge_scans([
        {"findings": [], "counts": {"high": 0, "medium": 1, "low": 0}, "high": 0, "ok": True},
        {"findings": [{"severity": "high"}], "counts": {"high": 1, "medium": 0, "low": 0},
         "high": 1, "ok": False},
    ])
    assert merged["high"] == 1 and merged["ok"] is False
    assert merged["counts"]["medium"] == 1  # the other resource's findings are not lost
