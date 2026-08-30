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
from sqlalchemy.orm import Session, object_session

from api import blueprint_capabilities, network_egress
from db.models import ComponentOption, SizingAnchor, Technology

# The detail fields, in the order the form shows them. `numeric` fields are
# whole numbers and can be derived from the sizing anchors; `version` is text and
# must be offered explicitly.
FIELDS = ("version", "image", "vcpu", "memory_gb", "storage_gb")
NUMERIC_FIELDS = ("vcpu", "memory_gb", "storage_gb")

LABELS = {"version": "Version", "image": "OS image", "vcpu": "vCPU",
          "memory_gb": "Memory", "storage_gb": "Disk"}

# An option row with this technology_code applies to every technology. Used for
# things that belong to the machine rather than the software on it, like the OS
# image the hourly cloud fetch caches.
ALL_TECHNOLOGIES = ""

# Cached shape rows are NOT offered as a dropdown — see cloud_options._rows_from.
# They are read here to check that a requested shape can actually be built.
_SHAPE_FIELD = "shape"

# How a value reads on screen. Bare numbers are ambiguous once three numeric
# dropdowns sit next to each other.
UNITS = {"vcpu": "", "memory_gb": " GB", "storage_gb": " GB"}


def as_dict(component) -> dict:
    """A stored RequestComponent as the plain dict sizing, pricing, policy and the
    orchestrator handoff all take.

    One function so a new detail field cannot reach the price but miss the
    machine (or the reverse). Every caller that used to write
    `{"technology_code": ..., "size": ...}` by hand goes through here.

    The chosen image's OS FAMILY is resolved and carried along. The orchestrator
    needs it to pick a package manager and cannot look it up itself — the cached
    image catalogue lives in this database. The session comes from the component
    itself (object_session) rather than from a parameter, because this is called
    from eight places and a signature nobody can forget to fill in is worth more
    than an explicit one they can.
    """
    out = {
        "technology_code": component.technology_code,
        "size": component.size,
        "version": component.version,
        "image": component.image,
        "vcpu": component.vcpu,
        "memory_gb": component.memory_gb,
        "storage_gb": component.storage_gb,
    }
    session = object_session(component)
    if session is not None and component.image:
        family = os_family_for_image(session, component.image)
        if family:
            out["os_family"] = family

    # HOW THE CATALOGUE SAYS THIS IS DELIVERED, carried to the machine.
    #
    # `machine` means the technology IS the machine — compute-vm, rhel9 — and
    # has nothing to install by design. Without this the boot script cannot tell
    # "a bare VM, as asked for" from "software we failed to install", writes
    # PORTAL FAILURE for both, and the verdict fails a perfectly healthy VM.
    # REQ-2026-0208, and every bare VM request since the gate went on.
    #
    # Read from the catalogue, never inferred from the absence of a recipe: a
    # recipe missing BY MISTAKE would otherwise look exactly like a bare machine
    # and pass silently, which is the failure this whole gate exists to prevent.
    if session is not None:
        delivers = _delivery_model(session, component.technology_code)
        if delivers:
            out["delivers"] = delivers
    return out


def _delivery_model(session: Session, code: str) -> str:
    """The catalogue's delivery model for a technology, or "" if unclassified.

    "" is deliberately not a default of "software": unclassified and software
    are different facts, and the caller must not treat one as the other.
    """
    from db.models import TechnologyDelivery

    # NO AUTOFLUSH. This is a read, called from a serialiser, and SQLAlchemy
    # would otherwise flush whatever is pending in the session first — turning
    # "describe this component" into "write half-built state to the database"
    # at a moment the caller never chose.
    with session.no_autoflush:
        row = session.get(TechnologyDelivery, (code or "").strip().lower())
    return (row.delivery_model or "") if row is not None else ""


