"""Real Azure pricing adapter (increment 2.1, Phase 2).

Fetches live VM prices from Azure's PUBLIC Retail Prices API (no credentials,
read-only) so an Azure estimate reflects real current prices. Controlled by the
per-adapter switch AZURE_PRICING_MODE (mock | live) so Azure can go live while
every other adapter stays mock.

Prices are cached in-process with a short TTL — the live cost panel calls the
API on every change, so we must not hit Azure on every keystroke. If Azure is
slow or unreachable, callers fall back to the cached rate cards (§13 graceful
degradation).
"""

import os
import time

import httpx

RETAIL_PRICES_URL = "https://prices.azure.com/api/retail/prices"
CURRENCY = "AED"
DEFAULT_REGION = "uaenorth"  # UAE North — fits the GDRFAD data-residency context
HOURS_PER_MONTH = 730
CACHE_TTL_SECONDS = 3600  # prices change rarely; refresh hourly

# Representative on-demand Linux VM SKU per size tier (config, not code — P5).
SIZE_TO_SKU = {
    "small": "Standard_B2s",
    "medium": "Standard_D4s_v5",
    "large": "Standard_D8s_v5",
}

# (sku, region) -> (hourly_price, fetched_at)
_cache: dict[tuple[str, str], tuple[float, float]] = {}


class AzureUnavailable(RuntimeError):
    """Azure pricing could not be fetched — caller should fall back to cache."""


def azure_mode() -> str:
    return os.getenv("AZURE_PRICING_MODE", "mock").strip().lower()


def is_live() -> bool:
    return azure_mode() == "live"


def _fetch_hourly(sku: str, region: str) -> float:
    """Fetch the on-demand Linux hourly price for a VM SKU from Azure."""
    price_filter = (
        f"armRegionName eq '{region}' and armSkuName eq '{sku}' "
        f"and priceType eq 'Consumption' and serviceName eq 'Virtual Machines'"
    )
    try:
        response = httpx.get(
            RETAIL_PRICES_URL,
            params={"$filter": price_filter, "currencyCode": CURRENCY},
            timeout=5.0,
        )
        response.raise_for_status()
        items = response.json().get("Items", [])
    except Exception as exc:  # noqa: BLE001
        raise AzureUnavailable(str(exc)) from exc

    # Keep on-demand Linux only: drop Windows, Spot and Low Priority meters.
    for item in items:
        name = item.get("meterName", "")
        product = item.get("productName", "")
        if "Windows" in product or "Spot" in name or "Low Priority" in name:
            continue
        if "Hour" in item.get("unitOfMeasure", ""):
            return float(item["unitPrice"])
    raise AzureUnavailable(f"No on-demand Linux price found for {sku} in {region}.")


def vm_hourly(size: str, region: str = DEFAULT_REGION) -> float:
    """Cached live hourly price (AED) for the VM SKU mapped to `size`."""
    sku = SIZE_TO_SKU.get(size)
    if sku is None:
        raise AzureUnavailable(f"No Azure SKU mapping for size '{size}'.")

    key = (sku, region)
    cached = _cache.get(key)
    if cached and (time.monotonic() - cached[1]) < CACHE_TTL_SECONDS:
        return cached[0]

    price = _fetch_hourly(sku, region)
    _cache[key] = (price, time.monotonic())
    return price


def vm_monthly(size: str, region: str = DEFAULT_REGION) -> float:
    """Live monthly compute cost (AED, list price) for a size."""
    return vm_hourly(size, region) * HOURS_PER_MONTH
