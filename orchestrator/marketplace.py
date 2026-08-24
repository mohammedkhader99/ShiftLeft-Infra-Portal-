"""Reading the OCI Marketplace, so a person can choose from it (C9).

WHAT THIS IS FOR. Every rung the agent climbs — package, vendor repository,
archive, container — exists to work out how to install software on a machine. A
Marketplace image skips the question entirely: the publisher already installed
it. What remains is choosing which listing, and that is a decision this module
never makes.

IT IS READ-ONLY, AND THAT IS THE POINT. Launching a Marketplace image requires
accepting three agreements — Oracle's terms of use, the publisher's terms, and a
PII disclosure that shares information with the publisher. Accepting them is a
legal act. Nothing here accepts anything; it reads what is on offer so a person
can decide, and the portal refuses a listing until that person's acceptance is
on record.

MEASURED IN me-dubai-1 ON 2026-08-24, not recalled:

  * 684 listings, of which 144 are published by Oracle themselves.
  * Oracle publishes NONE of this catalogue's middleware. The apparent matches
    are false: "App Stack for Java" is not a Java runtime, "TimesTen OKE Work
    Node" is not Node.js, and "Oracle Audit Vault" is a database firewall with
    nothing to do with HashiCorp Vault. Substring matching reports four hits and
    every one is wrong.
  * The 16 listings that do match — nginx, Redis, PostgreSQL, Keycloak, MongoDB,
    HAProxy — come from resellers (Cognosys, Hossted, Apps4Rent) and EVERY ONE
    IS PAYGO. Not one is free.
  * RabbitMQ and Kafka have no listing at all, from anyone.

THE LICENCE FEE IS THE PART A COST GATE CANNOT SEE. PAYGO means the publisher
charges by the hour on top of compute, in their own currency, and none of it
appears in the Oracle price list the portal costs from. nginx from Cognosys is
USD 0.15 PER_OCPU_LINEAR — about USD 219 a month on two OCPUs, invisible.
So the rate is read here and carried, rather than a machine being provisioned
against a quote that omitted most of its cost.
"""

from __future__ import annotations

import os
from decimal import Decimal, InvalidOperation


def enabled() -> bool:
    """Off unless switched on. Reading the Marketplace is harmless, but a
    feature nobody asked for should not add a network call to every request."""
    return os.getenv("MARKETPLACE_ENABLED", "false").strip().lower() in (
        "1", "true", "yes", "on")


def _client():  # pragma: no cover - thin SDK seam (mocked in tests)
    import oci

    from orchestrator.cloud_state import _oci_config
    return oci.marketplace.MarketplaceClient(_oci_config())


def _compartment() -> str:
    return (os.getenv("OCI_COMPARTMENT_OCID", "") or "").strip()


def _publisher(listing) -> str:
    return str(getattr(getattr(listing, "publisher", None), "name", "") or "")


def search(term: str, client=None, compartment: str = "") -> list[dict]:
    """Listings whose name contains `term`, for a person browsing.

    A SUBSTRING SEARCH, and its weakness is stated rather than hidden: matching
    "vault" finds Oracle Audit Vault, which is a database firewall. The name and
    publisher are returned precisely so the person choosing can see that for
    themselves — this module narrows the field, it does not decide.

    The Marketplace API's own `name` filter is an exact match and finds nothing
    for ordinary words, which is why the catalogue is enumerated and filtered
    here instead.
    """
    term = (term or "").strip().lower()
    if not term:
        return []
    client = client or _client()
    compartment = compartment or _compartment()

    rows, page = [], None
    while True:
        response = client.list_listings(compartment_id=compartment,
                                        limit=100, page=page)
        rows += list(response.data)
        page = getattr(response, "next_page", None)
        # A guard, not a cap on the answer: the catalogue is under a thousand
        # and an unbounded loop against a paged API is how one bad page becomes
        # an infinite request.
        if not page or len(rows) > 2000:
            break

    out = []
    for listing in rows:
        name = str(getattr(listing, "name", "") or "")
        if term not in name.lower():
            continue
        out.append({
            "listing_ocid": str(getattr(listing, "id", "") or ""),
            "name": name,
            "publisher": _publisher(listing),
            "package_type": str(getattr(listing, "package_type", "") or ""),
            "pricing_types": [str(p) for p in
                              (getattr(listing, "pricing_types", None) or [])],
        })
    return sorted(out, key=lambda r: r["name"])


def _decimal(value) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def resolve(listing_ocid: str, client=None, compartment: str = "",
            package_version: str = "") -> dict | None:
    """Everything needed to launch one listing, pinned, or None.

    PINNED AT THE VERSION RESOLVED HERE, not at whatever `default_package_version`
    says later. A publisher can move that pointer, and then what a proof
    certified and what a request provisions are different images wearing the same
    listing name — the same reasoning that makes a container digest mandatory
    rather than a tag.
    """
    client = client or _client()
    compartment = compartment or _compartment()
    try:
        listing = client.get_listing(listing_id=listing_ocid,
                                     compartment_id=compartment).data
        version = package_version or str(
            getattr(listing, "default_package_version", "") or "")
        if not version:
            return None
        package = client.get_package(listing_id=listing_ocid,
                                     package_version=version,
                                     compartment_id=compartment).data
    except Exception:  # noqa: BLE001 - an unreachable Marketplace is not an error
        return None

    image = str(getattr(package, "image_id", "") or "")
    if not image:
        # ORCHESTRATION and CONTAINER listings have no image to launch. Out of
        # scope deliberately: a Terraform stack from a third party is a much
        # larger thing to accept than one machine image.
        return None

    pricing = getattr(package, "pricing", None)
    return {
        "listing_ocid": listing_ocid,
        "listing_name": str(getattr(listing, "name", "") or ""),
        "publisher": _publisher(listing),
        "package_version": version,
        "image_ocid": image,
        "app_catalog_listing_ocid": str(
            getattr(package, "app_catalog_listing_id", "") or ""),
        "app_catalog_resource_version": str(
            getattr(package, "app_catalog_listing_resource_version", "") or ""),
        "pricing_type": str(getattr(pricing, "type", "") or ""),
        "licence_rate": _decimal(getattr(pricing, "rate", None)),
        "licence_currency": str(getattr(pricing, "currency", "") or ""),
        "licence_strategy": str(getattr(pricing, "pay_go_strategy", "") or ""),
    }


def agreements(listing_ocid: str, package_version: str, client=None,
               compartment: str = "") -> list[dict]:
    """The agreements a PERSON must accept before this listing may be used.

    Returned so the portal can show them and record which acceptance covers
    which listing. Nothing here accepts one: the portal has no business entering
    into a contract, and the PII agreement in particular shares a requester's
    information with a third party.
    """
    client = client or _client()
    compartment = compartment or _compartment()
    try:
        found = client.list_agreements(listing_id=listing_ocid,
                                       package_version=package_version,
                                       compartment_id=compartment).data
    except Exception:  # noqa: BLE001
        return []
    return [{
        "id": str(getattr(a, "id", "") or ""),
        "author": str(getattr(a, "author", "") or ""),
        "prompt": str(getattr(a, "prompt", "") or ""),
    } for a in found]
