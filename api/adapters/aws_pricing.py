"""AWS pricing adapter (multi-cloud breadth, F-CAT).

AWS compute/storage rates map onto our decomposed cloud cost formula (per
vCPU/hour, per GB-RAM/hour, per GB storage/month), so live mode just swaps in the
real AWS rate dict — exactly like the OCI adapter.

Controlled by AWS_PRICING_MODE (mock | live). Unlike Azure/OCI (public price
APIs), the AWS Price List API needs AWS credentials — so live fetching is a marked
extension point. Until it's wired, live mode raises and the caller falls back to
the seeded cloud_aws rate cards (§13). Mock mode (the default) prices entirely
from those seeded rate cards, so AWS estimates work out of the box.
"""

import os

CURRENCY = "AED"


class AWSUnavailable(RuntimeError):
    """AWS pricing could not be fetched — caller should fall back to cache."""


def aws_mode() -> str:
    return os.getenv("AWS_PRICING_MODE", "mock").strip().lower()


def is_live() -> bool:
    return aws_mode() == "live"


def rates() -> dict[str, float]:
    """Live AWS rate dict (AED), raw (pre-discount): {vcpu-hour, memory-gb-hour,
    storage-gb-month}.

    Extension point: wire the AWS Price List API here (the boto3 `pricing` client
    or the bulk price-list JSON), with AWS credentials, and return the decomposed
    rates. Until then live mode raises so the caller keeps the cached seeded rates.
    """
    raise AWSUnavailable(
        "Live AWS pricing is not configured. Wire the AWS Price List API (with AWS "
        "credentials) in api/adapters/aws_pricing.rates to enable it — mock mode "
        "prices from the seeded cloud_aws rate cards.")
