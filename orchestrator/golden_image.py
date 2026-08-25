"""Keep the machine that passed.

A proof build boots a real VM, installs the software, proves it healthy, and
destroys it. This module is the step in between: capture a custom image from the
proven instance before the teardown takes it away.

WHY IT COSTS NO EXTRA MACHINE. The instance already exists and has already
earned its certification. We are not building anything new — we are declining to
throw away the only artefact in the process that is worth keeping.

WHY IT MUST NEVER RAISE INTO THE PROOF. A capture is an optimisation on a path
that already works. `api/proof.py` calls this after the verdict is already
decided, and treats every failure here as "no image this time". The day a failed
capture turns a passing proof into a failing one is the day this file became a
liability, so the contract is a RESULT, never an exception.

It reuses `cloud_state`'s credentials, compartment resolution and instance
lookup rather than growing a second copy of them. Two functions answering one
question have diverged three times in this project; the fix each time was to
delete one of them.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

from orchestrator import cloud_state

#: OCI display names are not a free-for-all: they reach an API as an identifier.
_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,240}$")

#: The CODE is checked separately from the name it is folded into. Checking only
#: the assembled name let an EMPTY code through: "golden-" + "" + "-PROOF-X"
#: reads as `golden--proof-x`, every character of which is legal. The image would
#: have been created, named after nothing, and impossible to attribute.
_SAFE_CODE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,47}$")


@dataclass(frozen=True)
class Capture:
    """What happened. `ok` is the only thing a caller has to look at."""

    ok: bool
    image_ocid: str = ""
    source_instance_ocid: str = ""
    detail: str = ""
    size_gb: int | None = None


def mode() -> str:
    """Mock unless the cloud-state adapter is live.

    Deliberately tied to `CLOUD_STATE_MODE` rather than given a switch of its
    own. Capture uses exactly the same credentials, compartment and SDK as
    cloud-state, so a second switch could only ever be set to a value that
    contradicts the first one.
    """
    return cloud_state.mode()


def enabled() -> bool:
    """The master switch, read fresh so the Admin console takes effect at once."""
    return os.getenv("GOLDEN_IMAGES", "true").strip().lower() in ("1", "true", "yes", "on")


def image_name(technology_code: str, reference: str) -> str:
    """What the image is called in the console.

    Carries the proof reference on purpose. An image nobody can trace back to
    the build that justified it is an image nobody dares delete.
    """
    return f"golden-{technology_code}-{reference}".lower()[:240]


def capture(reference: str, *, technology_code: str, instance_name: str) -> Capture:
    """Take a custom image from the instance this proof built.

    Never raises. Every refusal, misconfiguration and SDK failure comes back as
    `Capture(ok=False, detail=...)` because the caller has already decided the
    proof's verdict and must not be able to change it here.
    """
    if not enabled():
        return Capture(False, detail="Golden images are switched off (GOLDEN_IMAGES).")

    if not _SAFE_CODE.match(technology_code or ""):
        return Capture(
            False,
            detail=f"Refusing to capture an image for technology {technology_code!r}.")

    name = image_name(technology_code, reference)
    if not _SAFE_NAME.match(name):
        return Capture(False, detail=f"Refusing to name an image {name!r}.")

    if mode() != "live":
        # A deterministic stand-in. It must be obviously fake: an OCID-shaped
        # string that reached a real API call would be a much worse bug than one
        # that fails loudly.
        return Capture(True, image_ocid=f"ocid1.image.mock.{name}",
                       source_instance_ocid=f"ocid1.instance.mock.{instance_name}",
                       detail=f"Mock capture of {instance_name}.", size_gb=50)

    try:
        cloud_state._require_oci_creds()
        client = cloud_state._compute_client()
        compartment = cloud_state._compute_compartment()
        if not compartment:
            return Capture(False, detail="No compute compartment is configured.")

        instance = cloud_state._find_instance(client, compartment, instance_name)
        if instance is None:
            return Capture(
                False,
                detail=(f"No live instance matching {instance_name!r} in the "
                        f"compartment — nothing to capture."))

        return _create(client, compartment, instance, name)
    except Exception as exc:  # noqa: BLE001 - a capture failure is never fatal
        return Capture(False, detail=f"{type(exc).__name__}: {exc}"[:400])


def state_of(image_ocids: list[str]) -> dict[str, str]:
    """`{ocid: lifecycle_state}` — what OCI says about images we captured.

    Only the cloud knows when a ten-to-twenty-minute image build has finished,
    and until it has, the image cannot boot anything.

    An OCID we cannot get an answer for is simply ABSENT from the result rather
    than reported as broken. "I could not ask" and "it failed" are different
    facts, and collapsing them would fail perfectly good images every time the
    network hiccuped.
    """
    wanted = [o for o in (image_ocids or []) if o]
    if not wanted:
        return {}

    if mode() != "live":
        # The mock capture hands back `ocid1.image.mock.<name>`, and it is ready
        # the moment it exists — there is no build to wait for.
        return {o: "AVAILABLE" for o in wanted if o.startswith("ocid1.image.mock.")}

    try:
        cloud_state._require_oci_creds()
        client = cloud_state._compute_client()
    except Exception:  # noqa: BLE001 - cannot ask is not the same as failed
        return {}

    found: dict[str, str] = {}
    for ocid in wanted:
        try:
            found[ocid] = client.get_image(ocid).data.lifecycle_state
        except Exception:  # noqa: BLE001 - per image, so one bad OCID is not all of them
            continue
    return found


def delete(image_ocid: str) -> tuple[bool, str]:
    """Remove a custom image we captured. `(ok, detail)`, never raises.

    THE CALLER NAMES THE OCID, and the caller only ever names one its own table
    recorded. This function deliberately has no "find images that look like
    ours and remove them" mode: a rule that selected by tag or name prefix would,
    one typo later, be a rule that deleted somebody else's image.

    An image already gone counts as SUCCESS. The goal is that it does not exist
    and is not billed for; a 404 is that goal, reached by someone else.
    """
    if not image_ocid or not image_ocid.startswith("ocid1.image"):
        return False, f"Refusing to delete {image_ocid!r}: not an image OCID."

    if mode() != "live":
        return True, f"Mock delete of {image_ocid}."

    try:
        cloud_state._require_oci_creds()
        client = cloud_state._compute_client()
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"[:300]

    try:
        client.delete_image(image_ocid)
        return True, f"Deleted {image_ocid}."
    except Exception as exc:  # noqa: BLE001
        if getattr(exc, "status", None) == 404 or "NotAuthorizedOrNotFound" in str(exc):
            return True, f"{image_ocid} is already gone."
        return False, f"{type(exc).__name__}: {exc}"[:300]


def _create(client, compartment: str, instance, name: str) -> Capture:  # pragma: no cover - SDK seam
    """The one call that makes a real, billable thing.

    Returned as soon as OCI accepts it, in PROVISIONING. Waiting for AVAILABLE
    would hold the proof open for the ten to twenty minutes an image takes, and
    the instance can be destroyed the moment OCI has taken its copy — the image
    build continues without it.
    """
    import oci

    details = oci.core.models.CreateImageDetails(
        compartment_id=compartment,
        instance_id=instance.id,
        display_name=name,
        freeform_tags={"origin": "shiftleft-proof", "source_instance": instance.id},
    )
    image = client.create_image(details).data
    return Capture(
        True,
        image_ocid=image.id,
        source_instance_ocid=instance.id,
        detail=f"Capturing {name} from {instance.id}; OCI reports {image.lifecycle_state}.",
        size_gb=getattr(image, "size_in_mbs", None) and int(image.size_in_mbs / 1024) or None,
    )
