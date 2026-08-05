"""What a requester was actually given (F-INT-04).

Terraform records each resource's real identity — OCID, display name, private IP,
hostname, URL — and the portal has stored it all along in
`ProvisionedResource.details`. It was never shown, so a request reached
'provisioned' and the requester still had to ask someone where their server was.

The awkward part is that every module names its outputs differently: the shared
module emits `instance_ocid`/`instance_private_ip` (singular), Apache emits
`instance_ids`/`private_ips` (plural), object storage emits `bucket_name`. This
module maps those onto one shape so the portal can render them uniformly.

Unrecognised outputs are NOT dropped — they are passed through under `other`. A
new blueprint's outputs appearing as raw keys is untidy; disappearing silently is
the failure mode this codebase keeps having to fix.
"""

from __future__ import annotations

# Canonical field -> the output names modules actually use for it. Order matters
# only for readability; every match is collected.
_ALIASES: dict[str, tuple[str, ...]] = {
    "ocids": ("instance_ids", "instance_ocids", "instance_ocid",
              "postgres_ocid", "nsg_id"),
    "names": ("instance_display_names", "instance_name",
              "postgres_name", "bucket_name", "bucket"),
    "private_ips": ("private_ips", "instance_private_ip"),
    "public_ips": ("public_ips",),
    "hostnames": ("hostname_labels",),
    "urls": ("http_urls", "https_urls"),
    "dns": ("dns_domain",),
}

# Outputs that describe the build rather than identify the resource. Kept out of
# `other` so the panel isn't cluttered, but still reported separately.
_INFO_KEYS = ("service_ports", "configured")


def _as_list(value) -> list[str]:
    """Modules emit scalars or lists for the same concept; normalise to a list and
    drop the empties Terraform produces for unset optional attributes."""
    if value is None:
        return []
    items = value if isinstance(value, (list, tuple)) else [value]
    return [str(v) for v in items if v not in (None, "", [], {})]


def normalise(details: dict | None) -> dict:
    """Terraform outputs -> {ocids, names, private_ips, ..., info, other}.

    Never raises: this renders a page, and a malformed output must not take the
    request view down with it.
    """
    if not isinstance(details, dict):
        return {"other": {}, "info": {}}

    out: dict = {}
    consumed: set[str] = set()
    for field, aliases in _ALIASES.items():
        values: list[str] = []
        for key in aliases:
            if key in details:
                consumed.add(key)
                for v in _as_list(details[key]):
                    if v not in values:
                        values.append(v)
        if values:
            out[field] = values

    info = {k: details[k] for k in _INFO_KEYS if k in details}
    consumed.update(info)
    out["info"] = info
    # Anything a blueprint emits that this module has never heard of.
    out["other"] = {k: v for k, v in details.items()
                    if k not in consumed and v not in (None, "", [], {})}
    return out


def summarise(resource) -> dict:
    """One provisioned resource as the portal shows it.

    `resource` is a db.models.ProvisionedResource.
    """
    data = normalise(getattr(resource, "details", None))
    active = resource.lifecycle_state == "active"
    return {
        "kind": resource.kind,
        "name": resource.name,
        "region": resource.region,
        "lifecycle_state": resource.lifecycle_state,
        # A torn-down resource has no power state. The registry keeps whatever it
        # last had, so reporting it verbatim would show a terminated VM as
        # "running" — worse than showing nothing.
        "power_state": resource.power_state if active else None,
        "created_at": resource.created_at.isoformat() if resource.created_at else None,
        **data,
    }
