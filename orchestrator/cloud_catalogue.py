"""What the cloud actually offers — compute shapes and OS images.

The component detail form's dropdowns were a list a human typed into `db/seed.py`.
This adapter replaces the machine-knowable part of that list with what the
tenancy really has, so a shape Oracle retires stops being offered without anyone
editing code.

WHY THIS LIVES IN THE ORCHESTRATOR
----------------------------------
Listing shapes needs OCI credentials, and the API must never hold one
(ARCHITECTURE §4 — the API collects and validates; only the orchestrator touches
the cloud). So the orchestrator reads the tenancy and the API asks it, over the
same signed channel as /posture, and caches the answer. What crosses the boundary
is a list of shape names and image OCIDs; no credential ever does.

READ-ONLY. Every call here is a list_* — this module cannot create, change or
destroy anything, which is why it needs no execution gate of its own.

THE ALLOWLIST FAILS CLOSED, DELIBERATELY
----------------------------------------
A tenancy offers ~100 shapes, including bare-metal and GPU machines costing
thousands a month. `OCI_SHAPE_ALLOWLIST` names the ones a requester may pick.
Unset means NOTHING live is offered and the seeded values stand — silence must
not mean "offer everything". The same applies to `OCI_IMAGE_FILTER`.

Modes (OCI_CATALOGUE_MODE), mirroring the other adapters:
  - mock (default): a small fixed set, so the whole path is testable and
    demoable without touching the tenancy.
  - live: the real OCI SDK.
"""

import os


class CloudCatalogueUnavailable(RuntimeError):
    """The live catalogue couldn't be read — the caller keeps whatever it cached."""


def mode() -> str:
    return os.getenv("OCI_CATALOGUE_MODE", "mock").strip().lower()


def is_live() -> bool:
    return mode() == "live"


def _split(raw: str) -> list[str]:
    return [v.strip() for v in (raw or "").split(",") if v.strip()]


# NOTE: the environment lookups below name their variable inline rather than
# taking it as an argument to a helper. The standing-rule guard
# (api/tests/test_settings_coverage.py) finds settings by scanning for a literal
# quoted variable name at the lookup, so routing them through a helper hid these
# three from it — which is exactly what happened, until the guard caught one and
# silently missed the other two.
def shape_allowlist() -> list[str]:
    """Shapes a requester may be offered. Empty = offer none (fail closed)."""
    return _split(os.getenv("OCI_SHAPE_ALLOWLIST", ""))


def image_filter() -> list[str]:
    """Substrings an image's display name must contain. Empty = offer none.

    A substring rather than an OCID list because platform images are re-published
    with a new OCID every few weeks — pinning OCIDs would go stale silently,
    which is the failure this whole module exists to stop.
    """
    return _split(os.getenv("OCI_IMAGE_FILTER", ""))


# --- Mock -------------------------------------------------------------------

_MOCK_SHAPES = [
    {"name": "VM.Standard.E4.Flex", "min_ocpus": 1, "max_ocpus": 64,
     "min_memory_gb": 1, "max_memory_gb": 1024},
    {"name": "VM.Standard.E5.Flex", "min_ocpus": 1, "max_ocpus": 94,
     "min_memory_gb": 1, "max_memory_gb": 1049},
    {"name": "VM.Standard.A1.Flex", "min_ocpus": 1, "max_ocpus": 78,
     "min_memory_gb": 1, "max_memory_gb": 512},
    # A deliberately small one, so the "your shape doesn't fit" rule has
    # something to catch in mock mode.
    {"name": "VM.Standard2.1", "min_ocpus": 1, "max_ocpus": 1,
     "min_memory_gb": 15, "max_memory_gb": 15},
]

