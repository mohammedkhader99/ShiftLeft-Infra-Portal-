"""Give the portal's VM subnet a route to the internet, and nothing else.

WHY THIS EXISTS
---------------
AI-ShiftLeft-DEV-VCN had a service gateway and no NAT gateway. That is enough for
Oracle Linux, whose yum mirrors sit inside the Oracle Services Network, and not
enough for Ubuntu, whose apt repositories are on the public internet. The two
behave identically right up to the moment a package is installed, so the portal
built Ubuntu machines, Terraform exited zero, and the machines had nothing on
them. REQ-2026-0134 proved it in one request: Apache on Oracle Linux installed
and served on :80, while nginx on Ubuntu reported `nginx NOT INSTALLED`.

WHAT IT CHANGES
---------------
All six subnets in the VCN share the default route table, so adding 0.0.0.0/0
there would hand outbound internet to the database and Kubernetes subnets too.
Instead this creates a route table used by the VM subnet ALONE:

    1. a NAT gateway on the VCN                      (outbound only; nothing
                                                      can open a connection in)
    2. a route table carrying BOTH rules —           (the service-gateway rule is
       Oracle Services Network -> service gateway     carried over deliberately:
       0.0.0.0/0                -> NAT gateway        without it the machines lose
                                                      Object Storage, which is how
                                                      they report on themselves)
    3. the VM subnet pointed at that route table

The database and Kubernetes subnets keep the default route table exactly as it
is. Reversing this is three steps in the other order: point the subnet back at
the default route table, delete the route table, delete the NAT gateway.

USAGE
-----
    python ops/oci_enable_internet_egress.py            # report only, changes nothing
    python ops/oci_enable_internet_egress.py --apply    # make the change

Idempotent: run it twice and the second run reports that everything already
exists and changes nothing.
"""

from __future__ import annotations

import argparse
import os
import sys

import oci

from orchestrator.cloud_state import _oci_config

NAT_NAME = "AI-ShiftLeft-DEV-NATGW"

# Which subnet to act on, by display name. Defaults to the one the portal builds
# VMs in; --subnet names another in the SAME VCN.
#
# Generalised on 15 Aug 2026 for the OKE worker subnet. OKE worker nodes pull
# container images, and over a service gateway alone they can reach OCIR and
# nothing else — no Docker Hub, no quay.io, no ghcr.io. That is the same
# condition that produced an empty Ubuntu machine this morning, and it would fail
# a cluster the same way.


