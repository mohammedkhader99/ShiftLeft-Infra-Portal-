"""What the component detail form may offer, and what a submission may contain.

The form asks the requester, per chosen technology, for a version and an explicit
shape (vCPU / memory / disk). That is a lot of free choice arriving from a
browser, so this module is the single place that decides which values exist —
and the same function answers both questions:

    "what should the form show?"      -> options_for()
    "is this submitted value valid?"  -> validate()

Asking one function both questions is the point. If the form built its list one
way and validation checked another, they would drift, and the gap between them is
exactly where a hand-crafted API call asking for 256 vCPU gets through. The
browser holds no authority here (ARCHITECTURE P2); it renders what this module
offers, and every value comes back through validate() before it is stored.

Where the values come from
--------------------------
NUMBERS are derived from the sizing anchors that already exist for every
technology (F-CAT-07), so all 46 work with no extra catalogue data: the offered
vCPU values are simply the vCPU values the anchors define. Anything an
administrator adds to `component_option` is merged on top.

VERSIONS come only from `component_option`, seeded short on purpose — see
db/seed.COMPONENT_VERSIONS. A technology with no version rows gets no version
dropdown rather than a fabricated one.

The hourly cloud-option fetch (next increment) writes into the same table with
source="oci-live", so it appears here without this module changing.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from db.models import ComponentOption, SizingAnchor, Technology

# The detail fields, in the order the form shows them. `numeric` fields are
# whole numbers and can be derived from the sizing anchors; `version` is text and
# must be offered explicitly.
FIELDS = ("version", "vcpu", "memory_gb", "storage_gb")
NUMERIC_FIELDS = ("vcpu", "memory_gb", "storage_gb")

LABELS = {"version": "Version", "vcpu": "vCPU", "memory_gb": "Memory",
          "storage_gb": "Disk"}

# How a value reads on screen. Bare numbers are ambiguous once three numeric
# dropdowns sit next to each other.
UNITS = {"vcpu": "", "memory_gb": " GB", "storage_gb": " GB"}


def as_dict(component) -> dict:
    """A stored RequestComponent as the plain dict sizing, pricing, policy and the
    orchestrator handoff all take.

    One function so a new detail field cannot reach the price but miss the
    machine (or the reverse). Every caller that used to write
    `{"technology_code": ..., "size": ...}` by hand goes through here.
    """
    return {
        "technology_code": component.technology_code,
        "size": component.size,
        "version": component.version,
        "vcpu": component.vcpu,
        "memory_gb": component.memory_gb,
        "storage_gb": component.storage_gb,
    }


def _anchors(session: Session, technology_code: str) -> list[SizingAnchor]:
    """Every current sizing anchor for a technology, smallest first.

    Effective-dating means a technology can have several anchors per size; the
    latest-dated one is the live shape, matching api.sizing._current_anchor.
    """
    tech = session.scalar(select(Technology).where(Technology.code == technology_code))
    if tech is None:
        return []
    rows = session.scalars(
        select(SizingAnchor)
        .where(SizingAnchor.technology_id == tech.id)
        .order_by(SizingAnchor.effective_from.desc())
    ).all()
    current: dict[str, SizingAnchor] = {}
    for row in rows:  # newest first, so the first of each size wins
        current.setdefault(row.size, row)
    return sorted(current.values(), key=lambda a: a.vcpu)


def presets(session: Session, technology_code: str) -> dict[str, dict]:
    """{size: {vcpu, memory_gb, storage_gb}} — what each size button fills in.

    The form used to carry these as a hard-coded table in the browser, which
    meant a changed anchor showed one shape and priced another.
    """
    return {
        a.size: {"vcpu": a.vcpu, "memory_gb": a.memory_gb, "storage_gb": a.storage_gb}
        for a in _anchors(session, technology_code)
    }


def _stored(session: Session, technology_code: str, target: str) -> dict[str, list[ComponentOption]]:
    """Catalogue-held options for a technology, grouped by field.

    An option with no deployment target applies everywhere; one naming a target
    applies only there.
    """
    rows = session.scalars(
        select(ComponentOption)
        .where(
            ComponentOption.technology_code == technology_code,
            ComponentOption.deployment_target.in_(("", (target or "").strip())),
        )
        .order_by(ComponentOption.sort_order, ComponentOption.id)
    ).all()
    grouped: dict[str, list[ComponentOption]] = {}
    for row in rows:
        grouped.setdefault(row.field, []).append(row)
    return grouped


def options_for(session: Session, technology_code: str, deployment_target: str = "") -> dict:
    """Everything the form needs for one component, and everything validate()
    will accept for it.

    Returns fields in FIELDS order. A field with no options is omitted entirely,
    so the form shows no empty dropdown and validate() rejects any value for it.
    """
    technology_code = (technology_code or "").strip()
    tech = session.scalar(select(Technology).where(Technology.code == technology_code))
    stored = _stored(session, technology_code, deployment_target)
    anchors = _anchors(session, technology_code)

    fields: dict[str, dict] = {}
    for field in FIELDS:
        values: list[str] = []
        default = ""
        for row in stored.get(field, []):
            if row.value not in values:
                values.append(row.value)
            if row.is_default and not default:
                default = row.value
        if field in NUMERIC_FIELDS:
            # Anchor-derived numbers, merged with anything catalogued above.
            for anchor in anchors:
                value = str(getattr(anchor, field))
                if value not in values:
                    values.append(value)
            values.sort(key=int)
        if not values:
            continue  # no honest options -> no dropdown, and nothing accepted
        fields[field] = {
            "label": LABELS[field],
            "options": [
                {"value": v, "label": f"{v}{UNITS.get(field, '')}"} for v in values
            ],
            "default": default or values[0],
        }

    return {
        "technology_code": technology_code or None,
        "technology_name": tech.name if tech else technology_code or None,
        "deployment_target": (deployment_target or "").strip() or None,
        "fields": fields,
        "presets": presets(session, technology_code),
    }


def validate(session: Session, technology_code: str, deployment_target: str,
             chosen: dict) -> dict[str, str]:
    """Errors keyed by field name; empty means every chosen value is offered.

    A field left unset is fine — it falls back to the size anchor, which is how
    every request raised before this form existed still works. A field SET to
    something not offered is refused: that is the whole point of the module.
    """
    offered = options_for(session, technology_code, deployment_target)["fields"]
    errors: dict[str, str] = {}

    for field in FIELDS:
        raw = chosen.get(field)
        if raw is None or str(raw).strip() == "":
            continue
        value = str(raw).strip()
        spec = offered.get(field)
        if spec is None:
            errors[field] = (
                f"{LABELS[field]} cannot be chosen for "
                f"{technology_code or 'this technology'}."
            )
            continue
        if value not in [o["value"] for o in spec["options"]]:
            allowed = ", ".join(o["value"] for o in spec["options"])
            errors[field] = (
                f"{LABELS[field]} '{value}' is not offered for "
                f"{technology_code or 'this technology'} — choose one of: {allowed}."
            )
    return errors


def is_custom_shape(session: Session, technology_code: str, shape: dict) -> bool:
    """True when a RESOLVED shape is not one of the technology's size presets.

    Takes the final numbers (anchor plus any override), not the raw form input,
    so there is exactly one definition of "non-standard machine" and sizing
    cannot drift from it. Note what this deliberately does NOT flag: choosing the
    'large' numbers while the size box still says medium is a standard machine
    with a stale label, not a custom build.

    Surfaced to the approver so a non-standard shape is visible at the point of
    approval rather than discovered afterwards. Routing it to a separate
    architecture review (F-CAT-11) is a later increment.
    """
    if any(shape.get(f) is None for f in NUMERIC_FIELDS):
        return False  # not fully resolved — nothing to compare
    numbers = {f: int(shape[f]) for f in NUMERIC_FIELDS}
    return numbers not in list(presets(session, technology_code).values())
