"""G2, orchestrator half: boot the proven image, and still prove it.

The speed-up is that software already on the image is not installed again. The
danger is everything adjacent to that sentence:

  * skip an install for software that is NOT on the image, and the machine is
    delivered empty;
  * skip the VERIFICATION too, and a badly captured image ships a broken runtime
    to everyone who asks for it, silently, for as long as the image lives.

The second is the one this file is mostly about. `packages` answers two separate
questions — what to install, and what the report asks the machine about — and
the tempting implementation filters the wrong one.
"""

from __future__ import annotations

import pytest

yaml = pytest.importorskip("yaml")

import orchestrator.main as orch
from orchestrator import configure

GOLDEN = "ocid1.image.oc1..golden"
CHOSEN = "ocid1.image.oc1..requesters-own"

PROFILE = {"code": "dotnet8", "builds_on": "oci/service-vm", "ports": [5000],
           "version_command": "dotnet --version", "expects": "8",
           "rhel": {"packages": ["dotnet-sdk-8.0"], "services": ["myapp"]}}

OTHER = {"code": "nginx", "builds_on": "oci/service-vm", "ports": [80],
         "version_command": "nginx -v 2>&1",
         "rhel": {"packages": ["nginx"], "services": ["nginx"]}}


def payload(*, golden=None, image="", codes=("dotnet8",)):
    components = [{"technology_code": c, "size": "small",
                   "resource_kind": "oci-service-vm"} for c in codes]
    if image:
        components[0]["image"] = image
    body = {"reference": "REQ-2026-0210", "resource_kind": "oci-service-vm",
            "policy_input": {"deployment_target": "oci", "components": components}}
    if golden:
        body["golden_images"] = golden
    return body


@pytest.fixture(autouse=True)
def _profiles(tmp_path, monkeypatch):
    import json
    for prof in (PROFILE, OTHER):
        (tmp_path / f"{prof['code']}.json").write_text(json.dumps(prof))
    monkeypatch.setattr(configure, "GENERATED_PROFILE_DIR", tmp_path)
    monkeypatch.setenv("CONFIG_ENABLED", "true")
    monkeypatch.delenv("CONFIG_PACKAGE_MAP", raising=False)
    monkeypatch.delenv("OCI_COMPUTE_IMAGE_OCID", raising=False)
    # Without a PAR there is no report script at all, so the verification tests
    # below would pass vacuously against a document that checks nothing.
    monkeypatch.setenv(
        "OCI_BOOT_REPORT_PAR_URL",
        "https://objectstorage.me-dubai-1.oraclecloud.com/p/T/n/ns"
        "/b/shiftleft-boot-reports/o/")


# --- which image the machine boots --------------------------------------------

def test_a_proven_image_is_booted_when_one_exists():
    assert orch._image_for(payload(golden={"dotnet8": GOLDEN}),
                           "oci-service-vm") == GOLDEN


def test_the_requesters_own_choice_still_wins():
    """They were shown it, it was priced, and it was approved with the request.
    Substituting a different image would make the approval describe something
    other than what was built."""
    got = orch._image_for(payload(golden={"dotnet8": GOLDEN}, image=CHOSEN),
                          "oci-service-vm")
    assert got == CHOSEN


def test_no_golden_image_changes_nothing():
    assert orch._image_for(payload(), "oci-service-vm") == ""


def test_a_golden_image_for_a_DIFFERENT_technology_is_not_used():
    assert orch._image_for(payload(golden={"redis7": GOLDEN}),
                           "oci-service-vm") == ""


def test_a_malformed_golden_field_is_ignored_not_fatal():
    body = payload()
    body["golden_images"] = "ocid1.image.oc1..not-a-mapping"
    assert orch._image_for(body, "oci-service-vm") == ""


# --- the install is skipped ----------------------------------------------------

def _boot(body):
    return orch._compute_spec(body, "oci-service-vm")["user_data"]


def test_software_already_on_the_image_is_not_installed_again():
    text = _boot(payload(golden={"dotnet8": GOLDEN}))
    assert "install -y dotnet-sdk-8.0" not in text, (
        "the machine reinstalls software the image already carries")
    assert "preinstalled: dotnet-sdk-8.0" in text, (
        "the machine does not say on itself why it installed nothing")


def test_everything_ELSE_on_the_machine_still_installs():
    """A stack is one image plus whatever else was asked for."""
    text = _boot(payload(golden={"dotnet8": GOLDEN}, codes=("dotnet8", "nginx")))
    assert "nginx" in text
    assert "install -y" in text, "the other technology was skipped too"


# --- and the proof is NOT skipped ---------------------------------------------

def test_the_machine_is_still_asked_whether_the_software_is_there():
    """THE test in this file.

    `packages` answers two questions — what to install, and what the report asks
    about. Filtering the preinstalled names out of the list itself would silence
    the check on exactly the software the image exists to provide, and a badly
    captured image would then ship a broken runtime to every requester with
    nothing to show for it."""
    text = _boot(payload(golden={"dotnet8": GOLDEN}))

    # ASSERTED ON A STRING WITH ONE SOURCE. The first version of this checked
    # for `rpm -q dotnet-sdk-8.0`, which the DISCOVERY block also emits — so a
    # plant that removed the package from the report entirely still passed. The
    # `NOT INSTALLED` fallback is written by the packages loop and nothing else.
    assert "dotnet-sdk-8.0 NOT INSTALLED" in text, (
        "the machine is never asked whether the preinstalled software exists, "
        "so a badly captured image would ship a broken runtime in silence")


def test_the_version_command_still_runs():
    text = _boot(payload(golden={"dotnet8": GOLDEN}))
    assert "dotnet --version" in text, (
        "an image could carry the wrong version and nothing would notice")


def test_the_service_is_still_started_and_the_ports_still_checked():
    text = _boot(payload(golden={"dotnet8": GOLDEN}))
    assert "myapp" in text
    assert "5000" in text


# --- the interaction that would deliver an empty machine ----------------------

def test_when_the_requesters_image_wins_NOTHING_is_treated_as_preinstalled():
    """The failure this guards against is silent and total.

    A golden image exists, so the boot script is told the software is already
    there — but the requester's own image outranked it, so the machine actually
    boots a plain OS. It installs nothing, and is delivered empty."""
    text = _boot(payload(golden={"dotnet8": GOLDEN}, image=CHOSEN))

    assert "install -y dotnet-sdk-8.0" in text, (
        "the install was skipped for software that is NOT on the booted image — "
        "this machine is delivered empty")
    assert "preinstalled:" not in text


def test_configure_render_defaults_to_installing_everything():
    """The parameter is additive. Every existing caller passes nothing and must
    keep getting exactly what it got before."""
    text = configure.render([{"technology_code": "dotnet8"}], "rhel", "")
    assert "install -y dotnet-sdk-8.0" in text
