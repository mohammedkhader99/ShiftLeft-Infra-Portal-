"""No module may demand a public IP, because every subnet here forbids one.

REQ-2026-0150 failed with "2 nodes(s) register timeout", which sent the
investigation to NSG rules, security lists and route tables. All three were
correct. OCI's work request errors said what actually happened:

    Public IP addresses are prohibited in this subnet
    {ocid1.subnet...aaes6fpq}     <- AI-ShiftL-DEV-VM-APP-SUBNET

bastion.tf asked for a public IP, the VNIC was refused, the apply died, and the
node pool's registration window expired as a downstream symptom. The visible
error pointed nowhere near the cause.

The modules used to build their own subnets and could make them public. They now
consume subnets the network team provisioned, and every one is private
(prohibit_public_ip_on_vnic = true) — so a public IP is not a preference here,
it is an instance that cannot launch.
"""

from __future__ import annotations

import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
TERRAFORM = ROOT / "orchestrator" / "terraform"
TF_FILES = sorted(TERRAFORM.rglob("*.tf"))


def test_there_are_files_to_check():
    """A moved directory would make every test below pass vacuously."""
    assert len(TF_FILES) > 10, len(TF_FILES)


@pytest.mark.parametrize("path", TF_FILES, ids=lambda p: f"{p.parent.name}/{p.name}")
def test_no_module_hard_codes_a_public_ip(path):
    """`assign_public_ip = true` cannot succeed in this tenancy's subnets."""
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if line.strip().startswith("#"):
            continue
        if re.search(r"assign_public_ip\s*=\s*true", line):
            raise AssertionError(
                f"{path.relative_to(ROOT)}:{number} requests a public IP. Every "
                f"subnet the portal builds into is private, so OCI refuses the "
                f"VNIC and the whole apply fails — with an error that names the "
                f"subnet, not this line.")


def test_the_bastion_is_explicitly_private():
    """Asserted by name, not just by the absence of `true`: a bastion with the
    field REMOVED would inherit whatever the provider defaults to, and the
    default is what caused this."""
    text = (TERRAFORM / "oci" / "oke" / "bastion.tf").read_text(encoding="utf-8")
    assert re.search(r"assign_public_ip\s*=\s*false", text), (
        "bastion.tf must say assign_public_ip = false explicitly")


def test_the_reason_is_recorded_where_someone_would_change_it_back():
    """The next person to want a reachable bastion will edit this line. The
    error they would otherwise get names a subnet, not a bastion."""
    text = (TERRAFORM / "oci" / "oke" / "bastion.tf").read_text(encoding="utf-8")
    assert "prohibited in this subnet" in text
