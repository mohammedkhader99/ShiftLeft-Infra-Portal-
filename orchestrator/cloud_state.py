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


# --- Actuation (increment 2 of cloud sync) -----------------------------------
# Unlike describe(), actuate() CHANGES resource state (stop/start). It is the one
# write path in this adapter; it is still driven only from the orchestrator, and
# only after the API has RBAC-gated the operator and the signed handoff verified.

def actuate(reference: str, resources: list[dict], action: str) -> list[dict]:
    """Stop or start a request's resources; report the resulting power state:
    [{kind, name, power_state, source}].

    `action` is 'stop' or 'start'. Idempotent — stopping a stopped resource is a
    no-op. Mock models the action (echoes the target power state) and touches no
    real cloud; live calls the provider APIs (an extension point needing creds).
    """
    if action not in ("stop", "start"):
        raise ValueError(f"Unsupported actuation action: {action!r}")
    if mode() == "live":
        return _actuate_live(reference, resources, action)
    return _actuate_mock(reference, resources, action)


def _actuate_mock(reference: str, resources: list[dict], action: str) -> list[dict]:
    power = "stopped" if action == "stop" else "running"
    return [
        {"kind": r.get("kind"), "name": r.get("name"), "power_state": power, "source": "mock"}
        for r in resources
    ]


def _actuate_live(reference: str, resources: list[dict], action: str) -> list[dict]:
    # Extension point. A real implementation calls the provider APIs (OCI SDK /
    # Azure SDK) to stop/start each resource, with credentials supplied via a
    # vault, then returns the resulting power state.
    raise CloudStateUnavailable(
        "Live cloud actuation not configured. Provide OCI/Azure credentials via a "
        "vault and implement the SDK stop/start in orchestrator/cloud_state._actuate_live."
    )
