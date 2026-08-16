"""Workload modules create no networking (LANDING-ZONE §7.1).

The enterprise architecture draws the line here:

    "CI policy gate: any module under modules/workload/** that declares
     oci_core_vcn, oci_core_subnet, oci_core_route_table,
     oci_core_internet_gateway, oci_core_nat_gateway, oci_core_service_gateway,
     oci_core_drg*, or oci_core_security_list fails the build.
     That single automated rule is worth more than any amount of documentation."

The reasoning, verified against the live tenancy rather than taken on faith:
three VCNs there already sit on 10.0.0.0/16, so a fourth workload-created VCN
would overlap them — and overlapping ranges can never be routed to each other or
to on-premises. A VCN per workload also runs into the DRG attachment ceiling and
loses NSG-to-NSG rules, which only work inside one VCN.

EXCEPTIONS ARE NAMED, NOT SKIPPED. A module listed below is a recorded piece of
architectural debt with a stated reason and a condition for removal. Silently
excluding it would leave the portal looking compliant while it is not.
"""

import re
from pathlib import Path

import pytest

TERRAFORM_ROOT = Path(__file__).resolve().parents[1] / "terraform"

# Resource types a workload module must never declare.
FORBIDDEN_PREFIXES = (
    "oci_core_vcn",
    "oci_core_subnet",
    "oci_core_route_table",
    "oci_core_internet_gateway",
    "oci_core_nat_gateway",
    "oci_core_service_gateway",
    "oci_core_drg",
    "oci_core_security_list",
)

# Module path -> why it is allowed to create networking, and what removes the
# exception. Adding an entry must be a decision someone defends in review.
# Empty since 16 Aug 2026. oci/oke was the last entry: it built its own VCN,
# subnets, gateways and route tables "because there is nothing to be compliant
# with yet". There was — the network team had provisioned four purpose-built
# Kubernetes subnets in AI-ShiftLeft-DEV-VCN, and the module ignored them.
#
# What the exception cost is visible in the tenancy: seven VCNs, five on
# overlapping CIDRs, including an oke-vcn-quick-* built this exact way and now
# unable to peer with anything.
KNOWN_EXCEPTIONS: dict[str, str] = {}

_RESOURCE = re.compile(r'^resource\s+"([a-z0-9_]+)"', re.M)


def _modules_creating_networking() -> dict[str, set[str]]:
    """Module path -> the forbidden resource types it declares."""
    found: dict[str, set[str]] = {}
    for tf in TERRAFORM_ROOT.rglob("*.tf"):
        module = tf.parent.relative_to(TERRAFORM_ROOT).as_posix() or "."
        for rtype in _RESOURCE.findall(tf.read_text(encoding="utf-8")):
            if rtype.startswith(FORBIDDEN_PREFIXES):
                found.setdefault(module, set()).add(rtype)
    return found


def test_no_workload_module_creates_networking():
    """The gate. A new blueprint that builds a VCN fails here, at the point it is
    written, rather than in the tenancy six months later."""
    offenders = {m: r for m, r in _modules_creating_networking().items()
                 if m not in KNOWN_EXCEPTIONS}
    assert not offenders, (
        "These modules create networking, which workload modules must not do:\n"
        + "\n".join(f"  {m}: {', '.join(sorted(r))}" for m, r in sorted(offenders.items()))
        + "\n\nConsume a resolved network contract from the landing zone instead. "
          "If a dedicated VCN is genuinely the right answer, add the module to "
          "KNOWN_EXCEPTIONS with the reason and what would remove it."
    )


@pytest.mark.parametrize("module", sorted(KNOWN_EXCEPTIONS))
def test_a_recorded_exception_is_still_actually_an_exception(module):
    """Stops the list rotting. A module that no longer creates networking should
    lose its exception, so the gate covers it again."""
    assert module in _modules_creating_networking(), (
        f"{module} no longer creates networking — remove it from KNOWN_EXCEPTIONS "
        "so the gate protects it."
    )


def test_the_modules_that_are_compliant_stay_compliant():
    """The ones that already consume an existing subnet. Named so a regression in
    any of them is a named failure rather than a count changing."""
    creating = _modules_creating_networking()
    for module in (".", "aws", "oci/apache-httpd", "oci/service-vm", "oci/kafka"):
        assert module not in creating, f"{module} started creating networking"


def test_oke_chooses_no_addresses_at_all():
    """The default-CIDR test that stood here asked which range OKE picks when
    nobody says. It picks none: the VCN and all five subnets arrive as OCIDs, and
    the module reads the VCN's CIDR rather than choosing one.

    That question mattered while the module allocated its own space — the OCI
    default of 10.0.0.0/16 collides with three VCNs in this tenancy, and a plan
    over an overlapping range succeeds while producing a cluster that cannot
    route. It is now unanswerable, which is the point."""
    variables = (TERRAFORM_ROOT / "oci" / "oke" / "variables.tf").read_text(encoding="utf-8")
    assert 'variable "default_vcn_cidr"' not in variables
    assert 'variable "oke_vcn_cidr"' not in variables
    for required in ("vcn_id", "api_subnet_id", "node_subnet_id",
                     "pod_subnet_id", "lb_subnet_id", "bastion_subnet_id"):
        assert f'variable "{required}"' in variables, (
            f"the module cannot consume a given network without {required}")


def test_oke_reads_the_vcn_cidr_rather_than_choosing_it():
    """The NSG rules need the VCN's range. Reading it from the VCN it was given
    keeps the rules true whatever the network team allocated per tier."""
    body = "".join(p.read_text(encoding="utf-8")
                   for p in (TERRAFORM_ROOT / "oci" / "oke").glob("*.tf"))
    assert 'data "oci_core_vcn" "provided"' in body
    assert "data.oci_core_vcn.provided.cidr_block" in body
    assert "cidrsubnet(" not in body, "it is still carving subnet ranges"