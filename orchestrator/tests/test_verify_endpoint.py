"""The orchestrator reads the machines' reports; it does not judge the request.

Separation of authority (ARCHITECTURE.md §4) applies to verification exactly as
it applies to approval. The orchestrator is the only layer holding cloud
credentials, so it is the only one that can read the bucket a machine wrote to.
What a broken machine MEANS for a request — provisioned, failed, retried — is the
portal's decision, and /verify must not make it.

So these tests check two things: that it reports what the machines said
faithfully, and that "there is nothing to check here" can never be confused with
"checked, and found nothing wrong".
"""

from __future__ import annotations

import pytest

import orchestrator.main as omain
from orchestrator import boot_reports, provisioner

HEALTHY = """os_family=rhel
technologies=apache
--- packages ---
httpd-2.4.62-13.0.1.el9_8.5.x86_64
--- services ---
httpd=active
--- ports ---
http_80=200
LISTEN 0 511 0.0.0.0:80 0.0.0.0:*
--- first-boot log ---
PORTAL: first-boot configuration finished
"""

EMPTY_MACHINE = HEALTHY.replace("httpd=active", "httpd=inactive")


@pytest.fixture(autouse=True)
def _apply_mode(monkeypatch):
    """Report expectations only exist where real machines are built."""
    monkeypatch.setattr(provisioner, "provision_mode", lambda: "apply")
    monkeypatch.setenv("OCI_BOOT_REPORT_PAR_URL",
                       "https://objectstorage.me-dubai-1.oraclecloud.com/p/T/n/ns"
                       "/b/shiftleft-boot-reports/o/")


def _reports(monkeypatch, mapping):
    """What the bucket contains, keyed by resource kind."""
    def fake(reference, kind, client=None):
        if kind not in mapping:
            return {}
        value = mapping[kind]
        if isinstance(value, Exception):
            raise value
        return value
    monkeypatch.setattr(boot_reports, "reports_for", fake)


def _verify(payload_kinds, monkeypatch, mapping):
    _reports(monkeypatch, mapping)
    payload = {"reference": "REQ-2026-0199", "resource_kinds": payload_kinds}
    monkeypatch.setattr(omain, "_authorise", lambda body, sig: payload)

    import asyncio

    class _Request:
        headers = {"X-Signature": "sig"}

        async def body(self):
            return b"{}"

    return asyncio.run(omain.verify_boot(_Request()))


# --- Reporting what the machines said -----------------------------------------

def test_a_healthy_machine_is_reported_healthy(monkeypatch):
    result = _verify(["oci-apache"], monkeypatch,
                     {"oci-apache": {"REQ-2026-0199-oci-apache.txt": HEALTHY}})
    assert result["all_ok"] is True and result["settled"] is True
    assert result["broken"] == []


def test_a_broken_machine_is_reported_broken_with_its_own_words(monkeypatch):
    result = _verify(["oci-apache"], monkeypatch,
                     {"oci-apache": {"REQ-2026-0199-oci-apache.txt": EMPTY_MACHINE}})
    assert result["all_ok"] is False
    assert result["broken"] == ["oci-apache"]
    problems = result["resources"][0]["problems"]
    assert any("httpd is inactive" in p for p in problems)
    assert any("REQ-2026-0199-oci-apache.txt" in p for p in problems), (
        "which machine complained has to survive into the answer")


def test_nothing_reported_yet_is_waiting_not_failure(monkeypatch):
    """Terraform returns before cloud-init finishes. Calling that a failure would
    fail every healthy machine ever built."""
    result = _verify(["oci-apache"], monkeypatch, {})
    assert result["settled"] is False
    assert result["waiting"] == ["oci-apache"]
    assert result["broken"] == []


def test_one_broken_machine_condemns_the_stack(monkeypatch):
    result = _verify(["oci-apache", "oci-service-vm"], monkeypatch, {
        "oci-apache": {"a.txt": HEALTHY},
        "oci-service-vm": {"b.txt": EMPTY_MACHINE}})
    assert result["all_ok"] is False
    assert result["checked"] == 2


def test_every_node_of_a_multi_machine_resource_is_read(monkeypatch):
    """A cluster reporting per node must not be judged on whichever one is read
    first — that is how two of three machines go unexamined."""
    result = _verify(["oci-kafka"], monkeypatch, {"oci-kafka": {
        "REQ-2026-0199-oci-kafka-node1.txt": HEALTHY,
        "REQ-2026-0199-oci-kafka-node2.txt": EMPTY_MACHINE}})
    assert result["all_ok"] is False
    assert len(result["resources"][0]["machines"]) == 2


