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

    A term starting with "-" EXCLUDES instead. Oracle names its images
    `Oracle-Linux-9.8-aarch64-...` and `Oracle-Linux-9.8-Gen2-GPU-...` alongside
    the plain x86 `Oracle-Linux-9.8-...`, so an include-only filter cannot say
    "Oracle Linux 9, x86" without naming the release date — which goes stale the
    moment Oracle publishes the next one. Offering an ARM image for an AMD shape
    fails at apply time, so this needs to be expressible:

        OCI_IMAGE_FILTER=Oracle-Linux-9,-aarch64,-GPU
    """
    return _split(os.getenv("OCI_IMAGE_FILTER", ""))


def _image_allowed(name: str, terms: list[str]) -> bool:
    """Whether an image name passes the filter. Empty terms = nothing passes."""
    include = [t for t in terms if not t.startswith("-")]
    exclude = [t[1:] for t in terms if t.startswith("-") and len(t) > 1]
    lowered = (name or "").lower()
    if not include:
        return False  # fail closed: exclusions alone must not open the gate
    if not any(t.lower() in lowered for t in include):
        return False
    return not any(t.lower() in lowered for t in exclude)


# --- Mock -------------------------------------------------------------------

# Numbers taken from a real tenancy listing (14 Aug 2026), not invented. Mock
# data that disagrees with the cloud is how the memory-attribute bug survived:
# the mock used our own spelling and our own plausible limits, so every test
# passed while live mode read 16 GB for a shape that goes to 1760.
_MOCK_SHAPES = [
    {"name": "VM.Standard.E4.Flex", "min_ocpus": 1, "max_ocpus": 114,
     "min_memory_gb": 1, "max_memory_gb": 1760, "max_memory_per_ocpu": 64},
    {"name": "VM.Standard.E5.Flex", "min_ocpus": 1, "max_ocpus": 126,
     "min_memory_gb": 1, "max_memory_gb": 1760, "max_memory_per_ocpu": 64},
    {"name": "VM.Standard.A1.Flex", "min_ocpus": 1, "max_ocpus": 80,
     "min_memory_gb": 1, "max_memory_gb": 512, "max_memory_per_ocpu": 64},
    # A deliberately small fixed shape, so the "your shape doesn't fit" rule has
    # something to catch in mock mode.
    {"name": "VM.Standard2.1", "min_ocpus": 1, "max_ocpus": 1,
     "min_memory_gb": 15, "max_memory_gb": 15, "max_memory_per_ocpu": 0},
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


def _attr(obj, *names, default=None):
    """First present attribute among `names`.

    The OCI SDK is not self-consistent about how it renders "GBs": the same
    object carries `max_in_g_bs` AND `max_per_ocpu_in_gbs`. Reading only one
    spelling returned None and fell through to a default that looked plausible —
    16 GB — so every flex shape appeared to cap at 16 GB when E4.Flex really
    goes to 1760. Found by listing a real tenancy; mock data could not have
    caught it, because the mock used our own spelling.
    """
    for name in names:
        value = getattr(obj, name, None)
        if value is not None:
            return value
    return default


def _live_shapes(client, compartment: str) -> list[dict]:
    out = []
    # Paged for the same reason as images: a region with many availability
    # domains can push the shape list past one page, and a silently truncated
    # allowlist match is indistinguishable from a shape that does not exist.
    for s in _all_pages(client.list_shapes, compartment_id=compartment):
        # Flex shapes report a range in ocpu_options/memory_options; fixed shapes
        # report a single ocpus/memory. Normalising both to a min/max lets one
        # validation rule cover the two.
        cfg = getattr(s, "ocpu_options", None)
        mem = getattr(s, "memory_options", None)
        ocpus = getattr(s, "ocpus", None) or 0
        memory = getattr(s, "memory_in_gbs", None) or 0
        out.append({
            "name": s.shape,
            "min_ocpus": int(_attr(cfg, "min", default=None) or ocpus or 1),
            "max_ocpus": int(_attr(cfg, "max", default=None) or ocpus or 1),
            "min_memory_gb": int(_attr(mem, "min_in_g_bs", "min_in_gbs",
                                       default=None) or memory or 1),
            "max_memory_gb": int(_attr(mem, "max_in_g_bs", "max_in_gbs",
                                       default=None) or memory or 1),
            # A flex shape also caps memory PER OCPU (64 GB on E4.Flex). Without
            # this, 1 OCPU with 128 GB passes a min/max check and still cannot be
            # built — the check would wave through exactly what it exists to stop.
            "max_memory_per_ocpu": int(_attr(mem, "max_per_ocpu_in_gbs",
                                             "max_per_ocpu_in_g_bs",
                                             default=0) or 0),
        })
    return out


def _all_pages(list_call, **kwargs):  # pragma: no cover - thin SDK seam
    """Every page of an OCI list call, not just the first.

    `list_images` returns 100 rows by default and this tenancy has far more than
    that. The first page came back entirely Windows, so an "Oracle-Linux-9"
    filter matched nothing and the portal offered NO images — with no error
    anywhere, because a short list is not an error. Found by listing a real
    tenancy; a three-item mock can never surface a paging bug.
    """
    import oci
    return oci.pagination.list_call_get_all_results(list_call, **kwargs).data


def _live_images(client, compartment: str) -> list[dict]:
    return [
        {"ocid": i.id, "name": i.display_name,
         "os": getattr(i, "operating_system", "") or "",
         "os_version": getattr(i, "operating_system_version", "") or ""}
        for i in _all_pages(client.list_images, compartment_id=compartment)
    ]


def with_family(images: list[dict]) -> list[dict]:
    """Tag each image with the OS family its first-boot configuration needs.

    Resolved HERE, next to the OCI response that names the operating system,
    rather than by pattern-matching an image OCID somewhere downstream. An image
    whose OS we do not recognise gets "" and the portal declines to configure
    software on it — guessing rhel is how a machine ends up being told to run
    dnf on something that has never heard of it.
    """
    from orchestrator import configure
    return [{**i, "os_family": configure.family_for_os(i.get("os", ""))} for i in images]


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
    images = with_family([i for i in all_images if _image_allowed(i["name"], keep)])

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
