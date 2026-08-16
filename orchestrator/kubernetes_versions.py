"""Which Kubernetes versions OCI will actually accept today.

WHY THIS EXISTS. The OKE module carried `default_kubernetes_version = "v1.29.1"`,
true when it was written and retired since. REQ-2026-0148 — the first real
cluster anyone asked this portal for — failed at apply with
"Invalid Kubernetes version v1.29.1. Supported versions: [v1.33.0 … v1.36.1]".

The same shape as the catalogue offering nginx 1.20 on an Oracle Linux 9.8 that
has no such stream: a version hard-coded in the repo, correct once, with nothing
checking it against the thing that has to accept it. Fixed for module streams by
measuring a machine; fixed here by asking OCI.

A CLOUD RETIRES VERSIONS ON ITS OWN SCHEDULE. Nothing in this repository can be
edited often enough to keep up, so the answer is fetched rather than stored, and
cached briefly because a cluster build asks once and a form render may ask often.
"""

from __future__ import annotations

import os
import threading
import time

_cache: list[str] = []
_fetched_at: float = 0.0
_lock = threading.Lock()


def ttl_seconds() -> int:
    """Supported versions change when Oracle retires one, which is a matter of
    months. An hour keeps a long-running orchestrator from holding a version
    past its retirement without asking OCI on every request."""
    try:
        return max(60, int(os.getenv("OKE_VERSION_TTL_SECONDS", "3600")))
    except ValueError:
        return 3600


def _client():  # pragma: no cover - thin SDK seam (mocked in tests)
    import oci

    from orchestrator.cloud_state import _oci_config
    return oci.container_engine.ContainerEngineClient(_oci_config())


def supported(client=None) -> list[str]:
    """Versions OCI accepts, oldest first, or [] if it could not be asked.

    [] means "unknown", never "none are supported". A caller must fall back to
    what it was configured with rather than refuse every cluster because the
    lookup failed — a portal that cannot reach OCI has worse problems than a
    version default, and blocking here would hide them.
    """
    global _cache, _fetched_at
    if _cache and (time.time() - _fetched_at) < ttl_seconds():
        return list(_cache)
    try:
        options = (client or _client()).get_cluster_options(
            cluster_option_id="all").data
        found = [str(v) for v in (options.kubernetes_versions or [])]
    except Exception:  # noqa: BLE001 - a lookup must never break provisioning
        return list(_cache)
    if not found:
        return list(_cache)
    with _lock:
        _cache, _fetched_at = found, time.time()
    return list(found)


def _rank(version: str) -> tuple:
    """Sortable form of a version string, so v1.33.10 beats v1.33.1.

    OCI returns them in an order that LOOKS sorted and is not — the list holds
    v1.33.1 before v1.33.10, which is string order, not version order. Taking the
    last element would work today and pick a lower version the moment a double
    digit patch appears in the middle.
    """
    parts = (version or "").lstrip("vV").split(".")
    out = []
    for part in parts:
        digits = "".join(c for c in part if c.isdigit())
        out.append(int(digits) if digits else 0)
    return tuple(out)


def newest(client=None) -> str:
    """The highest version OCI currently accepts, or "" if it could not be asked.

    Deliberately the newest rather than a pinned middle: a cluster built today
    should not start life two releases behind, and OCI only lists versions it is
    still willing to create.
    """
    found = supported(client)
    return max(found, key=_rank) if found else ""


def resolve(requested: str = "", client=None) -> tuple[str, str]:
    """(version to use, why) for a cluster about to be built.

    A requested version OCI no longer accepts is NOT silently replaced — that
    would build something other than what was asked for, which is the failure
    this codebase has spent a week removing. It is reported, and the caller
    decides.
    """
    requested = (requested or "").strip()
    found = supported(client)
    if requested and found and requested not in found:
        return "", (f"OCI no longer accepts Kubernetes {requested}. It accepts: "
                    f"{', '.join(found)}.")
    if requested:
        return requested, ""
    chosen = newest(client)
    if chosen:
        return chosen, f"newest OCI accepts ({chosen})"
    return "", ("Could not ask OCI which Kubernetes versions it accepts, and none "
                "was configured. Set OCI_OKE_KUBERNETES_VERSION.")
