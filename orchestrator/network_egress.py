"""What the network a machine is built in can actually REACH.

WHY THIS EXISTS. A first-boot script installs packages. Whether it can depends on
where the machine's packages come from and what its subnet is allowed to reach,
and those two facts had never met:

    Oracle Linux  yum mirrors sit INSIDE the Oracle Services Network, so a
                  service gateway is enough. No internet needed.
    Ubuntu        apt repositories are on the public internet. Nothing in the
                  Oracle Services Network mirrors them, so without a NAT or
                  internet gateway the install cannot happen at all.

REQ-2026-0134 built both at once, on the same subnet, and showed the difference
perfectly: Apache installed httpd 2.4.62 and served on :80, while nginx on Ubuntu
reported `nginx NOT INSTALLED`. The subnet had a service gateway and no NAT. The
portal had offered Ubuntu, priced it, had it approved, and built a machine that
could never have worked.

So the portal now asks this question BEFORE offering an image, and refuses the
combination with a reason and a way forward rather than building it and hoping.

READ-ONLY. It inspects route tables and reports; it changes nothing.
"""

from __future__ import annotations

import os

# Where each OS family gets its packages from, and therefore what its machines
# need to be able to reach. This is the whole domain fact the rule rests on.
FAMILY_NEEDS = {
    # Oracle's yum mirrors are reachable over the service gateway.
    "rhel": "oracle-services",
    # Ubuntu/Debian archives are public-internet only.
    "debian": "internet",
    # SUSE's repositories are likewise public.
    "suse": "internet",
}

# A service gateway can be scoped to Object Storage ALONE, which is enough for a
# machine to file its boot report and nowhere near enough to install a package.
# The two look identical from a distance, so the label is checked rather than the
# mere presence of a gateway.
_ALL_SERVICES = ("all-", "-services-in-oracle-services-network")


def _client():  # pragma: no cover - thin SDK seam (mocked in tests)
    import oci

    from orchestrator.cloud_state import _oci_config
    return oci.core.VirtualNetworkClient(_oci_config())


def _reaches_all_oracle_services(destination: str) -> bool:
    label = (destination or "").strip().lower()
    return all(part in label for part in _ALL_SERVICES)


def egress_for_subnet(subnet_ocid: str, client=None) -> dict:
    """What this subnet can reach, and which OS families can therefore install.

    Never raises. A lookup that fails returns known=False, and every caller must
    treat that as "cannot judge" rather than "nothing is restricted" — reading
    the two as the same thing is how the OS filter was inert in production for a
    fortnight while passing every test.
    """
    if not subnet_ocid:
        return _unknown("no compute subnet is configured, so its network cannot "
                        "be inspected")
    try:
        client = client or _client()
        subnet = client.get_subnet(subnet_ocid).data
        table = client.get_route_table(subnet.route_table_id).data
        rules = list(table.route_rules or [])
    except Exception as exc:  # noqa: BLE001 - SDK raises many types
        return _unknown(f"the subnet's routing could not be read: {exc}")

    internet = False
    oracle_services = False
    for rule in rules:
        entity = (getattr(rule, "network_entity_id", "") or "").lower()
        destination = getattr(rule, "destination", "") or ""
        if ".natgateway." in entity or ".internetgateway." in entity:
            # A default route through either is a way out to the public internet.
            if destination in ("0.0.0.0/0", "::/0"):
                internet = True
        elif ".servicegateway." in entity and _reaches_all_oracle_services(destination):
            oracle_services = True

    reaches = set()
    if internet:
        reaches.add("internet")
        # Anything on the public internet includes Oracle's mirrors.
        reaches.add("oracle-services")
    if oracle_services:
        reaches.add("oracle-services")

    families = sorted(f for f, need in FAMILY_NEEDS.items() if need in reaches)
    return {
        "known": True,
        "subnet_name": getattr(subnet, "display_name", ""),
        "route_table": getattr(table, "display_name", ""),
        "internet": internet,
        "oracle_services": oracle_services,
        # The families whose packages this subnet can actually fetch.
        "families": families,
        "reason": "",
    }


def _unknown(reason: str) -> dict:
    return {"known": False, "subnet_name": "", "route_table": "", "internet": False,
            "oracle_services": False, "families": [], "reason": reason}


def report(client=None) -> dict:
    """Egress for the subnet the portal builds its VMs in."""
    return egress_for_subnet(os.getenv("OCI_COMPUTE_SUBNET_OCID", ""), client)


def guidance(family: str, egress: dict) -> str:
    """Why this OS cannot be built here, and what to do about it — in a sentence
    a requester can act on.

    A refusal that only says no teaches nobody anything and gets worked around.
    """
    need = FAMILY_NEEDS.get((family or "").strip().lower())
    where = egress.get("subnet_name") or "this environment's subnet"
    if need == "internet":
        return (f"Ubuntu and SUSE images install their software from repositories "
                f"on the public internet, and {where} has no route to it. Oracle "
                f"Linux images use Oracle's own mirrors, which this network can "
                f"reach — choose one of those, or ask for a NAT gateway to be "
                f"added to {where}.")
    if need == "oracle-services":
        return (f"Oracle Linux images install their software from Oracle's mirrors, "
                f"and {where} cannot reach them: it has no service gateway covering "
                f"all Oracle services, and no route to the internet. Ask for one to "
                f"be added before requesting a machine on this network.")
    return (f"{where} cannot reach the package repositories this operating system "
            f"installs from.")
