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

# rates dict cache: {} -> (rates, fetched_at)
_cache: dict[str, tuple[dict[str, float], float]] = {}


class OCIUnavailable(RuntimeError):
    """OCI pricing could not be fetched — caller should fall back to cache."""


def oci_mode() -> str:
    return os.getenv("OCI_PRICING_MODE", "mock").strip().lower()


def is_live() -> bool:
    return oci_mode() == "live"


def _pay_as_you_go(item: dict) -> float | None:
    for loc in item.get("currencyCodeLocalizations", []):
        for price in loc.get("prices", []):
            if price.get("model") == "PAY_AS_YOU_GO":
                return float(price.get("value"))
    return None


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
    except KeyError as exc:
        raise OCIUnavailable(f"OCI part not found: {exc}") from exc

    if None in (ocpu_hour, memory_gb_hour, storage_gb_month):
        raise OCIUnavailable("OCI parts missing a pay-as-you-go price.")

    return {
        # Our model is per-vCPU; OCI charges per OCPU (= 2 vCPU).
        "vcpu-hour": ocpu_hour / OCPU_TO_VCPU,
        "memory-gb-hour": memory_gb_hour,
        "storage-gb-month": storage_gb_month,
    }


def rates() -> dict[str, float]:
    """Cached live OCI rate dict (AED), raw (pre-discount)."""
    cached = _cache.get("rates")
    if cached and (time.monotonic() - cached[1]) < CACHE_TTL_SECONDS:
        return cached[0]
    fetched = _fetch_rates()
    _cache["rates"] = (fetched, time.monotonic())
    return fetched
