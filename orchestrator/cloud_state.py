"""Cloud state adapter — the portal's window onto (and hands on) real cloud state.

Two operations:
  - describe() — reports the ACTUAL state of a request's resources so the portal
    can reconcile its registry against reality (increment 1). **Read-only.**
  - actuate() — stops/starts a request's resources (increment 2). The one write
    path; only ever driven from the orchestrator, after the API RBAC-gated the
    operator and the signed handoff verified.

Modes (CLOUD_STATE_MODE), mirroring the other adapters:
  - mock (default): describe reflects the registry; actuate echoes the target
    power state. Changes NO real cloud. A demo/test hook,
    CLOUD_STATE_SIMULATE_MISSING (comma-separated references), reports those
    resources as missing so a divergence can be shown offline.
  - live: the real OCI SDK path. describe queries Object Storage (bucket exists?)
    and Compute (instance running/stopped?); actuate stops/starts compute
    instances (buckets have no power state). It reuses the SAME credentials the
    orchestrator already uses for Terraform (OCI_* env + the private key mounted
    from ./secrets) — no new secret. If those aren't configured it raises a clear
    message. Real stop/start additionally requires an explicit opt-in
    (OCI_ACTUATE_ENABLED=true), so turning on live reconciliation (read) never by
    itself enables live actuation (write).
"""

import os
import re


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


# --- Live OCI adapter --------------------------------------------------------
# Reuses the SAME credentials the orchestrator already uses for Terraform: the
# OCI_* env vars plus the API private key mounted read-only from ./secrets. The
# `oci` SDK is lazy-imported (only the two client builders below), so mock mode
# and the test suite never need it installed.

_REQUIRED_OCI = ("OCI_TENANCY_OCID", "OCI_USER_OCID", "OCI_FINGERPRINT",
                 "OCI_REGION", "OCI_COMPARTMENT_OCID")

# OCI compute lifecycle_state -> our power_state. RUNNING/STARTING count as
# running; STOPPED/STOPPING as stopped; TERMINATED/TERMINATING as gone.
_OCI_POWER = {"RUNNING": "running", "STARTING": "running",
              "STOPPED": "stopped", "STOPPING": "stopped"}


def _key_path() -> str:
    return os.getenv("OCI_PRIVATE_KEY_PATH", "/secrets/oci_api_key.pem")


def _compute_compartment() -> str:
    """Where compute instances live — the compute compartment if set, else the
    default provisioning compartment (must match how the VM was provisioned)."""
    return os.getenv("OCI_COMPUTE_COMPARTMENT_OCID") or os.getenv("OCI_COMPARTMENT_OCID", "")


def _require_oci_creds() -> None:
    """Raise a clear, actionable error if the live OCI adapter isn't configured."""
    missing = [k for k in _REQUIRED_OCI if not os.getenv(k)]
    if missing or not os.path.exists(_key_path()):
        raise CloudStateUnavailable(
            "Live OCI adapter selected (CLOUD_STATE_MODE=live) but not configured. Set "
            + ", ".join(_REQUIRED_OCI)
            + f" and mount the API signing key at {_key_path()} (see ./secrets). These are "
            "the same credentials the orchestrator uses for Terraform."
        )


def _oci_config() -> dict:
    return {
        "user": os.getenv("OCI_USER_OCID"),
        "fingerprint": os.getenv("OCI_FINGERPRINT"),
        "tenancy": os.getenv("OCI_TENANCY_OCID"),
        "region": os.getenv("OCI_REGION"),
        "key_file": _key_path(),
    }


def _object_storage_client():  # pragma: no cover - thin SDK seam (mocked in tests)
    import oci
    return oci.object_storage.ObjectStorageClient(_oci_config())


def _compute_client():  # pragma: no cover - thin SDK seam (mocked in tests)
    import oci
    return oci.core.ComputeClient(_oci_config())


#: A blueprint that BOOTS A MACHINE has to say so, because the machine must
#: report on itself at the end of first boot. That declaration is already made
#: by every blueprint and already held to by test_every_machine_reports.py, so
#: it is the existing authority on this question rather than a new one.
_BOOTS_A_MACHINE = ("user_data", "template")


def _is_compute(kind: str) -> bool:
    """Is this resource kind a machine we can ask the compute API about?

    ASKED OF THE BLUEPRINT, NOT INFERRED FROM THE NAME.

    The name rule below matched "instance", "compute", "vm" and "oci-vm" — and
    `oci-service-vm` is none of those, because "vm" was an EXACT match and not a
    suffix. So the kind that 24 of this estate's resources are, and every VM
    that carries software (nginx, redis, .NET, Vault, SQL Server), was invisible
    to live cloud state: reconcile asked object storage for a bucket named after
    a machine, got a 404, and would have reported a healthy running server as
    deleted out-of-band. `oci-apache` and `oci-kafka` were invisible for the
    same reason, another 13 resources.

    A NAME RULE CANNOT FIX THIS. `oci-apache` and `oci-kafka` are single compute
    instances and say nothing about it; `oci-oke`'s module creates instances too,
    but the resource this kind TRACKS is a cluster, and asking the compute API
    for a cluster's name finds nothing — which is the same false "missing" in the
    other direction. Reading the Terraform is no better: the legacy flat module
    declares a bucket, a database AND an instance.

    So the question goes to the recipe, which is the only layer that knows what
    it builds. `oci-oke` declares `boot_report: none` explicitly and is correctly
    not a machine.

    The old name rule survives ONLY for a kind no manifest claims, so a
    resource from outside the registry is judged no worse than before.
    """
    from orchestrator import blueprint_registry

    try:
        manifest = blueprint_registry.for_resource_kind(kind)
    except Exception:  # noqa: BLE001 - a registry that cannot answer is not a verdict
        manifest = None
    if manifest is not None:
        declared = str(manifest.get("boot_report") or "").strip().lower()
        return declared in _BOOTS_A_MACHINE

    k = (kind or "").lower()
    return "instance" in k or "compute" in k or k in ("vm", "oci-vm")