# --- Nothing to check is not the same as nothing wrong ------------------------

def test_a_bucket_is_asked_whether_it_EXISTS(monkeypatch):
    """A bucket has no machine to disbelieve — and that used to mean it was never
    checked at all, so oci-objectstorage was certifiable on no evidence
    whatsoever. It files no boot report, so the proof comes from the resource
    itself."""
    from orchestrator import resource_state
    monkeypatch.setattr(resource_state, "check",
                        lambda kind, name, clients=None: {"state": "ok",
                                                          "detail": "bucket exists"})
    result = _verify(["oci-bucket"], monkeypatch, {})
    assert result["checked"] == 1, "a bucket is no longer exempt from proving itself"
    assert result["all_ok"] is True


def test_a_bucket_that_does_not_exist_is_not_healthy(monkeypatch):
    from orchestrator import resource_state
    monkeypatch.setattr(resource_state, "check",
                        lambda kind, name, clients=None: {"state": "waiting",
                                                          "detail": "no bucket yet"})
    result = _verify(["oci-bucket"], monkeypatch, {})
    assert result["settled"] is False


def test_a_kind_with_no_health_check_is_unproven_not_healthy(monkeypatch):
    """`unknown` must never read as permission. A kind nothing can judge is a
    kind nothing has proven."""
    from orchestrator import resource_state
    monkeypatch.setattr(resource_state, "check",
                        lambda kind, name, clients=None: {"state": "unknown",
                                                          "detail": "no check defined"})
    result = _verify(["oci-bucket"], monkeypatch, {})
    assert result["checked"] == 0
    assert result["resources"][0]["state"] == "not-applicable"
    assert result["resources"][0]["note"], "it has to say WHY it checked nothing"


def test_mock_mode_checks_nothing(monkeypatch):
    """A verification step that failed closed in mock mode would stop the demo
    path this whole project is built to run on."""
    monkeypatch.setattr(provisioner, "provision_mode", lambda: "mock")
    result = _verify(["oci-apache"], monkeypatch, {})
    assert result["checked"] == 0 and result["all_ok"] is True


def test_no_par_configured_checks_nothing(monkeypatch):
    """No machine was ever ASKED to report, so its silence proves nothing."""
    monkeypatch.delenv("OCI_BOOT_REPORT_PAR_URL", raising=False)
    result = _verify(["oci-apache"], monkeypatch, {})
    assert result["checked"] == 0


def test_a_cluster_is_asked_whether_it_is_RUNNING(monkeypatch):
    """OKE's nodes boot an image this project never renders, so no boot report is
    possible — but a cluster can be asked whether it is ACTIVE with its nodes
    present. Exempt from ONE kind of proof is not exempt from proof."""
    from orchestrator import resource_state
    monkeypatch.setattr(resource_state, "check",
                        lambda kind, name, clients=None: {
                            "state": "broken",
                            "detail": "cluster is ACTIVE but has no node pool"})
    result = _verify(["oci-oke"], monkeypatch, {})
    assert result["checked"] == 1
    assert result["all_ok"] is False
    assert "no node pool" in result["resources"][0]["problems"][0]


def test_a_cluster_that_cannot_be_asked_is_not_assumed_healthy(monkeypatch):
    from orchestrator import resource_state

    def explode(kind, name, clients=None):
        raise resource_state.StateUnavailable("no compartment configured")
    monkeypatch.setattr(resource_state, "check", explode)
    result = _verify(["oci-oke"], monkeypatch, {})
    assert result["all_ok"] is False
    assert result["resources"][0]["state"] == "unreadable"


def test_an_unreadable_bucket_is_not_reported_as_healthy(monkeypatch):
    """Losing the ability to check must never look like a clean bill of health."""
    result = _verify(["oci-apache"], monkeypatch,
                     {"oci-apache": boot_reports.BootReportUnavailable("403 denied")})
    assert result["all_ok"] is False
    assert result["resources"][0]["state"] == "unreadable"
    assert result["settled"] is True, "waiting longer will not fix a 403"


# --- It reports; it does not decide -------------------------------------------

def test_the_endpoint_never_changes_a_request(monkeypatch):
    """If /verify could set a status it would be judging its own work — the same
    concentration of authority the architecture forbids for approvals."""
    import inspect
    source = inspect.getsource(omain.verify_boot)
    for forbidden in ("status =", "terraform_apply", "terraform_destroy", "httpx.post"):
        assert forbidden not in source, f"/verify does more than read: {forbidden}"
