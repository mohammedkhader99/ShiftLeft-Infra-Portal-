"""K.1 — the one question Phase K rests on, asked before anything is built on it.

Every note in this repository said "the orchestrator has no route to the
Kubernetes API endpoint". True — and read for months as "a network change is
required", which it is not. The OKE module's own security group admits TCP 6443
from `operator_cidr`, and `operator_cidr` is the WHOLE VCN. The orchestrator
cannot reach the API because it runs in a container outside the VCN, not because
no rule permits it. The portal already builds machines inside that VCN.

So: can one of them reach it? Everything else in Phase K is worthless if the
answer is no, which is why this increment builds a probe and nothing else.

WHAT THESE TESTS CAN AND CANNOT DO. They hold the probe to saying only what it
found. They cannot tell you whether the endpoint is reachable — that needs the
probe run once against the real tenancy, which is the platform owner's to do
(CLAUDE.md: nothing real gets provisioned without them).
"""

from __future__ import annotations

import pytest

from orchestrator import oke_probe

PAR = "https://objectstorage.me-dubai-1.oraclecloud.com/p/tok/n/ns/b/boot/o/probe.txt"


# --- what the machine is told to do ------------------------------------------

def test_it_asks_the_endpoint_on_the_port_the_rule_names():
    """6443 is the port the NSG rule admits from the VCN. Asking on another
    would prove something about a door nobody opened."""
    script = oke_probe.script("10.0.1.15", PAR)

    assert "https://10.0.1.15:6443/version" in script
    assert str(oke_probe.API_PORT) == "6443"


def test_an_endpoint_that_names_its_own_port_is_left_alone():
    assert "https://10.0.1.15:6443/version" in oke_probe.script("10.0.1.15:6443", PAR)


def test_it_refuses_to_render_without_an_endpoint():
    """A probe with nowhere to ask would boot, report nothing useful and cost a
    machine to say so."""
    with pytest.raises(ValueError):
        oke_probe.script("", PAR)


def test_it_carries_no_credential_of_any_kind():
    """THE SECURITY PROPERTY. This script travels in instance metadata, which
    anyone able to read that VM can see. It asks about a route; it must never
    carry a way in.

    The PAR is the one URL it holds, and that is write-only to a bucket holding
    nothing else — the same channel every other machine here reports through.
    """
    script = oke_probe.script("10.0.1.15", PAR)
    lowered = script.lower()

    for forbidden in ("private_key", "fingerprint", "password", "token=",
                      "kubeconfig", "bearer", "api_key", "secret"):
        assert forbidden not in lowered, forbidden


def test_it_does_not_stop_at_the_first_failure():
    """`set -e` would exit before the report is written, and a missing report is
    exactly what an unreachable endpoint produces — indistinguishable from a
    machine that never booted at all."""
    script = oke_probe.script("10.0.1.15", PAR)

    assert "set -u" in script
    assert "set -e" not in script


def test_the_report_is_sent_even_when_the_answer_is_no():
    """A negative answer is the valuable one here. Reporting only success would
    turn "Phase K cannot work" into silence."""
    script = oke_probe.script("10.0.1.15", PAR)

    put = script[script.index("} > $REPORT"):]
    assert "-X PUT" in put and PAR in put
    assert "|| true" in put, "a failed upload must not mask the result"


def test_nothing_is_uploaded_when_no_report_url_is_configured():
    """Same rule as boot_report_url: unconfigured means silent, not broken."""
    assert "-X PUT" not in oke_probe.script("10.0.1.15")


# --- how its answer is read ---------------------------------------------------

def test_an_answer_of_any_kind_means_the_route_exists():
    """401 IS A SUCCESS. The packets arrived, TLS completed and Kubernetes
    replied. Whether this machine may authenticate is K.2's question, and
    treating "not allowed in" as "cannot get there" would send somebody to the
    network team for an IAM problem."""
    for status in ("200", "401", "403"):
        got = oke_probe.verdict(
            f"probe=oke-api\nendpoint=10.0.1.15:6443\nhttp_status={status}\n"
            f"result=reachable\ndetail={{}}\n")
        assert got["result"] == oke_probe.REACHABLE, status
        assert got["http_status"] == status


def test_no_answer_at_all_means_the_phase_is_built_on_sand():
    got = oke_probe.verdict(
        "probe=oke-api\nendpoint=10.0.1.15:6443\nhttp_status=000\n"
        "result=unreachable\ndetail=Connection timed out\n")

    assert got["result"] == oke_probe.UNREACHABLE
    assert "should not be built on it" in got["reason"]
    assert "Connection timed out" in got["detail"]


def test_a_missing_report_is_not_an_unreachable_endpoint():
    """They have different remedies. Nothing reported may mean the machine never
    booted, and blaming the network for a build that did not happen sends
    somebody to the wrong team with a wrong fact."""
    for nothing in (None, "", "   "):
        got = oke_probe.verdict(nothing)
        assert got["result"] == oke_probe.UNKNOWN, repr(nothing)
        assert got["result"] != oke_probe.UNREACHABLE


def test_a_report_that_says_nothing_useful_is_unknown_too():
    got = oke_probe.verdict("probe=oke-api\nendpoint=10.0.1.15:6443\n")

    assert got["result"] == oke_probe.UNKNOWN
    assert "not the same as" in got["reason"]


def test_the_first_line_wins_if_a_key_repeats():
    """A report is appended to as the script runs. Reading the last value would
    let a later line quietly overwrite the answer."""
    got = oke_probe.verdict("result=reachable\nhttp_status=200\nresult=unreachable\n")

    assert got["result"] == oke_probe.REACHABLE


def test_the_reachable_reason_does_not_claim_more_than_it_proved():
    """It establishes a ROUTE. The certificate is not verified and no credential
    is used, so saying anything about trust or access would be a claim this
    probe cannot support."""
    got = oke_probe.verdict("result=reachable\nhttp_status=401\nendpoint=10.0.1.15:6443\n")

    assert "separate question" in got["reason"]
    for overclaim in ("trusted", "authenticated", "deploy"):
        assert overclaim not in got["reason"].lower(), overclaim
