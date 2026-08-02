"""GCP pricing adapter (multi-cloud breadth, F-CAT).

GCP compute/storage rates map onto our decomposed cloud cost formula (per
vCPU/hour, per GB-RAM/hour, per GB storage/month), so live mode just swaps in the
real GCP rate dict — exactly like the OCI and AWS adapters.

Controlled by GCP_PRICING_MODE (mock | live). The GCP Cloud Billing Catalog API
needs Google Cloud credentials, so live fetching is a marked extension point.
Until it's wired, live mode raises and the caller falls back to the seeded
cloud_gcp rate cards (§13). Mock mode (the default) prices entirely from those
seeded rate cards, so GCP estimates work out of the box.
"""

import os

CURRENCY = "AED"


class GCPUnavailable(RuntimeError):
    """GCP pricing could not be fetched — caller should fall back to cache."""


def gcp_mode() -> str:
    return os.getenv("GCP_PRICING_MODE", "mock").strip().lower()


def is_live() -> bool:
    return gcp_mode() == "live"


def rates() -> dict[str, float]:
    """Live GCP rate dict (AED), raw (pre-discount): {vcpu-hour, memory-gb-hour,
    storage-gb-month}.

    Extension point: wire the GCP Cloud Billing Catalog API here (the
    `cloudbilling` client), with Google Cloud credentials, and return the
    decomposed rates. Until then live mode raises so the caller keeps the cached
    seeded rates.
    """
    raise GCPUnavailable(
        "Live GCP pricing is not configured. Wire the GCP Cloud Billing Catalog API "
        "(with Google Cloud credentials) in api/adapters/gcp_pricing.rates to enable "
        "it — mock mode prices from the seeded cloud_gcp rate cards.")
