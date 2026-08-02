"""How a requested technology actually gets delivered (F-CAT — honesty layer).

The catalogue advertises far more than the orchestrator can build. A request for
"PostgreSQL 16 on OCI" is validated, priced, approved and audited — but the
provisioner has no Postgres module, so it creates a placeholder bucket and a
human on the infrastructure team does the real work. Requesters could not see
that distinction anywhere, so the catalogue promised more than the platform
delivers (see GAP-ANALYSIS.md §2).

This module is the single, server-side source of truth for that question:

    fulfilment_for(technology, target) -> {"mode": "automated"|"manual", "reason": ...}

* **automated** — the orchestrator provisions the requested thing itself, with no
  human step.
* **manual** — the portal governs the request end to end (validation, cost,
  approval, audit), then the infrastructure team fulfils it.

It deliberately mirrors `api.main._environment_resource_kind()` plus the
orchestrator's provisioning modules, so the badge shown to a requester can't
drift from what actually happens. If you add a provisioning module, update
AUTOMATED here in the same change — the tests assert the two stay in step.

Classification is **capability-based**: it answers "does the platform have an
automation path for this?", not "is that path switched on in this particular
deployment right now". Whether a path is enabled is separate configuration
(PROVISION_MODE, cloud credentials) and is surfaced on the admin console; a
capability that needs credentials carries a caveat in its reason text.
"""

from __future__ import annotations

# Technology codes that provision a real OCI Compute instance. Mirrors
# db.seed.COMPUTE_CODES / Technology.resource_kind == "oci-instance".
_OCI_COMPUTE_KIND = "oci-instance"

# Technologies delivered as a managed OCI Database with PostgreSQL system —
# the first catalogue entry provisioned as the thing actually requested.
_OCI_POSTGRES_KIND = "oci-postgres"

# (target, technology code) pairs the orchestrator genuinely builds, beyond
# compute. Object storage is the one non-compute resource with a real module.
_OCI_OBJECT_STORAGE = "oci-objectstorage"
_AWS_OBJECT_STORAGE = "aws-s3"

_REASON_MANUAL = (
    "The portal validates, prices, approves and audits this request, then the "
    "infrastructure team provisions it. It is not built automatically."
)

# Technologies with a first-boot configuration template (GAP-ANALYSIS step 4).
# Mirrors orchestrator/configure.TEMPLATES — a test asserts the two stay in step.
#
# These are deliberately still MANUAL. A template that has never been booted on a
# real VM is a plan, not a capability: package names are image-dependent and the
# install needs subnet egress. Claiming 'automated' on that basis would undo the
# honesty step 1 bought. A code moves to CONFIG_VERIFIED_CODES only after a real
# VM has been provisioned and checked.
CONFIGURABLE_CODES = {"nginx", "apache", "redis7", "java21", "python312", "nodejs20"}

# Proven on a real VM. Empty until one actually is.
CONFIG_VERIFIED_CODES: set[str] = set()

_REASON_CONFIG_PENDING = (
    "A first-boot configuration exists for this technology but has not yet been "
    "verified on a real VM, so the infrastructure team still fulfils it. The portal "
    "validates, prices, approves and audits the request as normal."
)
_REASON_NO_PATH = (
    "There is no automated provisioning path for this deployment target yet, so "
    "the infrastructure team fulfils the request after approval."
)


def fulfilment_for(technology, target: str | None) -> dict:
    """Whether `technology` on `target` is provisioned automatically.

    `technology` is a db.models.Technology (or anything exposing `.code` and
    `.resource_kind`). Returns {"mode", "reason"} — never raises, so it is safe to
    call while rendering a catalogue.
    """
    code = (getattr(technology, "code", "") or "").strip()
    kind = (getattr(technology, "resource_kind", "") or "").strip()
    tgt = (target or "").strip().lower()

    if tgt == "oci":
        if kind == _OCI_POSTGRES_KIND:
            return {"mode": "automated",
                    "reason": ("Provisioned automatically as a managed OCI Database "
                               "with PostgreSQL system. Real provisioning must be "
                               "enabled with a DB subnet and vault secret.")}
        if kind == _OCI_COMPUTE_KIND:
            return {"mode": "automated",
                    "reason": "Provisioned automatically as an OCI Compute instance."}
        if code == _OCI_OBJECT_STORAGE:
            return {"mode": "automated",
                    "reason": "Provisioned automatically as an OCI Object Storage bucket."}
        if code in CONFIG_VERIFIED_CODES:
            return {"mode": "automated",
                    "reason": ("Provisioned as an OCI Compute instance and configured "
                               "automatically at first boot.")}
        if code in CONFIGURABLE_CODES:
            return {"mode": "manual", "reason": _REASON_CONFIG_PENDING}
        return {"mode": "manual", "reason": _REASON_MANUAL}

    if tgt == "aws":
        if code == _AWS_OBJECT_STORAGE:
            return {"mode": "automated",
                    "reason": ("Provisioned automatically as an S3 bucket. Real AWS "
                               "provisioning must be enabled with credentials.")}
        return {"mode": "manual", "reason": _REASON_MANUAL}

    # on-prem, Azure and GCP have no provisioning module at all today.
    return {"mode": "manual", "reason": _REASON_NO_PATH}


def automated_targets(technology, targets: list[str]) -> list[str]:
    """Of `targets`, those where this technology is provisioned automatically."""
    return [t for t in targets if fulfilment_for(technology, t)["mode"] == "automated"]