def _bucket_state(client, namespace: str, name: str) -> tuple[str, bool]:
    """('active', True) if the bucket exists, ('missing', False) on a 404."""
    try:
        client.get_bucket(namespace, name)
        return "active", True
    except Exception as exc:  # oci.exceptions.ServiceError carries .status
        if getattr(exc, "status", None) == 404:
            return "missing", False
        raise


def _find_instance(client, compartment: str, name: str):
    """Locate a compute instance by OCID (if `name` is one) or by display name.

    The compute modules name instances `<name>-01`, `-02`, … because a blueprint
    can build more than one. An exact-match lookup therefore finds nothing and
    reports a running environment as deleted, so an indexed name is matched too.
    Anchored on purpose: a loose prefix would match `web-staging` for `web`.
    """
    if name and name.startswith("ocid1.instance"):
        return client.get_instance(name).data

    def _alive(items):
        return [i for i in (items or [])
                if getattr(i, "lifecycle_state", "") not in ("TERMINATED", "TERMINATING")]

    live = _alive(client.list_instances(compartment_id=compartment, display_name=name).data)
    if not live and name:
        indexed = re.compile(rf"^{re.escape(name)}-\d+$")
        live = sorted(
            _alive(i for i in client.list_instances(compartment_id=compartment).data
                   if indexed.match(getattr(i, "display_name", "") or "")),
            key=lambda i: i.display_name,
        )
    return live[0] if live else None


def _instance_state(instance) -> tuple[bool, str]:
    """(exists, power_state) for a compute instance from its lifecycle_state."""
    state = (getattr(instance, "lifecycle_state", "") or "").upper()
    if state in ("TERMINATED", "TERMINATING"):
        return False, "stopped"
    return True, _OCI_POWER.get(state, "running")


def _describe_live(reference: str, resources: list[dict]) -> list[dict]:
    """Query OCI for the real state of each resource. Read-only."""
    _require_oci_creds()
    compartment = _compute_compartment()
    os_client = namespace = compute = None
    out = []
    for r in resources:
        kind, name = (r.get("kind") or ""), r.get("name")
        entry = {"kind": kind, "name": name, "source": "oci"}
        if _is_compute(kind):
            compute = compute or _compute_client()
            inst = _find_instance(compute, compartment, name)
            exists, power = _instance_state(inst) if inst is not None else (False, "stopped")
            entry.update(status="active" if exists else "missing", exists=exists, power_state=power)
        else:  # object-storage bucket (the only catalogue resource today)
            if os_client is None:
                os_client = _object_storage_client()
                namespace = os_client.get_namespace().data
            status, exists = _bucket_state(os_client, namespace, name)
            entry.update(status=status, exists=exists)
        out.append(entry)
    return out


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


def _actuate_enabled() -> bool:
    """Real stop/start is a SECOND, explicit opt-in beyond CLOUD_STATE_MODE=live,
    so enabling live reconciliation (read) never by itself enables live writes."""
    return os.getenv("OCI_ACTUATE_ENABLED", "false").strip().lower() in ("1", "true", "yes", "on")


def _instance_action(client, instance_id: str, action: str) -> None:  # pragma: no cover - SDK seam
    client.instance_action(instance_id, action)


def _actuate_live(reference: str, resources: list[dict], action: str) -> list[dict]:
    """Stop/start real OCI compute instances. Buckets have no power state, so a
    non-compute resource is refused rather than silently ignored."""
    if not _actuate_enabled():
        raise CloudStateUnavailable(
            "Live OCI actuation is not enabled. Set OCI_ACTUATE_ENABLED=true to allow real "
            "stop/start (read-only reconciliation works without it)."
        )
    _require_oci_creds()
    compartment = _compute_compartment()
    compute = _compute_client()
    # SOFTSTOP = graceful ACPI shutdown; START powers a stopped instance back on.
    oci_action = "SOFTSTOP" if action == "stop" else "START"
    power = "stopped" if action == "stop" else "running"
    out = []
    for r in resources:
        kind, name = (r.get("kind") or ""), r.get("name")
        if not _is_compute(kind):
            raise CloudStateUnavailable(
                f"Cannot {action} '{name}': only compute instances can be stopped or started "
                f"(resource kind is '{kind}')."
            )
        inst = _find_instance(compute, compartment, name)
        if inst is None:
            raise CloudStateUnavailable(f"Cannot {action} '{name}': no matching OCI instance found.")
        _instance_action(compute, inst.id, oci_action)
        out.append({"kind": kind, "name": name, "power_state": power, "source": "oci"})
    return out