_MOCK_IMAGES = [
    {"ocid": "ocid1.image.oc1..mock-ol9", "name": "Oracle-Linux-9.4-2026.01.31-0",
     "os": "Oracle Linux", "os_version": "9"},
    {"ocid": "ocid1.image.oc1..mock-ol8", "name": "Oracle-Linux-8.10-2026.01.31-0",
     "os": "Oracle Linux", "os_version": "8"},
    {"ocid": "ocid1.image.oc1..mock-ubuntu", "name": "Canonical-Ubuntu-22.04-2026.01.15-0",
     "os": "Canonical Ubuntu", "os_version": "22.04"},
]


# --- Live -------------------------------------------------------------------

def _compute_client():  # pragma: no cover - thin SDK seam (mocked in tests)
    import oci

    from orchestrator.cloud_state import _oci_config
    return oci.core.ComputeClient(_oci_config())


def _compartment() -> str:
    """Where to look. Compute may live in its own compartment; fall back to the
    main one, matching how the provisioner resolves it."""
    return (os.getenv("OCI_COMPUTE_COMPARTMENT_OCID", "").strip()
            or os.getenv("OCI_COMPARTMENT_OCID", "").strip())


def _live_shapes(client, compartment: str) -> list[dict]:
    out = []
    for s in client.list_shapes(compartment_id=compartment).data:
        # Flex shapes report their range in shape_config; fixed shapes report a
        # single ocpus/memory. Normalising both to a min/max lets one validation
        # rule cover the two.
        cfg = getattr(s, "ocpu_options", None)
        mem = getattr(s, "memory_options", None)
        ocpus = getattr(s, "ocpus", None) or 0
        memory = getattr(s, "memory_in_gbs", None) or 0
        out.append({
            "name": s.shape,
            "min_ocpus": int(getattr(cfg, "min", None) or ocpus or 1),
            "max_ocpus": int(getattr(cfg, "max", None) or ocpus or 1),
            "min_memory_gb": int(getattr(mem, "min_in_gbs", None) or memory or 1),
            "max_memory_gb": int(getattr(mem, "max_in_gbs", None) or memory or 1),
        })
    return out


def _live_images(client, compartment: str) -> list[dict]:
    return [
        {"ocid": i.id, "name": i.display_name,
         "os": getattr(i, "operating_system", "") or "",
         "os_version": getattr(i, "operating_system_version", "") or ""}
        for i in client.list_images(compartment_id=compartment).data
    ]


# --- The one entry point ----------------------------------------------------

def fetch() -> dict:
    """Everything the portal may offer, already filtered.

    Returns counts alongside the lists so an admin can see "12 shapes exist, 3
    are allowed" — otherwise an empty allowlist and an unreachable tenancy look
    identical from the console.
    """
    allow = shape_allowlist()
    keep = image_filter()

    if is_live():
        compartment = _compartment()
        if not compartment:
            raise CloudCatalogueUnavailable(
                "OCI_COMPARTMENT_OCID is not set — cannot list shapes or images.")
        try:
            client = _compute_client()
            all_shapes = _live_shapes(client, compartment)
            all_images = _live_images(client, compartment)
        except CloudCatalogueUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001 - SDK raises many types
            raise CloudCatalogueUnavailable(str(exc)) from exc
    else:
        all_shapes, all_images = list(_MOCK_SHAPES), list(_MOCK_IMAGES)

    # De-duplicate shapes: list_shapes returns one entry per availability domain,
    # so the same shape comes back several times.
    seen: set[str] = set()
    unique_shapes = []
    for s in all_shapes:
        if s["name"] not in seen:
            seen.add(s["name"])
            unique_shapes.append(s)

    shapes = [s for s in unique_shapes if s["name"] in allow]
    images = [i for i in all_images if any(k.lower() in i["name"].lower() for k in keep)]

    return {
        "mode": mode(),
        "shapes": shapes,
        "images": images,
        # What was on offer before filtering, so an empty result is explainable.
        "shapes_available": len(unique_shapes),
        "images_available": len(all_images),
        "allowlist_set": bool(allow),
        "image_filter_set": bool(keep),
    }
