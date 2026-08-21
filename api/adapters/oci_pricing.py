"""Real OCI pricing adapter (increment 2.2, Phase 2).

Fetches live prices from Oracle's PUBLIC price-list API (no credentials,
read-only, AED). OCI prices are decomposed — per OCPU/hour, per GB-RAM/hour,
per GB storage/month — which maps directly onto our cloud cost formula, so live
mode just swaps the OCI rate dict for the real one.

Controlled by OCI_PRICING_MODE (mock | live). Cached in-process with a short
TTL; on failure the caller falls back to the seeded rate cards (§13).
"""

import os
import time

import httpx

PRICELIST_URL = "https://apexapps.oracle.com/pls/apex/cetools/api/v1/products/"
CURRENCY = "AED"
CACHE_TTL_SECONDS = 3600
OCPU_TO_VCPU = 2  # one Oracle OCPU is two vCPUs (x86)

# Part numbers for current-gen Standard (E5) compute + block volume (config, P5).
PART_OCPU_HOUR = "B97384"          # Compute - Standard - E5 - OCPU (per OCPU/hour)
PART_MEMORY_GB_HOUR = "B97385"     # Compute - Standard - E5 - Memory (per GB/hour)
PART_STORAGE_GB_MONTH = "B91961"   # Storage - Block Volume - Storage (per GB/month)

# Parts for the resource kinds that are NOT virtual machines. Until these
# existed, a bucket and a Kubernetes cluster were both priced as a VM of the
# requested size — every OCI component came to exactly the same monthly figure,
# because the estimate was driven by size alone and never by what was being
# built (found 2026-08-21 while proving oci-objectstorage).
PART_BUCKET_GB_MONTH = "B91628"    # Object Storage - Storage (per GB/month, tiered)
PART_OKE_CLUSTER_HOUR = "B96545"   # OCI Kubernetes Engine - Enhanced Cluster (per cluster/hour)
PART_PSQL_OCPU_HOUR = "B99060"     # Database with PostgreSQL - X86 (per OCPU/hour)

# rates dict cache: {} -> (rates, fetched_at)
_cache: dict[str, tuple[dict[str, float], float]] = {}


class OCIUnavailable(RuntimeError):
    """OCI pricing could not be fetched — caller should fall back to cache."""


def oci_mode() -> str:
    return os.getenv("OCI_PRICING_MODE", "mock").strip().lower()


def is_live() -> bool:
    return oci_mode() == "live"


def _payg_tiers(item: dict) -> list[dict]:
    return [p for loc in item.get("currencyCodeLocalizations", [])
            for p in loc.get("prices", []) if p.get("model") == "PAY_AS_YOU_GO"]


def _pay_as_you_go(item: dict) -> float | None:
    """The rate actually charged, which is NOT always the first one listed.

    SOME PARTS ARE TIERED AND THE FIRST TIER IS FREE. Object Storage publishes
    0.00 for the first 10 GB and 0.0936615 beyond it; returning the first match
    reported object storage as free of charge. Take the highest band — the
    marginal rate — and let `free_allowance` describe the free tier separately,
    so the two facts stay distinguishable instead of averaging into a wrong one.
    """
    tiers = _payg_tiers(item)
    if not tiers:
        return None
    paid = [t for t in tiers if float(t.get("value") or 0) > 0]
    chosen = max(paid, key=lambda t: float(t.get("rangeMin") or 0)) if paid else tiers[0]
    return float(chosen.get("value"))


def free_allowance(item: dict) -> float:
    """Units billed at zero before the marginal rate applies (0 if none)."""
    for tier in _payg_tiers(item):
        if float(tier.get("value") or 0) == 0 and tier.get("rangeMax") is not None:
            return float(tier.get("rangeMax") or 0)
    return 0.0


def _fetch_rates() -> dict[str, float]:
    """Fetch the three OCI parts and return a decomposed rate dict (AED)."""
    try:
        response = httpx.get(PRICELIST_URL, params={"currencyCode": CURRENCY}, timeout=8.0)
        response.raise_for_status()
        items = response.json().get("items", [])
    except Exception as exc:  # noqa: BLE001
        raise OCIUnavailable(str(exc)) from exc

    by_part = {it.get("partNumber"): it for it in items}
    try:
        ocpu_hour = _pay_as_you_go(by_part[PART_OCPU_HOUR])
        memory_gb_hour = _pay_as_you_go(by_part[PART_MEMORY_GB_HOUR])
        storage_gb_month = _pay_as_you_go(by_part[PART_STORAGE_GB_MONTH])
        bucket_gb_month = _pay_as_you_go(by_part[PART_BUCKET_GB_MONTH])
        bucket_free_gb = free_allowance(by_part[PART_BUCKET_GB_MONTH])
        oke_cluster_hour = _pay_as_you_go(by_part[PART_OKE_CLUSTER_HOUR])
        psql_ocpu_hour = _pay_as_you_go(by_part[PART_PSQL_OCPU_HOUR])
    except KeyError as exc:
        raise OCIUnavailable(f"OCI part not found: {exc}") from exc

    if None in (ocpu_hour, memory_gb_hour, storage_gb_month, bucket_gb_month,
                oke_cluster_hour, psql_ocpu_hour):
        raise OCIUnavailable("OCI parts missing a pay-as-you-go price.")

    return {
        # Our model is per-vCPU; OCI charges per OCPU (= 2 vCPU).
        "vcpu-hour": ocpu_hour / OCPU_TO_VCPU,
        "memory-gb-hour": memory_gb_hour,
        "storage-gb-month": storage_gb_month,
        # Not-a-VM rates. A bucket has no OCPUs at all; a cluster is charged a
        # flat fee for its control plane on top of whatever its nodes cost.
        "bucket-storage-gb-month": bucket_gb_month,
        "bucket-free-gb": bucket_free_gb,
        "oke-cluster-hour": oke_cluster_hour,
        # Stored per vCPU, like "vcpu-hour" above, because the sizing model
        # counts vCPUs. Naming it per-OCPU while holding a per-vCPU value
        # would be a factor-of-two waiting to happen.
        "psql-vcpu-hour": psql_ocpu_hour / OCPU_TO_VCPU,
    }


def rates() -> dict[str, float]:
    """Cached live OCI rate dict (AED), raw (pre-discount)."""
    cached = _cache.get("rates")
    if cached and (time.monotonic() - cached[1]) < CACHE_TTL_SECONDS:
        return cached[0]
    fetched = _fetch_rates()
    _cache["rates"] = (fetched, time.monotonic())
    return fetched
