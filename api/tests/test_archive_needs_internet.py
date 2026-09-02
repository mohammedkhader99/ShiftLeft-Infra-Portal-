"""An archive install needs the public internet, and the portal must know first.

The existing egress check is keyed on OS FAMILY: Oracle Linux reaches its own
mirrors through a service gateway, Ubuntu and SUSE need the public internet. That
is the right question for packages and blind to the one archive installs raise —
keycloak on Oracle Linux installs its java dependency from Oracle's mirrors
perfectly, and then cannot reach github.com.

Found by adversarial review of C6b, 2026-08-22. Without this the portal spends a
real sandbox VM, eight minutes and a machine, to be told what the subnet's route
table already said. The failure was at least honest — the machine reported
`PORTAL FAILURE: archive fetch failed` and the profile was withdrawn — but a
refusal it could have made up front is cheaper than a proof, and this project's
standing rule is that validation tells the truth FIRST.

The need is declared where it is known: the blueprint manifest for shipped
recipes, and the archive itself for agent-written profiles. The API learns it
over the signed channel with the rest of the capabilities, because the API image
has no orchestrator code and no view of the profile store.
"""

from __future__ import annotations

import pytest

from api import blueprint_capabilities, network_egress

NO_INTERNET = {"known": True, "subnet_name": "AI-ShiftL-DEV-VM-APP-SUBNET",
               "route_table": "rt", "internet": False, "oracle_services": True,
               "families": ["rhel"], "reason": ""}
INTERNET = {**NO_INTERNET, "internet": True, "families": ["debian", "rhel", "suse"]}
UNKNOWN = {"known": False, "subnet_name": "", "route_table": "", "internet": False,
           "oracle_services": False, "families": [], "reason": "could not be read"}


@pytest.fixture(autouse=True)
def _clean():
    network_egress.reset()
    blueprint_capabilities.reset()
    yield
    network_egress.reset()
    blueprint_capabilities.reset()


def fetcher(answer):
    return lambda: answer


# --- the subnet question ------------------------------------------------------

def test_a_subnet_with_a_nat_reaches_the_internet():
    assert network_egress.reaches_internet(fetcher(INTERNET)) is True


def test_a_service_gateway_only_subnet_does_not():
    """The exact shape that installs every Oracle RPM and no archive."""
    assert network_egress.reaches_internet(fetcher(NO_INTERNET)) is False


def test_an_unreadable_answer_does_not_refuse_everything():
    """Matching can_install: refusing every technology because a lookup failed
    would take the portal down to prevent a failure the machine's own boot
    report catches anyway."""
    assert network_egress.reaches_internet(fetcher(UNKNOWN)) is True


# --- the per-technology need --------------------------------------------------

def test_the_orchestrator_declaration_reaches_the_api():
    """Asked over the signed channel, never worked out here: the API image has
    no orchestrator code and cannot see the generated profile store."""
    blueprint_capabilities.refresh(lambda: [{
        "ref": "oci/service-vm", "target": "oci", "resource_kind": "oci-service-vm",
        "builds": ["nginx", "keycloak"], "needs_internet": ["keycloak"],
        "os_families": ["rhel"],
    }])
    assert blueprint_capabilities.needs_internet("keycloak") is True
    assert blueprint_capabilities.needs_internet("nginx") is False


def test_an_unknown_technology_does_not_need_it():
    """Unlike families_for, absent knowledge here must NOT refuse — it would
    block the whole catalogue on a subnet whose egress could not be read."""
    assert blueprint_capabilities.needs_internet("anything") is False


# --- the refusal, at the same gate that catches the family case ---------------

def test_the_form_refuses_keycloak_on_a_subnet_with_no_route_out(monkeypatch):
    from api import component_options

    blueprint_capabilities.refresh(lambda: [{
        "ref": "oci/service-vm", "target": "oci", "resource_kind": "oci-service-vm",
        "builds": ["keycloak"], "needs_internet": ["keycloak"],
        "os_families": ["rhel"],
    }])
    monkeypatch.setattr(component_options, "_fetch_egress", fetcher(NO_INTERNET))
    assert network_egress.reaches_internet(fetcher(NO_INTERNET)) is False


def test_the_refusal_says_why_and_what_to_do():
    """A refusal that only says no teaches nobody anything and gets worked
    around. It must distinguish itself from the OS-family refusal, because the
    cause and the fix are different: the OS is fine here."""
    message = network_egress.archive_guidance("keycloak", fetcher(NO_INTERNET))

    assert "keycloak" in message
    assert "release archive" in message or "archive" in message
    assert "NAT gateway" in message, "it did not say what would fix it"
    assert "AI-ShiftL-DEV-VM-APP-SUBNET" in message, "it did not say where"
    assert "package" in message, (
        "it did not say that package-based software still works here — a "
        "requester reading this must not conclude the whole subnet is broken")


# --- and before a machine is ever spent ---------------------------------------

def test_the_proof_is_refused_before_publishing_or_building():
    """THE saving. The route table already answers this; a sandbox VM costs
    eight minutes and real money to answer it again."""
    from api import autobuild
    from api.ai_blueprint import Draft

    proposal = Draft(candidate="keycloak", kind="vm-service", files={
        "generated/profiles/keycloak.json":
            '{"code": "keycloak", "archive": {"url": "https://x/k.tar.gz"}}'})
    published, proved = [], []

    result = autobuild._ensure_vm_service(
        "keycloak", None, proposal, target="oci",
        shipped=lambda c: {"ref": "oci/service-vm", "target": "oci",
                           "resource_kind": "oci-service-vm"},
        run_proof=lambda s, m: proved.append(1),
        publish=published.append, certify=lambda m, r: None,
        withdraw=lambda f: None, reachable=lambda: False)

    assert result.status == "refused"
    assert proved == [], "it built a machine that could never fetch the software"
    assert published == [], "it published a profile it knew could not work"
    assert "no route" in result.detail
    assert "Nothing was built" in result.detail


def test_a_package_profile_is_unaffected_by_the_subnet_having_no_nat():
    """Oracle Linux packages come from a service gateway. A profile with no
    archive must still be provable on a subnet with no internet — refusing it
    would take the whole catalogue offline to guard one install method."""
    from api import autobuild
    from api.ai_blueprint import Draft

    proposal = Draft(candidate="haproxy", kind="vm-service", files={
        "generated/profiles/haproxy.json":
            '{"code": "haproxy", "rhel": {"packages": ["haproxy"]}}'})
    proved = []

    autobuild._ensure_vm_service(
        "haproxy", None, proposal, target="oci",
        shipped=lambda c: {"ref": "oci/service-vm", "target": "oci",
                           "resource_kind": "oci-service-vm"},
        run_proof=lambda s, m: proved.append(1) or _passed(),
        publish=lambda f: list(f), certify=lambda m, r: None,
        withdraw=lambda f: None, reachable=lambda: False)

    assert proved == [1], "a package install was refused for lacking internet"


def _passed():
    from api.proof import ProofOutcome
    return ProofOutcome("PROOF-X", "passed", "built, verified, destroyed")