def os_family_for_image(session: Session, image_ocid: str) -> str:
    """The OS family an image needs, from the cached catalogue, or "".

    "" when the image is unknown or its OS was not recognised. The caller must
    treat that as "do not claim to know", never as a licence to assume Red Hat —
    that assumption is what tells an Ubuntu machine to run dnf.
    """
    row = session.scalar(
        select(ComponentOption).where(
            ComponentOption.field == "image",
            ComponentOption.value == (image_ocid or "").strip(),
        )
    )
    return ((row.attributes or {}).get("os_family") or "") if row else ""


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

    Two wildcards, both meaning "applies more widely than this row's key":
    an empty deployment_target applies on every target, and an empty
    technology_code applies to every technology. The second is how a fetched OS
    image — a property of the machine, not of nginx — is stored once rather than
    46 times.
    """
    rows = session.scalars(
        select(ComponentOption)
        .where(
            ComponentOption.technology_code.in_((ALL_TECHNOLOGIES, technology_code)),
            ComponentOption.deployment_target.in_(("", (target or "").strip())),
        )
        .order_by(ComponentOption.sort_order, ComponentOption.id)
    ).all()
    grouped: dict[str, list[ComponentOption]] = {}
    for row in rows:
        grouped.setdefault(row.field, []).append(row)
    return grouped


# Versions are delivered by Red Hat module streams (GAP-ANALYSIS step 9). Debian
# has no equivalent — you get whatever the release carries — so a version chosen
# against an Ubuntu image could not be honoured, and offering it would be the
# Redis-6-sold-as-7 bug with a different distribution.
_FAMILIES_THAT_CAN_PIN_A_VERSION = {"rhel"}


def _image_suits(row: ComponentOption, technology_code: str) -> bool:
    """Whether an OS image belongs on this technology's dropdown.

    An image whose family we cannot read is SHOWN rather than hidden: the portal
    not recognising an operating system is our gap, and silently removing a
    legitimate image over it would be a worse failure than letting validation
    have the final word.
    """
    families = supported_families(technology_code)
    if families is None:
        # We could not read what this technology supports. That is OUR gap, and
        # hiding a legitimate image over it would be worse than showing one that
        # validation can refuse later.
        return True
    if not families:
        # DECLARED none, which is a different fact entirely: this thing
        # configures no operating system, so no image applies to it.
        #
        # A bucket, a managed database and an OKE cluster were all being offered
        # an OS image dropdown. For OKE the choice was not merely useless but
        # misleading — its worker image is selected by OCI from those compatible
        # with the cluster's Kubernetes version, so a requester picking Ubuntu
        # got Oracle Linux nodes and no explanation.
        return False
    family = (row.attributes or {}).get("os_family")
    if family and not network_egress.can_install(family, _fetch_egress):
        # The machine would build, boot, and find no repository to install from.
        # REQ-2026-0134 is what that looks like: Apache on Oracle Linux served on
        # :80 while nginx on Ubuntu, same subnet, same request, reported
        # `nginx NOT INSTALLED` — because Oracle's mirrors are inside the Oracle
        # Services Network and Ubuntu's are not.
        return False
    return not family or family in families


def options_for(session: Session, technology_code: str, deployment_target: str = "",
                image: str = "") -> dict:
    """Everything the form needs for one component, and everything validate()
    will accept for it.

    Returns fields in FIELDS order. A field with no options is omitted entirely,
    so the form shows no empty dropdown and validate() rejects any value for it.

    `image` is the OS image the requester has picked. It narrows what is offered:
    software with no install recipe for that image's OS family, and versions that
    family cannot pin, are withdrawn rather than collected and discarded.
    """
    technology_code = (technology_code or "").strip()
    family = os_family_for_image(session, image) if image else ""
    tech = session.scalar(select(Technology).where(Technology.code == technology_code))
    stored = _stored(session, technology_code, deployment_target)
    anchors = _anchors(session, technology_code)

    fields: dict[str, dict] = {}
    for field in FIELDS:
        # A version the chosen image's OS cannot pin is not a choice, it is a
        # wish. Withdraw the dropdown rather than record an answer nothing acts on.
        if (field == "version" and family
                and family not in _FAMILIES_THAT_CAN_PIN_A_VERSION):
            continue
        values: list[str] = []
        default = ""
        # A stored row may carry its own display text. That is essential for an
        # OS image, whose value is an OCID nobody can read — the requester picks
        # by "Oracle-Linux-9.4-2026.01.31-0", not by ocid1.image..
        captions: dict[str, str] = {}
        for row in stored.get(field, []):
            # THE OS IMAGE LIST IS FILTERED TO WHAT THIS TECHNOLOGY RUNS ON.
            #
            # Refusing a bad combination at submit is a poor second to never
            # offering it: the requester has by then filled in a whole form, and
            # the rejection reads as the portal changing its mind. An image the
            # platform cannot install this software on is not a choice, so it is
            # not shown.
            #
            # This narrows and widens by itself. A technology certified on
            # another OS tomorrow gains those images the moment its blueprint
            # declares the family — nobody edits a list of images per technology.
            #
            # The dropdown is still only UX. validate() re-checks every submitted
            # value against this same function, so a hand-crafted request naming
            # a hidden image is refused all the same (ARCHITECTURE P2).
            if field == "image" and not _image_suits(row, technology_code):
                continue
            # A VERSION THE CHOSEN OS CANNOT ACTUALLY PIN IS NOT A CHOICE.
            #
            # The catalogue offered nginx 1.20, 1.22 and 1.24. Oracle Linux 9.8
            # ships streams 1.22, 1.24 and 1.26 — measured on the machine from
            # REQ-2026-0146. There is no 1.20 stream, so `dnf module enable
            # nginx:1.20` fails and the request lands verify-failed, while the
            # non-modular 1.20.1 installs anyway and version_nginx reports OK:
            # the requester got what they asked for by accident with the pinning
            # step broken. And 1.26 was silently unavailable.
            #
            # A seeded list that was true once, with nothing checking it against
            # a machine. Measured streams win where they exist; where nothing has
            # been measured the catalogue's list stands, because an empty answer
            # is our gap and must not withdraw every version over it.
            if field == "version" and family:
                measured = blueprint_capabilities.streams(technology_code, family)
                if measured and row.value not in measured:
                    continue
            if row.value not in values:
                values.append(row.value)
            if row.label and row.label != row.value:
                captions[row.value] = row.label
            if row.is_default and not default:
                default = row.value
        if field == "version" and family:
            # The OS may offer a stream the catalogue has never heard of. 1.26 is
            # on Oracle Linux 9.8 and was missing from the seeded list, so a
            # filter alone would have kept the form a version behind the machine.
            for stream in blueprint_capabilities.streams(technology_code, family):
                if stream not in values:
                    values.append(stream)
            if default and default not in values:
                default = values[0] if values else ""
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
                {"value": v, "label": captions.get(v, f"{v}{UNITS.get(field, '')}")}
                for v in values
            ],
            "default": default or values[0],
        }

    return {
        "technology_code": technology_code or None,
        "technology_name": tech.name if tech else technology_code or None,
        "deployment_target": (deployment_target or "").strip() or None,
        "fields": fields,
        "presets": presets(session, technology_code),
        # HOW THIS COMPONENT WILL BE DELIVERED — a managed service the cloud
        # runs, or a machine we build. Reported, not asked: see delivery_for.
        "delivery": delivery_for(session, technology_code, deployment_target),
        # The chosen image's OS family, and whether this technology can actually
        # be installed on it. The form shows the warning; validation refuses the
        # combination outright — see validate().
        "os_family": family or None,
        "installable": (not family) or installable_on(technology_code, family),
        # Whether the OS filtering is actually in force. False means the
        # orchestrator could not be reached, so nothing was filtered — reported
        # rather than left to look like "everything is allowed", which is how the
        # rule silently did nothing in production for its first day.
        "capabilities_known": blueprint_capabilities.known(),
        # Same disclosure for the network check: False means images were
        # not filtered on what this subnet can reach.
        "network_known": network_egress.known(),
    }


#: How a delivery reads on screen, and what it means in one line.
DELIVERY_LABELS = {
    "managed": ("Managed service",
                "the cloud provider runs it — there is no machine to patch"),
    "vm": ("Virtual machine",
           "a machine we build, with the software installed on it"),
}


def delivery_for(session: Session, technology_code: str,
                 deployment_target: str = "") -> dict:
    """How this technology will be delivered, from the blueprint that builds it.

    WHY THE REQUESTER IS TOLD. "PostgreSQL" and "Kafka" sit next to each other on
    one catalogue and arrive as completely different things: one is a database
    OCI runs, the other is three machines somebody has to patch. Nothing on the
    form said so. A requester learned it from the bill, or from being handed a
    machine they did not know they now owned.

    READ FROM THE BLUEPRINT THAT WILL ACTUALLY BUILD IT, never from the
    technology's name or its catalogue classification. Inferring it from the code
    is the mistake db/models.TechnologyDelivery was created to end: `postgres16`
    reads as software by that rule and is a managed service.

    Returns {options, chosen, known}. `known` False means the orchestrator could
    not be reached, and the form must say "not known" rather than show nothing —
    an unreachable orchestrator and a technology nothing builds are different
    facts, and only one of them is the requester's problem.

    MORE THAN ONE OPTION IS REPORTED, NOT CHOSEN. No technology has two today,
    and the certification table cannot hold two: `blueprint` is keyed on
    (technology, target), so a second certified delivery has nowhere to live.
    Until that is widened the portal must not offer a choice it cannot honour —
    which is the whole reason this reports what WILL happen rather than asking.
    """
    options = blueprint_capabilities.deliveries_for(technology_code,
                                                    _fetch_blueprints)
    target = (deployment_target or "").strip().lower()
    if target:
        options = [o for o in options
                   if not o.get("target") or o["target"] == target]
    out = []
    for option in options:
        label, meaning = DELIVERY_LABELS.get(
            option["delivery"], (option["delivery"].title() or "Unclassified", ""))
        out.append({**option, "label": label, "meaning": meaning})
    return {
        "options": out,
        # What the platform will use. One option means one answer; with more than
        # one the portal states the first rather than implying a choice exists.
        "chosen": out[0]["delivery"] if out else None,
        "known": blueprint_capabilities.known(),
    }


def supported_families(technology_code: str) -> set[str] | None:
    """OS families the thing that ACTUALLY BUILDS this technology can configure.

    Asked of the ORCHESTRATOR, which owns the blueprints — over the signed
    channel the API already uses, never by importing its code. The API image does
    not contain the orchestrator package, so an import here raises in production
    while passing every test, and the except branch silently switched the whole
    rule off. See api/blueprint_capabilities.

    Returns None when no answer is available: either the orchestrator could not
    be reached, or no blueprint claims this technology. Callers must not read
    None as "unrestricted" without saying so — that conflation is exactly what
    let the portal accept Apache on Ubuntu.

    Why the blueprint and not configure.py: configure.py carries a Debian recipe
    for apache, and apache is built by oci/apache-httpd, a module that renders
    its own Red Hat-only cloud-init and never calls configure.py at all.
    """
    return blueprint_capabilities.families_for(technology_code, _fetch_blueprints)


def _fetch_blueprints() -> list[dict]:
    """Ask the orchestrator what it ships. Imported lazily to avoid a cycle."""
    from api.main import _orchestrator_blueprints
    return _orchestrator_blueprints() or []


def _fetch_egress() -> dict:
    """Ask the orchestrator what the build network can reach. Lazy, as above."""
    from api.main import _orchestrator_network_egress
    return _orchestrator_network_egress() or {}


def installable_on(technology_code: str, family: str) -> bool:
    """Whether the platform can really install this technology on this OS.

    Three things mean "no opinion, allow", and conflating any with "no" would
    block far more than it protects:

      * an OS family we failed to recognise,
      * a technology no blueprint claims — an object storage bucket or a managed
        database installs nothing on a machine, so the image is irrelevant to it
        (reading its empty declaration as "supports no operating system" refused
        every image for buckets, which a test caught before it shipped), and
      * capabilities we could not read at all, which is NOT the same thing and is
        reported separately by options_for so an inert filter is visible rather
        than looking permissive.
    """
    if not family:
        return True
    families = supported_families(technology_code)
    if not families:
        return True
    return family in families


# One Oracle OCPU is two x86 vCPUs. The orchestrator converts the same way when
# it sizes the flex shape (orchestrator/main._instance_sizing), so the check here
# is against the number the machine will actually be asked for.
_VCPU_PER_OCPU = 2


def cached_shapes(session: Session, deployment_target: str = "oci") -> list[dict]:
    """Compute shapes the hourly fetch cached, with their OCPU/memory limits.

    Empty when the fetch has never run or nothing is allow-listed — in which case
    the fit check below is skipped rather than failing everything. Absent data is
    not evidence that a shape is too small.
    """
    rows = session.scalars(
        select(ComponentOption).where(
            ComponentOption.field == _SHAPE_FIELD,
            ComponentOption.deployment_target.in_(("", (deployment_target or "").strip())),
        )
    ).all()
    shapes = []
    for row in rows:
        limits = row.attributes or {}
        if not limits.get("max_ocpus"):
            continue  # a shape row with no limits tells us nothing
        shapes.append({"name": row.value, **limits})
    return shapes


def _shape_hosts(shape: dict, ocpus: int, memory_gb: int) -> bool:
    """Whether one shape can actually be built at this size."""
    if not (shape.get("min_ocpus", 1) <= ocpus <= shape.get("max_ocpus", 1)):
        return False
    if not (shape.get("min_memory_gb", 1) <= memory_gb <= shape.get("max_memory_gb", 1)):
        return False
    # A flex shape also caps memory PER OCPU — 64 GB on E4.Flex. One OCPU with
    # 128 GB is inside both ranges above and still cannot be built, so without
    # this the check waves through exactly what it exists to stop.
    per_ocpu = shape.get("max_memory_per_ocpu") or 0
    if per_ocpu and memory_gb > ocpus * per_ocpu:
        return False
    return True


def shape_fit_error(session: Session, deployment_target: str, vcpu, memory_gb) -> str:
    """"" if some allowed shape can host this, else why not.

    Catches at request time what would otherwise fail at apply time, after
    approval — the requester finding out from a Terraform error that the machine
    they were promised cannot exist.
    """
    shapes = cached_shapes(session, deployment_target)
    if not shapes or vcpu in (None, "") or memory_gb in (None, ""):
        return ""
    try:
        ocpus = max(1, round(int(vcpu) / _VCPU_PER_OCPU))
        memory = int(memory_gb)
    except (TypeError, ValueError):
        return ""
    if any(_shape_hosts(s, ocpus, memory) for s in shapes):
        return ""

    # Say which limit was hit. "Too big" and "too much memory for that many
    # cores" need different fixes, and a message that does not distinguish them
    # leaves the requester guessing.
    fits_cpu = [s for s in shapes if s.get("min_ocpus", 1) <= ocpus <= s.get("max_ocpus", 1)]
    if fits_cpu:
        best = max(fits_cpu, key=lambda s: min(
            s.get("max_memory_gb", 0),
            ocpus * (s.get("max_memory_per_ocpu") or s.get("max_memory_gb", 0))))
        ceiling = min(best.get("max_memory_gb", 0),
                      ocpus * (best.get("max_memory_per_ocpu")
                               or best.get("max_memory_gb", 0)))
        return (f"{memory} GB of memory is more than any approved shape allows "
                f"with {vcpu} vCPU. The most {best['name']} can pair with "
                f"{vcpu} vCPU is {ceiling} GB — either lower the memory or raise "
                f"the vCPU.")
    largest = max(shapes, key=lambda s: s.get("max_ocpus", 0))
    return (f"No approved machine shape can provide {vcpu} vCPU. The largest "
            f"available is {largest['name']} at "
            f"{largest['max_ocpus'] * _VCPU_PER_OCPU} vCPU.")


def validate(session: Session, technology_code: str, deployment_target: str,
             chosen: dict) -> dict[str, str]:
    """Errors keyed by field name; empty means every chosen value is offered.

    A field left unset is fine — it falls back to the size anchor, which is how
    every request raised before this form existed still works. A field SET to
    something not offered is refused: that is the whole point of the module.
    """
    chosen_image = str(chosen.get("image") or "").strip()
    offered_all = options_for(session, technology_code, deployment_target, chosen_image)
    offered = offered_all["fields"]
    errors: dict[str, str] = {}

    # Can this OS get its packages here AT ALL? Asked first, because it is the
    # most basic question about a machine and the one nobody was asking: the
    # image is real, the technology is real, the recipe supports the family, and
    # the machine still comes up empty because the subnet cannot reach the
    # repository. The dropdown already hides these; this is the authority, and it
    # also catches a stale draft or a value posted straight to the API.
    family = offered_all["os_family"]
    if family and not network_egress.can_install(family, _fetch_egress):
        errors["image"] = network_egress.guidance(family, _fetch_egress)
        return errors

    # THE SAME QUESTION, ASKED PER TECHNOLOGY. The check above is keyed on OS
    # family, and a family answer cannot see that ONE technology on a perfectly
    # supported OS still needs the public internet: an archive install fetches
    # its software from a release host, so keycloak on Oracle Linux installs its
    # java dependency from Oracle's mirrors and then cannot reach github.
    #
    # Without this the portal spends a real sandbox machine to learn what the
    # subnet's route table already said. The orchestrator declares the need
    # (blueprint manifests, plus any agent-written profile that fetches an
    # archive) and reports it with the rest of the capabilities.
    if (blueprint_capabilities.needs_internet(technology_code)
            and not network_egress.reaches_internet(_fetch_egress)):
        errors["technology_code"] = network_egress.archive_guidance(
            technology_code, _fetch_egress)
        return errors

    # The combination check, before the per-field ones: picking an Ubuntu image
    # for software we can only install on Red Hat would otherwise sail through —
    # the image is offered and the technology is offered, and the machine would
    # boot, report success and install nothing.
    # A combination a real machine DISPROVED gets its own message. "the recipe
    # only supports rhel" would be true and misleading here: the recipe supports
    # it, it was built, and it delivered the wrong thing. The requester deserves
    # the measurement, not a shrug.
    measured = blueprint_capabilities.refusal(technology_code, family)
    if measured:
        errors["image"] = (
            f"{technology_code} was built on this operating system and did not "
            f"deliver what the catalogue promises: {measured}. Until the recipe "
            f"is fixed, choose a different OS image for it, or remove "
            f"{technology_code} from the request.")
        return errors

    if not offered_all["installable"]:
        can = supported_families(technology_code)
        # Name an image the requester can actually pick, not just a family name.
        # "it can install it on: rhel" is true and useless — nobody chooses
        # "rhel" from a dropdown, they choose "Oracle-Linux-9.8-2026.07.20-0".
        suggestions = [
            row.label for row in session.scalars(
                select(ComponentOption).where(
                    ComponentOption.field == "image",
                    ComponentOption.deployment_target.in_(
                        ("", (deployment_target or "").strip())),
                ).order_by(ComponentOption.sort_order)
            ).all()
            if (row.attributes or {}).get("os_family") in can
        ][:2]
        instead = (f" Choose one of these instead: {', '.join(suggestions)}."
                   if suggestions else "")
        errors["image"] = (
            f"{technology_code} cannot be installed on this image — the "
            f"platform's recipe for it only supports "
            f"{', '.join(sorted(can)) or 'no operating system yet'}, and this "
            f"image is {family}.{instead} Or remove {technology_code} from the "
            f"request."
        )
        return errors

    for field in FIELDS:
        raw = chosen.get(field)
        if raw is None or str(raw).strip() == "":
            continue
        value = str(raw).strip()
        spec = offered.get(field)
        if spec is None:
            # Say WHY it is not on offer. "Version cannot be chosen" leaves the
            # requester with nothing to act on, and when the field has been
            # withdrawn by an image choice the control is not even on screen to
            # point at.
            family = offered_all["os_family"]
            if field == "version" and family:
                errors[field] = (
                    f"The chosen OS image installs whichever {technology_code} "
                    f"version its release carries, so a version cannot be pinned "
                    f"on it. Clear the version, or choose an Oracle Linux image "
                    f"if you need a specific one."
                )
            else:
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

    # A combination each of whose parts is offered can still be unbuildable: 16
    # vCPU and 4 GB are both on their dropdowns, and no shape provides both.
    # Only checked when the hourly fetch has actually cached shapes.
    if "vcpu" not in errors and "memory_gb" not in errors:
        fit = shape_fit_error(session, deployment_target,
                              chosen.get("vcpu"), chosen.get("memory_gb"))
        if fit:
            errors["vcpu"] = fit
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
