"""What OCI's managed PostgreSQL will actually accept — asked, not assumed.

WHY THIS EXISTS. provisioner.py carried two constants that were never checked
against the service:

    "db_version": os.getenv("OCI_PSQL_VERSION", "14")
    "db_shape":   os.getenv("OCI_PSQL_SHAPE",
                            "PostgreSQL.VM.Standard.E4.Flex.2.32GB")

Both are wrong in me-dubai-1. The catalogue sells `postgres16` while the code
defaulted to 14 — a silent substitution of the kind this codebase has spent a
week removing. And OCI offers no E4 shapes here at all, so the default shape
cannot be built: 14 shapes are published and every one is E5 or Standard3.

There was also no mapping from the portal's sizes to PostgreSQL shapes. The
portal's "small" is 2 vCPU / 4 GB; the smallest managed PostgreSQL is 2 OCPU /
32 GB and the service floor for memory is 16 GB. Nothing reconciled the two, so
a "small" request described a machine that does not exist.

Same shape as the OKE worker image chosen by list position, the Kubernetes
version pinned to v1.29.1, and nginx 1.20 offered on an OS without that stream.
The fix is the same: ask the thing that has to accept the answer.
"""

from __future__ import annotations

import os
import threading
import time

_shape_cache: list[str] = []
_version_cache: list[str] = []
_fetched_at: float = 0.0
_lock = threading.Lock()


def ttl_seconds() -> int:
    """Shapes and versions change when Oracle publishes or retires one — months,
    not minutes. An hour keeps a long-running orchestrator honest without asking
    OCI on every form render."""
    try:
        return max(60, int(os.getenv("PSQL_CATALOGUE_TTL_SECONDS", "3600")))
    except ValueError:
        return 3600


def _client():  # pragma: no cover - thin SDK seam (mocked in tests)
    import oci

    from orchestrator.cloud_state import _oci_config
    return oci.psql.PostgresqlClient(_oci_config())


def _compartment() -> str:
    return (os.getenv("OCI_PSQL_COMPARTMENT_OCID")
            or os.getenv("OCI_COMPUTE_COMPARTMENT_OCID")
            or os.getenv("OCI_COMPARTMENT_OCID") or "")


def _refresh(client=None) -> None:
    """Populate both caches. Failure leaves whatever was there — never empties."""
    global _shape_cache, _version_cache, _fetched_at
    if _shape_cache and (time.time() - _fetched_at) < ttl_seconds():
        return
    try:
        api = client or _client()
    except Exception:  # noqa: BLE001 - no SDK installed is "could not ask",
        return             # not a crash. Tests and offline tooling run without it.
    shapes, versions = [], []
    try:
        shapes = [str(s.id) for s in api.list_shapes(
            compartment_id=_compartment()).data.items]
    except Exception:  # noqa: BLE001 - a lookup must never break provisioning
        pass
    try:
        versions = sorted({str(c.db_version)
                           for c in api.list_default_configurations().data.items})
    except Exception:  # noqa: BLE001
        pass
    if not shapes and not versions:
        return
    with _lock:
        if shapes:
            _shape_cache = shapes
        if versions:
            _version_cache = versions
        _fetched_at = time.time()


def shapes(client=None) -> list[str]:
    """Shape ids OCI publishes, or [] if it could not be asked.

    [] means "unknown", never "none exist" — see resolve_shape.
    """
    _refresh(client)
    return list(_shape_cache)


def versions(client=None) -> list[str]:
    """PostgreSQL major versions OCI offers, oldest first, or []."""
    _refresh(client)
    return list(_version_cache)


def _spec(shape_id: str) -> tuple[int, int]:
    """(ocpus, memory_gb) parsed from a shape id.

    Ids look like PostgreSQL.VM.Standard.E5.Flex.2.32GB — the last two segments
    carry the size. Parsed rather than trusted from list order, which is the
    mistake that put Oracle Linux 7 on the OKE workers.
    """
    parts = shape_id.split(".")
    try:
        ocpu = int(parts[-2])
        memory = int("".join(c for c in parts[-1] if c.isdigit()))
    except (ValueError, IndexError):
        return (0, 0)
    return (ocpu, memory)


# What the portal's t-shirt sizes mean, as a FLOOR the real shape must meet or
# beat. Deliberately not a shape id: ids differ per region and generation, and
# hard-coding one is what broke this.
SIZE_FLOORS: dict[str, tuple[int, int]] = {
    "small": (2, 16),
    "medium": (4, 32),
    "large": (8, 64),
}


def resolve_shape(size: str, client=None) -> tuple[str, str]:
    """(shape id to build, why) for a requested t-shirt size.

    Returns ("", reason) when no published shape satisfies the size — the caller
    must refuse rather than quietly build something smaller or larger. A wrong
    database size is a bill and a performance characteristic the requester did
    not ask for.
    """
    size = (size or "").strip().lower()
    floor = SIZE_FLOORS.get(size)
    if floor is None:
        return "", (f"Unknown size {size!r}. The portal offers: "
                    f"{', '.join(sorted(SIZE_FLOORS))}.")

    published = shapes(client)
    if not published:
        configured = os.getenv("OCI_PSQL_SHAPE", "").strip()
        if configured:
            return configured, ("could not ask OCI which shapes exist; using the "
                                "configured OCI_PSQL_SHAPE")
        return "", ("Could not ask OCI which PostgreSQL shapes it offers, and "
                    "OCI_PSQL_SHAPE is not set.")

    want_ocpu, want_memory = floor
    fits = [s for s in published
            if _spec(s)[0] >= want_ocpu and _spec(s)[1] >= want_memory]
    if not fits:
        return "", (f"OCI publishes no PostgreSQL shape meeting {size} "
                    f"({want_ocpu} OCPU / {want_memory} GB). It offers: "
                    f"{', '.join(sorted(published))}.")

    # Smallest shape that satisfies the floor, so a size maps to the cheapest
    # thing that honours it. Sorted on the parsed spec, never on list order.
    chosen = min(fits, key=lambda s: (_spec(s)[0], _spec(s)[1], s))
    ocpu, memory = _spec(chosen)
    return chosen, f"smallest shape meeting {size} ({ocpu} OCPU / {memory} GB)"


def resolve_version(requested: str = "", client=None) -> tuple[str, str]:
    """(version to build, why) for a requested PostgreSQL major version.

    A version OCI does not offer is reported, never silently swapped — the
    catalogue said postgres16 and the code built 14, which is precisely the
    substitution this returns an error for.
    """
    requested = (requested or "").strip().lstrip("vV")
    available = versions(client)
    if requested and available and requested not in available:
        return "", (f"OCI does not offer PostgreSQL {requested}. It offers: "
                    f"{', '.join(available)}.")
    if requested:
        return requested, ""
    if available:
        newest = max(available, key=lambda v: int("".join(
            c for c in v if c.isdigit()) or 0))
        return newest, f"newest OCI offers ({newest})"
    return "", ("Could not ask OCI which PostgreSQL versions it offers, and none "
                "was configured. Set OCI_PSQL_VERSION.")


def version_from_build(build: str) -> str:
    """"postgres16" -> "16". The catalogue name is the promise; this reads it.

    The build name is what the requester chose and what the approval names, so
    it — not an environment default — decides the version that gets built.
    """
    digits = "".join(c for c in (build or "") if c.isdigit())
    return digits
