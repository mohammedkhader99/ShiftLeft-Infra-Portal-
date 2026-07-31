"""Cloud state adapter — read-only reconciliation (increment 1 of cloud sync).

Reports the ACTUAL state of a request's cloud resources so the portal can
reconcile its registry against reality — e.g. a resource stopped, resized, or
deleted directly in OCI/Azure. **Read-only:** it observes, it never changes
cloud state.

Modes (CLOUD_STATE_MODE), mirroring the other adapters:
  - mock (default): reflects the registry (reports in-sync). A demo/test hook,
    CLOUD_STATE_SIMULATE_MISSING (comma-separated references), reports those
    resources as missing so a divergence can be shown offline.
  - live: the OCI/Azure SDK path — an extension point that needs the customer's
    cloud credentials (via a vault). Not wired to a real tenancy yet; it raises a
    clear message until implemented.
"""

import os


class CloudStateUnavailable(RuntimeError):
    """The live cloud-state adapter isn't configured (no credentials / not implemented)."""


def mode() -> str:
    return os.getenv("CLOUD_STATE_MODE", "mock").strip().lower()


def _simulate_missing() -> set[str]:
    raw = os.getenv("CLOUD_STATE_SIMULATE_MISSING", "").strip()
    return {r.strip() for r in raw.split(",") if r.strip()}


def describe(reference: str, resources: list[dict]) -> list[dict]:
    """Actual state per resource: [{kind, name, status, exists, source}].

    `resources` is the [{kind, name}] the portal expects to exist for this request.
    """
    if mode() == "live":
        return _describe_live(reference, resources)
    return _describe_mock(reference, resources)


def _describe_mock(reference: str, resources: list[dict]) -> list[dict]:
    missing = reference in _simulate_missing()
    return [
        {
            "kind": r.get("kind"),
            "name": r.get("name"),
            "status": "missing" if missing else "active",
            "exists": not missing,
            "source": "mock",
        }
        for r in resources
    ]


def _describe_live(reference: str, resources: list[dict]) -> list[dict]:
    # Extension point. A real implementation queries the provider APIs (OCI SDK /
    # Azure SDK) with credentials supplied via a vault, and maps each resource to
    # its live status (running/stopped/missing). It stays read-only.
    raise CloudStateUnavailable(
        "Live cloud-state adapter not configured. Provide OCI/Azure credentials via a "
        "vault and implement the SDK query in orchestrator/cloud_state._describe_live."
    )
