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
KNOWN_EXCEPTIONS: dict[str, str] = {
    "oci/oke": (
        "Builds its own VCN because there is nothing to be compliant with yet: "
        "the tenancy has no hub, no spoke VCNs and no IPAM. Remove this exception "
        "once a landing-zone spoke exists and the module consumes a resolved "
        "network contract instead."
    ),
}

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


def test_the_oke_default_cidr_avoids_the_ranges_already_in_use():
    """10.0.0.0/16 is the OCI default and therefore the collision: three VCNs in
    this tenancy already use it. A default that overlaps is worse than no default,
    because the plan succeeds and the cluster is simply unroutable."""
    variables = (TERRAFORM_ROOT / "oci" / "oke" / "variables.tf").read_text(encoding="utf-8")
    block = variables.split('variable "default_vcn_cidr"', 1)[1]
    default = re.search(r'default\s*=\s*"([^"]+)"', block).group(1)
    assert default != "10.0.0.0/16", "the OKE default VCN CIDR collides with three existing VCNs"
    assert default.startswith("10.56."), (
        "expected a range inside the tenancy's 10.56 space; anything else needs "
        "checking against the VCNs actually deployed")
