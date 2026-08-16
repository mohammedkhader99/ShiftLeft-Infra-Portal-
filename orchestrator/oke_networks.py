"""Which network an OKE cluster is built in, per environment tier.

ONE VCN PER TIER is the platform owner's plan (16 Aug 2026), so this is a map and
not five settings. The module no longer chooses any address: it is handed a VCN
and five subnets, and reads the VCN's range for its security rules.

A TIER WITH NO MAPPING IS REFUSED. Never defaulted, never falling back to another
tier's network — a production cluster silently built into the development VCN
would look exactly like success, and this is the only place that can prevent it.

Configured as JSON in OCI_OKE_NETWORKS:

    {"Development": {"vcn": "ocid1.vcn...", "api": "ocid1.subnet...",
                     "node": "...", "pod": "...", "lb": "...",
                     "bastion": "..."}}

Why not five environment variables: they would describe one tier, and the second
tier would need five more with no rule saying which belong together. A tier is
either mapped completely or it is not mapped.
"""

from __future__ import annotations

import json
import os

# Every key a tier's entry must carry. Partial entries are refused rather than
# filled in: a cluster built with four of five subnets is a cluster in the wrong
# place, and Terraform would not notice.
REQUIRED = ("vcn", "api", "node", "pod", "lb", "bastion")

# The subnet ordinary machines are built in, per tier. Separate from REQUIRED
# because a tier can legitimately have compute without a cluster: most tiers will
# run VMs long before anyone asks for Kubernetes, and demanding five OKE subnets
# before a single nginx could be built would be the rule getting in the way of
# the work it exists to protect.
COMPUTE = "compute"


class NetworkNotMapped(RuntimeError):
    """This tier has no network, so no cluster may be built for it."""


def _configured() -> dict:
    raw = (os.getenv("OCI_OKE_NETWORKS", "") or "").strip()
    if not raw:
        return {}
    try:
        loaded = json.loads(raw)
    except ValueError:
        # Malformed configuration must not read as "no tiers mapped", which
        # would refuse every request with a misleading reason.
        raise NetworkNotMapped(
            "OCI_OKE_NETWORKS is not valid JSON, so no tier can be resolved.")
    return loaded if isinstance(loaded, dict) else {}


def for_tier(tier: str) -> dict:
    """The five OCIDs for this tier, or raise NetworkNotMapped saying why."""
    tier = (tier or "").strip()
    mapped = _configured()
    if not mapped:
        raise NetworkNotMapped(
            "No OKE networks are configured (OCI_OKE_NETWORKS), so there is no "
            "VCN to build a cluster in.")
    entry = mapped.get(tier)
    if not entry:
        raise NetworkNotMapped(
            f"No network is mapped for the {tier or '(unnamed)'} tier. Clusters "
            f"are built one VCN per tier, and building this one in another "
            f"tier's network is not something the portal will do. Mapped tiers: "
            f"{', '.join(sorted(mapped)) or 'none'}.")
    missing = [k for k in REQUIRED if not str(entry.get(k, "")).strip()]
    if missing:
        raise NetworkNotMapped(
            f"The {tier} tier's network is incomplete — missing "
            f"{', '.join(missing)}. A cluster built with part of a network is a "
            f"cluster in the wrong place.")
    return {k: str(entry[k]).strip() for k in REQUIRED}


def compute_subnet(tier: str) -> str:
    """The subnet ordinary machines are built in for this tier.

    Returns "" when NO tier declares one, which is the migration state: the
    portal falls back to OCI_COMPUTE_SUBNET_OCID and behaves exactly as it did
    before. Once any tier declares a compute subnet the map is in force, and a
    tier without one is REFUSED — because at that point silence means somebody
    added tiers and forgot this one, not that the feature is unused.

    Raises NetworkNotMapped when the map is in force and this tier is missing. A
    Production machine built in the development subnet is the failure this
    exists to prevent, and it would look exactly like success.
    """
    tier = (tier or "").strip()
    mapped = _configured()
    in_force = any(str(entry.get(COMPUTE, "")).strip()
                   for entry in mapped.values() if isinstance(entry, dict))
    if not in_force:
        return ""
    entry = mapped.get(tier) or {}
    subnet = str(entry.get(COMPUTE, "")).strip()
    if not subnet:
        with_compute = sorted(t for t, e in mapped.items()
                              if isinstance(e, dict) and str(e.get(COMPUTE, "")).strip())
        raise NetworkNotMapped(
            f"No compute subnet is mapped for the {tier or '(unnamed)'} tier. "
            f"Machines are built one network per tier, and building this one in "
            f"another tier's subnet is not something the portal will do. Mapped "
            f"tiers: {', '.join(with_compute) or 'none'}.")
    return subnet


def compute_tiers() -> list[str]:
    """Tiers that can have a machine built in them, or [] when the map is not in
    force and the single configured subnet still serves every tier."""
    try:
        mapped = _configured()
    except NetworkNotMapped:
        return []
    return sorted(t for t, e in mapped.items()
                  if isinstance(e, dict) and str(e.get(COMPUTE, "")).strip())


def mapped_tiers() -> list[str]:
    """Tiers that could be built today. Surfaced so an unmapped tier is visible
    as a gap rather than discovered when somebody raises a request."""
    try:
        return sorted(_configured())
    except NetworkNotMapped:
        return []
