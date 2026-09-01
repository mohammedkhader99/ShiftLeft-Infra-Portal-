"""A boot report is a photograph; this asks whether the machine is running NOW.

REQ-2026-0250 asked for Redis 7, and the question put to the portal afterwards
was "is an actual Redis service running against this request". The boot report
answered half of it beautifully — redis-7.2.14 installed, version verified
against what the catalogue promises, service active, port 6379 opened — and it
answered that at the end of first boot and never again.

    resource_state: {'state': 'unknown',
                     'detail': 'no health check is defined for oci-service-vm'}

Clusters and buckets could be asked. Machines could not, which is odd given they
are most of what this portal builds. A VM that booted perfectly and was stopped
last week still has a boot report saying everything is fine.

ONE CHECK FOR EVERY COMPUTE KIND. `oci-service-vm`, `oci-instance`, `oci-apache`
and `oci-kafka` are all instances with a display name, so a blueprint that
builds machines is covered the day it ships rather than needing a line here.

THE FIRST VERSION OF THIS FILE PASSED THIRTEEN TESTS AND WOULD HAVE FAILED ON
EVERY REAL MACHINE. It matched the display name exactly, and the modules name
their instances

    format("%s-%02d", var.instance_name, count.index + 1)

so the ledger's `test-req-2026-0250-service-vm` is `...-service-vm-01` in OCI.
The tests passed because the fakes carried the name the code expected. Running
the check against the real cloud is what found it, in one call.

So the fakes below name their instances the way the modules do, and several
machines under one environment is a NORMAL answer -- a Kafka quorum is three.

READ-ONLY, like the rest of this module: it lists instances and reads a
lifecycle state.
"""

from __future__ import annotations

import pytest

from orchestrator import resource_state


class FakeInstance:
    def __init__(self, display_name, lifecycle_state):
        self.display_name = display_name
        self.lifecycle_state = lifecycle_state


class FakeCompute:
    def __init__(self, instances):
        self._instances = instances
        self.asked = []

    def list_instances(self, compartment_id=None, **kwargs):
        self.asked.append(compartment_id)

        class Page:
            data = self._instances
        return Page()


def clients_with(*instances):
    """(container_engine, object_storage, compute) — compute is the third."""
    return (None, None, FakeCompute(list(instances)))


@pytest.fixture(autouse=True)
def compartment(monkeypatch):
    monkeypatch.setenv("OCI_COMPUTE_COMPARTMENT_OCID", "ocid1.compartment.test")


@pytest.mark.parametrize("kind", resource_state.COMPUTE_KINDS)
def test_every_kind_that_boots_a_machine_can_be_asked(kind):
    """It was only `unknown` that made REQ-2026-0250 unanswerable, and `unknown`
    blocks certification — so a kind missing here is not merely undocumented."""
    out = resource_state.check(kind, "env-req-vm",
                               clients=clients_with(FakeInstance("env-req-vm-01", "RUNNING")))

    assert out["state"] == "ok", out


def test_a_running_machine_is_healthy():
    out = resource_state.check(
        "oci-service-vm", "test-req-2026-0250-service-vm",
        clients=clients_with(FakeInstance("test-req-2026-0250-service-vm-01", "RUNNING")))

    assert out["state"] == "ok"
    assert "test-req-2026-0250-service-vm-01" in out["detail"], (
        "the check does not name the instance OCI actually carries")


def test_a_machine_still_coming_up_is_waiting_not_broken():
    """PROVISIONING becomes RUNNING on its own. Calling it broken would fail a
    request for being asked too early."""
    out = resource_state.check("oci-service-vm", "vm",
                               clients=clients_with(FakeInstance("vm-01", "PROVISIONING")))

    assert out["state"] == "waiting"


def test_a_stopped_machine_is_broken_rather_than_waiting():
    """STOPPED is the distinction that matters. Nothing will start it without a
    person deciding to, so waiting for it would wait for ever — and a stopped
    machine still carries a boot report saying it is perfectly healthy."""
    out = resource_state.check("oci-service-vm", "vm",
                               clients=clients_with(FakeInstance("vm-01", "STOPPED")))

    assert out["state"] == "broken"
    assert "will not become RUNNING on its own" in out["detail"]


def test_a_machine_that_is_not_there_yet_is_waiting():
    out = resource_state.check("oci-service-vm", "vm", clients=clients_with())

    assert out["state"] == "waiting"
    assert "no instance named vm yet" in out["detail"]


def test_a_terminated_machine_does_not_count_as_present():
    """Terminated instances linger in listings. Treating one as the machine
    would report a destroyed environment as merely 'not RUNNING' rather than
    absent."""
    out = resource_state.check(
        "oci-service-vm", "vm",
        clients=clients_with(FakeInstance("vm-01", "TERMINATED")))

    assert out["state"] == "waiting"
    assert "no instance named vm yet" in out["detail"]