def _find(items, name):
    for item in items:
        if item.display_name == name and item.lifecycle_state not in (
                "TERMINATED", "TERMINATING"):
            return item
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true",
                        help="make the change; without it, only report")
    parser.add_argument("--subnet", default="",
                        help="display name of the subnet to act on; defaults to "
                             "the compute subnet the portal builds VMs in")
    args = parser.parse_args()

    subnet_ocid = os.getenv("OCI_COMPUTE_SUBNET_OCID", "")
    if not subnet_ocid:
        print("OCI_COMPUTE_SUBNET_OCID is not set — nothing to act on.")
        return 2

    net = oci.core.VirtualNetworkClient(_oci_config())
    subnet = net.get_subnet(subnet_ocid).data
    if args.subnet:
        # Looked up WITHIN the compute subnet's VCN, so a name typed by hand can
        # never reach a different network by accident.
        found = [s for s in oci.pagination.list_call_get_all_results(
                     net.list_subnets, subnet.compartment_id, vcn_id=subnet.vcn_id).data
                 if s.display_name == args.subnet]
        if not found:
            print(f"No subnet called {args.subnet!r} in this VCN. It must be one of:")
            for s in oci.pagination.list_call_get_all_results(
                    net.list_subnets, subnet.compartment_id, vcn_id=subnet.vcn_id).data:
                print(f"   {s.display_name}")
            return 2
        subnet = found[0]
    vcn = net.get_vcn(subnet.vcn_id).data
    compartment = subnet.compartment_id
    # Derived from the subnet so two subnets can never share one table and one
    # change silently move both.
    route_table_name = f"{subnet.display_name}-RT"

    print(f"VCN     : {vcn.display_name} ({vcn.cidr_block})")
    print(f"subnet  : {subnet.display_name} ({subnet.cidr_block})")
    print(f"mode    : {'APPLY — this will change the tenancy' if args.apply else 'REPORT ONLY'}")
    print()

    # --- 1. the NAT gateway ---------------------------------------------------
    gateways = net.list_nat_gateways(compartment, vcn_id=vcn.id).data
    nat = _find(gateways, NAT_NAME) or (gateways[0] if gateways else None)
    if nat:
        print(f"NAT gateway     : exists — {nat.display_name} ({nat.lifecycle_state})")
    elif not args.apply:
        print(f"NAT gateway     : WOULD CREATE {NAT_NAME}")
    else:
        nat = net.create_nat_gateway(oci.core.models.CreateNatGatewayDetails(
            compartment_id=compartment, vcn_id=vcn.id, display_name=NAT_NAME,
            block_traffic=False)).data
        nat = oci.wait_until(net, net.get_nat_gateway(nat.id),
                             "lifecycle_state", "AVAILABLE", max_wait_seconds=300).data
        print(f"NAT gateway     : CREATED {nat.display_name}")

    # --- 2. the route table, carrying the service-gateway rule forward --------
    service_gateways = net.list_service_gateways(compartment, vcn_id=vcn.id).data
    if not service_gateways:
        print("no service gateway found — refusing to build a route table that "
              "would cut the machines off from Object Storage.")
        return 1
    sgw = service_gateways[0]

    # The service rule is copied from the table in use rather than looked up
    # fresh, so this cannot quietly narrow what the subnet can already reach.
    current = net.get_route_table(subnet.route_table_id).data
    service_rules = [r for r in current.route_rules
                     if r.destination_type == "SERVICE_CIDR_BLOCK"]
    print(f"service rules carried forward: "
          f"{[r.destination for r in service_rules] or 'NONE FOUND'}")
    if not service_rules:
        print("  the subnet's current table has no service rule to carry — stopping.")
        return 1

    tables = net.list_route_tables(compartment, vcn_id=vcn.id).data
    table = _find(tables, route_table_name)
    if table:
        print(f"route table     : exists — {table.display_name}")
    elif not args.apply:
        print(f"route table     : WOULD CREATE {route_table_name} with "
              f"{len(service_rules)} service rule(s) + 0.0.0.0/0 -> NAT")
    else:
        rules = [oci.core.models.RouteRule(
            destination=r.destination, destination_type="SERVICE_CIDR_BLOCK",
            network_entity_id=sgw.id,
            description="Oracle Services Network — Object Storage, yum mirrors")
            for r in service_rules]
        rules.append(oci.core.models.RouteRule(
            destination="0.0.0.0/0", destination_type="CIDR_BLOCK",
            network_entity_id=nat.id,
            description="Outbound internet for OS package repositories (apt)"))
        table = net.create_route_table(oci.core.models.CreateRouteTableDetails(
            compartment_id=compartment, vcn_id=vcn.id,
            display_name=route_table_name, route_rules=rules)).data
        print(f"route table     : CREATED {table.display_name} with {len(rules)} rules")

    # --- 3. point the subnet at it -------------------------------------------
    if table and subnet.route_table_id == table.id:
        print(f"subnet          : already uses {route_table_name}")
    elif not args.apply:
        print(f"subnet          : WOULD MOVE from '{current.display_name}' "
              f"to '{route_table_name}'")
    elif table:
        net.update_subnet(subnet.id, oci.core.models.UpdateSubnetDetails(
            route_table_id=table.id))
        print(f"subnet          : MOVED to {route_table_name}")

    # --- what the other subnets see ------------------------------------------
    # Every other subnet, with what its routing ACTUALLY says.
    #
    # This used to print "keep the default route table and have no route to the
    # internet" over a bare list, asserting a fact it had not checked. Run twice
    # — once for the worker subnet, once for the pod subnet — and the second run
    # confidently reported the worker subnet as having no internet thirty seconds
    # after giving it some.
    #
    # Read from the route table via the same code the portal uses to decide which
    # operating systems it may offer, so the report and the decision can never
    # disagree about the same subnet.
    from orchestrator import network_egress

    others = [s for s in oci.pagination.list_call_get_all_results(
        net.list_subnets, compartment, vcn_id=vcn.id).data
        if s.id != subnet.id]
    print()
    print(f"unchanged by this run — {len(others)} other subnet(s), as they stand:")
    for s in others:
        state = network_egress.egress_for_subnet(s.id, net)
        if not state["known"]:
            reach = "could not read its routing"
        elif state["internet"]:
            reach = f"HAS internet via {state['route_table']}"
        else:
            reach = f"no internet ({state['route_table']})"
        print(f"   {s.display_name:34} {reach}")
    if not args.apply:
        print("\nNothing was changed. Re-run with --apply to make it so.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