def test_a_quorum_of_machines_is_a_healthy_answer():
    """SEVERAL IS NORMAL. Kafka builds three brokers, named -01, -02 and -03 by
    the same rule. A check that treated more than one as a fault would refuse
    every clustered technology this portal builds."""
    out = resource_state.check(
        "oci-kafka", "env-kafka",
        clients=clients_with(FakeInstance("env-kafka-01", "RUNNING"),
                             FakeInstance("env-kafka-02", "RUNNING"),
                             FakeInstance("env-kafka-03", "RUNNING")))

    assert out["state"] == "ok"
    assert "3 machines RUNNING" in out["detail"]


def test_one_broker_down_fails_the_whole_environment():
    """Two of three running is not two-thirds healthy; it is a quorum with a
    machine missing, and saying "ok" would hide it."""
    out = resource_state.check(
        "oci-kafka", "env-kafka",
        clients=clients_with(FakeInstance("env-kafka-01", "RUNNING"),
                             FakeInstance("env-kafka-02", "STOPPED"),
                             FakeInstance("env-kafka-03", "RUNNING")))

    assert out["state"] == "broken"
    assert "env-kafka-02 is STOPPED" in out["detail"]


def test_two_machines_sharing_one_display_name_is_not_healthy():
    """Indexed names are distinct, so a genuine duplicate means the SAME name
    twice — a re-apply that built a second copy the tenancy is billing for."""
    out = resource_state.check(
        "oci-service-vm", "vm",
        clients=clients_with(FakeInstance("vm-01", "RUNNING"),
                             FakeInstance("vm-01", "RUNNING")))

    assert out["state"] == "broken"
    assert "unaccounted for" in out["detail"]


def test_the_ledgers_name_is_what_callers_pass():
    """THE DEFECT ITSELF. The portal records `<env>-<ref>-service-vm`; OCI
    carries `<env>-<ref>-service-vm-01`. A caller passing the recorded name must
    still find the machine, or every VM reads as missing."""
    out = resource_state.check(
        "oci-service-vm", "test-req-2026-0250-service-vm",
        clients=clients_with(
            FakeInstance("test-req-2026-0250-service-vm-01", "RUNNING")))

    assert out["state"] == "ok", out


def test_a_similarly_named_environment_is_not_swept_in():
    """`vm` must not match `vm-extra`. The suffix is two digits or nothing."""
    out = resource_state.check(
        "oci-service-vm", "vm",
        clients=clients_with(FakeInstance("vm-extra", "RUNNING")))

    assert out["state"] == "waiting"


def test_another_environments_machine_is_not_mistaken_for_this_one():
    out = resource_state.check(
        "oci-service-vm", "vm",
        clients=clients_with(FakeInstance("some-other-vm-01", "RUNNING")))

    assert out["state"] == "waiting"


def test_a_kind_that_boots_nothing_is_still_not_judged_here():
    """`unknown` must keep meaning "cannot judge". Widening this check to kinds
    it knows nothing about would certify them on silence."""
    out = resource_state.check("oci-something-new", "x", clients=clients_with())

    assert out["state"] == "unknown"


def test_it_refuses_rather_than_guesses_without_a_compartment(monkeypatch):
    monkeypatch.delenv("OCI_COMPUTE_COMPARTMENT_OCID", raising=False)
    monkeypatch.delenv("OCI_COMPARTMENT_OCID", raising=False)

    with pytest.raises(resource_state.StateUnavailable):
        resource_state.check("oci-service-vm", "vm", clients=clients_with())


# --- but a running VM is never a substitute for a boot report --------------------

def test_a_machine_is_never_verified_by_its_power_state():
    """THE REGRESSION THIS FEATURE NEARLY CAUSED.

    `verify` falls back to asking the RESOURCE when no boot report is expected —
    right for a bucket or a cluster, which can never file one. But
    `_report_expected` says "no" for several different reasons, and one of them
    is "no OCI_BOOT_REPORT_PAR_URL is configured, so no machine was asked".

    Adding compute kinds to CHECKABLE made that path answer "the VM is RUNNING",
    and a RUNNING VM is not evidence that anything installed on it. That is the
    exact fallacy boot reports exist to close: "Terraform exiting zero says the
    VM exists, not that anything was installed on it." It would have let a
    machine that came up empty certify — through the gate built to stop it.

    So `verify` must not substitute for a compute kind. The check is still there
    and still useful; it answers "is it running NOW", which is a different
    question from "did it become what was promised".
    """
    from pathlib import Path

    source = Path("orchestrator/main.py").read_text(encoding="utf-8")
    at = source.index("# No machine to ask — so ask the RESOURCE.")
    block = source[at:at + 2000]

    assert "resource_state.COMPUTE_KINDS" in block, (
        "verify() will answer for a machine from its power state, which is not "
        "evidence that the software installed")
