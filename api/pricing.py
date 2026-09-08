"""Cost estimation (increment 1.5; Azure live pricing added in 2.1).

Prices a request's components per deployment target (on-prem / Azure / OCI).
Discount-aware (F-FIN-04). On-prem and OCI price from the seeded rate cards;
Azure compute prices live from the public Retail Prices API when
AZURE_PRICING_MODE=live, falling back to the cached rate cards if Azure is
unreachable (§13). All figures are authoritative server-side (P2).

Returns one-time, monthly, and annual totals plus a per-component line-item
breakdown, in AED.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from api.adapters import aws_pricing, azure_pricing, gcp_pricing, oci_pricing
from api.adapters.aws_pricing import AWSUnavailable
from api.adapters.azure_pricing import AzureUnavailable
from api.adapters.gcp_pricing import GCPUnavailable
from api.adapters.oci_pricing import OCIUnavailable
from api.sizing import resolve_components
from db.models import Blueprint, RateCard

HOURS_PER_MONTH = 730  # standard cloud billing month
CURRENCY = "AED"

# Which rate_card.kind holds each target's resource rates.
TARGET_KIND = {"onprem": "onprem", "azure": "cloud_azure", "oci": "cloud_oci",
               "aws": "cloud_aws", "gcp": "cloud_gcp"}
DEPLOYMENT_TARGETS = set(TARGET_KIND)

# Which licence (if any) a technology carries. Mock config for now; a proper
# technology->licence link can replace this later without touching pricing.
TECHNOLOGY_LICENCE = {
    "postgres16": "postgres-licence",
    "win2019": "windows-licence",
    # Catalog expansion (6.2): commercial DB licences.
    "oracle-db": "oracle-licence",
    "mssql": "mssql-licence",
}

# --- What kind of thing is this, and how is that kind billed? ---------------
#
# FOUND 2026-08-21, proving oci-objectstorage. Every OCI component priced at
# exactly the same monthly figure, because the estimate came from the SIZE alone:
# small/medium/large chose a VM shape and that shape's compute was charged to
# everything. A bucket was billed AED 158.85 of compute for CPUs it does not
# have; so were a Kubernetes cluster and a managed database.
#
# The kind is not guessed. It comes from the blueprint — the recipe that will
# actually be built — and an unrecognised kind is priced at nothing and marked
# unresolved, never quietly billed as a VM.
#
# WHY NOT Technology.resource_kind: it defaults to "oci-bucket" and is wrong for
# seven of the nine certified components (it calls NGINX a bucket). Falling back
# to it would price a VM as storage — roughly AED 1 instead of 162 — and an
# under-estimate is the dangerous direction: it slips under every cost cap and
# then builds the expensive thing anyway.
BILLING_VM = "vm"                  # compute + storage, the original model
BILLING_BUCKET = "bucket"          # storage only, tiered, no compute at all
BILLING_CLUSTER = "cluster"        # a flat control-plane fee + its nodes
BILLING_MANAGED_DB = "managed-db"  # its own OCPU rate + storage

# The rate items each not-a-VM model CANNOT be priced without.
#
# FOUND 2026-08-21, minutes after the models above were written. The code was
# right and the running database had never been given the new rate rows — seeding
# only ever happened on a fresh database — so `resource.get("oke-cluster-hour",
# 0.0)` priced a Kubernetes control plane at nothing and reported AED 162.25 with
# every appearance of confidence. A default of zero on a missing PRICE is the
# same sin this whole change exists to fix: an answer where there is no answer.
#
# Deliberately not declared for the VM model. That one is long-standing, spans
# five targets with different key sets (on-prem bills per vCPU, clouds per
# vCPU-hour) and works; tightening it here would risk what already functions to
# guard against a fault it has never had.
REQUIRED_RATES = {
    BILLING_BUCKET: ("bucket-storage-gb-month",),
    BILLING_CLUSTER: ("oke-cluster-hour", "vcpu-hour", "memory-gb-hour"),
    BILLING_MANAGED_DB: ("psql-vcpu-hour", "storage-gb-month"),
}

# Keys in a rate dict that are ALLOWANCES, not prices. A negotiated discount
# multiplies every rate; applying it to "the first 10 GB are free" would quietly
# reduce the free tier to 8.5 GB, which is not what a discount means.
NOT_A_RATE = {"bucket-free-gb"}

BILLING_MODEL = {
    # Machines. One VM shape, one price — correct as it always was.
    "oci-service-vm": BILLING_VM,
    "oci-apache": BILLING_VM,
    "oci-instance": BILLING_VM,
    "onprem-vm": BILLING_VM,
    # Not machines.
    "oci-bucket": BILLING_BUCKET,
    "oci-oke": BILLING_CLUSTER,
    "oci-postgres": BILLING_MANAGED_DB,
    "oci-psql": BILLING_MANAGED_DB,
}

# Advanced-option cost modifiers (6.5). Transparent, documented mock/demo
# constants; an unset option contributes nothing, so base totals are unchanged.
HA_COMPUTE_MULTIPLIER = 2.0                                # high_availability doubles compute
BACKUP_FACTOR = {"7": 0.10, "30": 0.30, "90": 0.60}       # x monthly storage cost, by retention days
MONITORING_MONTHLY = {"basic": 150.0, "enhanced": 500.0}  # flat monthly, by level
SUPPORT_PCT = {"business": 0.10, "premium": 0.20}         # x infra subtotal, by tier


def _apply_discount(rates: dict[str, float], discount: float) -> dict[str, float]:
    """Discount the prices and leave the allowances alone (see NOT_A_RATE)."""
    return {item: (value if item in NOT_A_RATE else value * (1 - discount))
            for item, value in rates.items()}


def _rates(session: Session, kind: str) -> dict[str, float]:
    """Return {item: discounted rate} for one rate-card kind."""
    rows = session.scalars(select(RateCard).where(RateCard.kind == kind)).all()
    return {
        item: value for item, value in (
            (row.item, float(row.rate) if row.item in NOT_A_RATE
             else float(row.rate) * (1 - float(row.discount_pct) / 100))
            for row in rows
        )
    }


def _discount(session: Session, kind: str) -> float:
    """Negotiated discount fraction (0-1) for a rate-card kind (F-FIN-04)."""
    row = session.scalar(select(RateCard).where(RateCard.kind == kind))
    return float(row.discount_pct) / 100 if row else 0.0


def _cloud_compute_cached(resource: dict, vcpu: int, memory_gb: int) -> float:
    """Decomposed cached cloud compute (per-hour rates x 730)."""
    return (
        vcpu * resource.get("vcpu-hour", 0)
        + memory_gb * resource.get("memory-gb-hour", 0)
    ) * HOURS_PER_MONTH


def _component_parts(target: str, resource: dict, vcpu: int, memory_gb: int, storage_gb: int) -> tuple[float, float]:
    """(compute_monthly, storage_monthly) for one component, per target's rates.

    Splitting compute from storage lets the breakdown report a per-category
    cost (6.4); their sum equals the previous single resource figure, so totals
    are unchanged.
    """
    if target == "onprem":
        compute = vcpu * resource.get("vcpu", 0) + memory_gb * resource.get("memory-gb", 0)
        storage = storage_gb * resource.get("storage-gb", 0)
    else:
        # Cloud: compute billed per hour, storage per GB/month.
        compute = (
            vcpu * resource.get("vcpu-hour", 0) + memory_gb * resource.get("memory-gb-hour", 0)
        ) * HOURS_PER_MONTH
        storage = storage_gb * resource.get("storage-gb-month", 0)
    return compute, storage


def resource_kind_for(component: dict, session: Session, target: str) -> str | None:
    """Which cloud resource this component becomes, or None if nothing says.

    Order matters, and THE BLUEPRINT WINS:
      1. the certified blueprint for (technology, target) — authoritative for
         anything a user can actually order;
      2. only if none exists, an explicit "resource_kind" on the component.

    That order is a security property, not a preference. `components` arrives
    from the browser on /api/cost, so a caller that could override a certified
    component's kind could declare a virtual machine to be a bucket and have it
    priced at AED 3 instead of 162 — the client holding authority over its own
    price, which the architecture forbids (P2). Step 2 exists only for the case
    where nothing else CAN know: a proof prices the component it is about to
    build precisely because that component is not certified yet.

    There is no third step. Technology.resource_kind exists and is wrong for most
    components; guessing from it would under-price, and an under-price is the
    failure that spends money rather than the one that refuses to.
    """
    code = (component.get("technology_code") or "").strip()
    if code:
        row = session.get(Blueprint, (code, target))
        if row is not None and row.resource_kind:
            return row.resource_kind
    return (component.get("resource_kind") or "").strip() or None


def marketplace_licence(technology_code: str, target: str, ocpus: float,
                        session: Session) -> dict | None:
    """The publisher's hourly fee for a Marketplace image, or None (C9).

    A COST THE PRICE LIST CANNOT SEE. Every Marketplace listing matching this
    catalogue is PAYGO: the publisher charges by the hour on top of compute, in
    their own currency, and none of it appears in Oracle's public rates. nginx
    from Cognosys is USD 0.15 PER_OCPU_LINEAR — about USD 219 a month on two
    OCPUs, and entirely invisible to a gate reading only the price list.

    RETURNED IN ITS OWN CURRENCY, NEVER FOLDED INTO THE TOTAL. This portal
    prices in AED and the rate is USD; adding them would need an exchange rate
    nothing here measures, and inventing one is the recalled-rather-than-measured
    mistake that has already cost this project machines. An approver is better
    served by "AED X, plus USD Y not included" than by one confident wrong
    number.
    """
    from db.models import MarketplaceListing

    row = session.get(MarketplaceListing, (technology_code, target))
    if row is None or row.licence_rate is None or not row.licence_currency:
        return None
    rate = float(row.licence_rate)
    # PER_OCPU_LINEAR is the only strategy measured on this tenancy. An
    # unrecognised one is reported WITHOUT a monthly figure rather than guessed:
    # a wrong number here is worse than an absent one, because a number gets
    # approved.
    if (row.licence_strategy or "").upper() == "PER_OCPU_LINEAR":
        monthly = rate * max(1.0, float(ocpus or 1)) * HOURS_PER_MONTH
    else:
        monthly = None
    return {
        "monthly": round(monthly, 2) if monthly is not None else None,
        "currency": row.licence_currency,
        "rate": rate,
        "strategy": row.licence_strategy or "unknown",
        "publisher": row.publisher,
        "note": (f"{row.publisher} charges {row.licence_currency} {rate} "
                 f"{(row.licence_strategy or 'per hour').lower().replace('_', ' ')} "
                 f"for this image. It is NOT included in the total below, which "
                 f"is in {CURRENCY}."),
    }


def projected_model(component: dict, session: Session) -> str | None:
    """The billing model a component with NO blueprint would be built under.

    REQ-2026-0176 and REQ-2026-0178 both showed 0.00 AED for keycloak, were
    approved at that fiction, and were then correctly refused at execution:
    "monthly 90.59 exceeds approved 0.00 by more than 10%". The guard was right
    every time. What was wrong is that the portal HAD the answer and never asked
    for it.

    What decides a price is the billing model, and for an uncertified component
    the agent's own classifier already decides that: `vm-service` means it will
    be built by extending oci/service-vm — `_ensure_vm_service` certifies against
    the manifest it extended, so the kind is determined, not guessed. Software on
    a machine is billed as a machine.

    A cloud-managed service (`oci-*`, `aws-*`) is a real unknown until a
    blueprint exists, and returns None: an estimate nobody can derive must stay
    an absence rather than become a number.

    Deliberately NOT a resource_kind: naming one here would hard-code a string
    that belongs to the orchestrator's manifests. The model is the whole of what
    pricing needs.
    """
    from api import ai_blueprint

    code = (component.get("technology_code") or "").strip()
    if not code:
        return None
    try:
        kind, _sibling = ai_blueprint.classify(code, session)
    except Exception:  # noqa: BLE001 — an estimate must never break the form
        return None
    return BILLING_VM if kind == "vm-service" else None


def _target_uses_blueprints(session: Session, target: str) -> bool:
    """Whether this target's catalogue is described by blueprints at all.

    Only OCI is today. On a target with no blueprints there is nothing that
    could name a resource kind, and everything in its catalogue is generic
    software on a machine — so the VM model is not an assumption there, it is
    the whole model, and pricing must go on working exactly as before.

    Asked of the data rather than hard-coded, so this corrects itself the day
    another target gains blueprints instead of quietly staying wrong.
    """
    return session.scalar(
        select(Blueprint.technology_code)
        .where(Blueprint.deployment_target == target).limit(1)) is not None


def _not_a_vm_parts(model: str, resource: dict, vcpu: int, memory_gb: int,
                    storage_gb: int) -> tuple[float, float]:
    """(compute_monthly, storage_monthly) for the kinds that are not machines."""
    if model == BILLING_BUCKET:
        # No compute whatsoever, and the first GBs are free.
        free = resource.get("bucket-free-gb", 0.0)
        rate = resource.get("bucket-storage-gb-month", resource.get("storage-gb-month", 0.0))
        return 0.0, max(0.0, storage_gb - free) * rate
    if model == BILLING_CLUSTER:
        # The control plane is a flat fee; the node pool is ordinary compute.
        cluster = resource.get("oke-cluster-hour", 0.0) * HOURS_PER_MONTH
        # The nodes are ordinary machines — priced by the ordinary formula, RAM
        # included. Counting only their vCPUs made a cluster look CHEAPER than
        # the single VM it contains.
        nodes = _cloud_compute_cached(resource, vcpu, memory_gb)
        return cluster + nodes, storage_gb * resource.get("storage-gb-month", 0.0)
    if model == BILLING_MANAGED_DB:
        # A managed database is charged on its own OCPU rate, not the VM one.
        rate = resource.get("psql-vcpu-hour", resource.get("vcpu-hour", 0.0))
        return vcpu * rate * HOURS_PER_MONTH, storage_gb * resource.get("storage-gb-month", 0.0)
    return 0.0, 0.0


def estimate_cost(
    components: list[dict],
    deployment_target: str,
    session: Session,
    advanced: dict | None = None,
) -> dict:
    """Return a cost breakdown + one-time/monthly/annual totals for a target.

    If the target is unknown, totals are zero and each line is marked so.
    `advanced` (6.5) may add cost: high_availability doubles compute, and
    backup_retention / monitoring_level / support_tier add their own lines.
    """
    target = (deployment_target or "").strip()
    advanced = advanced or {}
    ha = bool(advanced.get("high_availability"))
    sizing = resolve_components(components, session)

    known_target = target in DEPLOYMENT_TARGETS
    resource = _rates(session, TARGET_KIND[target]) if known_target else {}
    licences = _rates(session, "licence")
    setup_fee = resource.get("setup", 0.0) if target == "onprem" else 0.0

    # Azure live pricing (2.1): compute from the real API, everything else cached.
    azure_live = target == "azure" and azure_pricing.is_live()
    azure_discount = _discount(session, "cloud_azure") if target == "azure" else 0.0
    pricing_source = "azure-live" if azure_live else "mock"

    # OCI live pricing (2.2): decomposed rates map straight onto the cloud
    # formula, so swap in the live (discounted) rate dict for `resource`.
    if target == "oci" and oci_pricing.is_live():
        try:
            oci_discount = _discount(session, "cloud_oci")
            resource = _apply_discount(oci_pricing.rates(), oci_discount)
            pricing_source = "oci-live"
        except OCIUnavailable:
            pricing_source = "oci-cached"  # keep the cached rate cards

    # AWS live pricing (multi-cloud breadth): same decomposed formula — swap in the
    # live (discounted) rate dict. The live fetch is an extension point, so this
    # falls back to the cached seeded rate cards until it's wired.
    if target == "aws" and aws_pricing.is_live():
        try:
            aws_discount = _discount(session, "cloud_aws")
            resource = _apply_discount(aws_pricing.rates(), aws_discount)
            pricing_source = "aws-live"
        except AWSUnavailable:
            pricing_source = "aws-cached"  # keep the cached rate cards

    # GCP live pricing (multi-cloud breadth): same decomposed formula, same
    # cache fallback as AWS/OCI. The live fetch is an extension point.
    if target == "gcp" and gcp_pricing.is_live():
        try:
            gcp_discount = _discount(session, "cloud_gcp")
            resource = _apply_discount(gcp_pricing.rates(), gcp_discount)
            pricing_source = "gcp-live"
        except GCPUnavailable:
            pricing_source = "gcp-cached"  # keep the cached rate cards

    blueprint_target = _target_uses_blueprints(session, target) if known_target else False

    lines: list[dict] = []
    one_time_total = 0.0
    monthly_total = 0.0
    compute_total = 0.0
    storage_total = 0.0
    licence_total = 0.0

    for raw, comp in zip(components, sizing["components"]):
        compute_monthly = 0.0
        storage_monthly = 0.0
        licence_monthly = 0.0
        one_time = 0.0

        # WHAT is being built decides HOW it is billed. An unrecognised kind is
        # left unpriced and unresolved on purpose: "we do not know what this
        # costs" must not come out looking like a number somebody can approve.
        kind = resource_kind_for(raw, session, target) if known_target else None
        provisional = False
        if kind:
            model = BILLING_MODEL.get(kind)
        elif known_target and not blueprint_target:
            # No blueprints on this target, so nothing could have named a kind.
            # Machines are the only thing it builds; that is not a guess.
            model = BILLING_VM
        else:
            # No blueprint yet — but the agent's classifier already knows what it
            # would build this as, and that decides the billing model. The figure
            # is real; what is provisional is whether the thing gets certified at
            # all, which is why the line says so and the form repeats it.
            model = projected_model(raw, session) if known_target else None
            provisional = model is not None
        # A model whose rate card is incomplete cannot price anything. Say so,
        # rather than charging zero for whatever is missing.
        missing = [item for item in REQUIRED_RATES.get(model or "", ())
                   if item not in resource]
        priceable = (comp["resolved"] and known_target
                     and model is not None and not missing)

        if priceable and model != BILLING_VM:
            compute_monthly, storage_monthly = _not_a_vm_parts(
                model, resource, comp["vcpu"], comp["memory_gb"], comp["storage_gb"])
            licence_item = TECHNOLOGY_LICENCE.get(comp["technology_code"])
            if licence_item:
                licence_monthly = licences.get(licence_item, 0.0)
        elif priceable:
            if azure_live:
                storage_monthly = comp["storage_gb"] * resource.get("storage-gb-month", 0)
                try:
                    compute_monthly = azure_pricing.vm_monthly(comp["size"]) * (1 - azure_discount)
                except AzureUnavailable:
                    # Fall back to the cached rate cards, and flag it (§13).
                    compute_monthly = _cloud_compute_cached(resource, comp["vcpu"], comp["memory_gb"])
                    pricing_source = "azure-cached"
            else:
                compute_monthly, storage_monthly = _component_parts(
                    target, resource, comp["vcpu"], comp["memory_gb"], comp["storage_gb"]
                )
            licence_item = TECHNOLOGY_LICENCE.get(comp["technology_code"])
            if licence_item:
                licence_monthly = licences.get(licence_item, 0.0)
            one_time = setup_fee

        if ha:
            compute_monthly *= HA_COMPUTE_MULTIPLIER  # redundant nodes (6.5)
        resource_monthly = compute_monthly + storage_monthly
        component_monthly = resource_monthly + licence_monthly
        one_time_total += one_time
        monthly_total += component_monthly
        compute_total += compute_monthly
        storage_total += storage_monthly
        licence_total += licence_monthly
        lines.append(
            {
                "technology_name": comp["technology_name"],
                "size": comp["size"],
                "resolved": priceable,
                # The component has no certified blueprint, so this figure is
                # what it WOULD cost if the agent certifies it as expected —
                # real arithmetic, uncertain outcome. Shown labelled rather than
                # hidden, because 0.00 was read as free and approved as free.
                "provisional": provisional and priceable,
                "resource_kind": kind,
                "billing_model": model,
                # Names the rate rows a deployment is missing, so "we cannot
                # price this" arrives with the reason attached.
                "unpriceable": ("missing rates: " + ", ".join(missing)) if missing else None,
                "compute_monthly": round(compute_monthly, 2),
                "storage_monthly": round(storage_monthly, 2),
                "resource_monthly": round(resource_monthly, 2),
                "licence_monthly": round(licence_monthly, 2),
                # A publisher's fee for a Marketplace image, in the publisher's
                # own currency and deliberately NOT added to the totals below.
                "external_licence": marketplace_licence(
                    raw.get("technology_code") or "", target or "",
                    comp.get("ocpus") or comp.get("vcpu") or 1, session)
                if target else None,
                "one_time": round(one_time, 2),
                "monthly": round(component_monthly, 2),
            }
        )

    # Advanced-option add-ons (6.5), driven by the selected options. Each is 0
    # when its option is unset, so a plain request keeps its base total.
    backup_total = round(storage_total * BACKUP_FACTOR.get(str(advanced.get("backup_retention")), 0.0), 2)
    monitoring_total = MONITORING_MONTHLY.get(advanced.get("monitoring_level"), 0.0)
    support_base = compute_total + storage_total + licence_total + backup_total + monitoring_total
    support_total = round(support_base * SUPPORT_PCT.get(advanced.get("support_tier"), 0.0), 2)
    monthly_total += backup_total + monitoring_total + support_total

    # WHAT COULD NOT BE PRICED, at the top level where a decision can see it.
    #
    # FOUND 2026-08-21 on REQ-2026-0175. The line for `keycloak` correctly said
    # resolved=False, and the total said 0.00 — so the proof's cost gate read
    # "free", approved it against a AED 300 ceiling, and built for real. An
    # honest signal that only the line carries is not a signal at all: whatever
    # ACTS on the number must be able to see it.
    unpriced = [li["technology_name"] for li in lines if not li["resolved"]]
    # Priced, but on a component nothing has certified yet. Named at the top
    # level for the same reason `unpriced` is: whatever ACTS on the number has
    # to be able to see its status, and only the total reaches most callers.
    provisional_names = [li["technology_name"] for li in lines
                         if li.get("provisional")]
    # NAMED AT THE TOP LEVEL, for the reason `unpriced` is. A cost that only the
    # line carries is a cost the gate cannot see, and this project has already
    # approved a AED 300 cap against a total of 0.00 once. Here the money is
    # KNOWN — it is simply in another currency and cannot honestly be added, so
    # whatever acts on the total has to be told it is incomplete.
    external_licences = [
        {"technology_name": li["technology_name"], **li["external_licence"]}
        for li in lines if li.get("external_licence")]

    return {
        "currency": CURRENCY,
        "unpriced": unpriced,
        "provisional": provisional_names,
        "external_licences": external_licences,
        "deployment_target": target or None,
        "known_target": known_target,
        "pricing_source": pricing_source,
        "lines": lines,
        # Per-category subtotals. Compute/storage/licence come from the components
        # (6.4); backup/monitoring/support are advanced-option add-ons (6.5). The
        # six sum to the monthly total. Network stays usage-based (not estimated).
        "by_category": {
            "compute": round(compute_total, 2),
            "storage": round(storage_total, 2),
            "licence": round(licence_total, 2),
            "backup": backup_total,
            "monitoring": round(monitoring_total, 2),
            "support": support_total,
        },
        "totals": {
            "one_time": round(one_time_total, 2),
            "monthly": round(monthly_total, 2),
            "annual": round(monthly_total * 12, 2),
        },
    }


# --- Cost from a resolved placement (P.7) ------------------------------------
#
# `estimate_cost` prices COMPONENTS. Once placement exists that is the wrong
# unit, and wrong in a way that makes the cheaper option look dearer:
#
#   Compute and storage are bought ONCE PER MACHINE. Three components on one
#   host is one machine's compute, not three. Pricing per component charges
#   consolidation as though it were separation and hides the saving it exists
#   to offer.
#
#   A setup fee is what it costs to stand a machine up, so it too is per host.
#   Charging it per component would bill three setups for the one machine a
#   consolidated option builds — making the option the portal recommends look
#   worse the more it saves. This differs from the pre-placement path, where a
#   component and a machine were the same thing and the distinction could not
#   arise.
#
#   A LICENCE is per component wherever it runs. Two licensed engines on one
#   host need two licences; moving them apart changes nothing about that. So
#   licences are added per component and compute is not.
#
#   A managed service is priced by its own rate, not as a machine — the cloud's
#   OCPU charge, not a VM that nobody provisions.

def estimate_placement_cost(
    sized: dict,
    deployment_target: str,
    session: Session,
    component_sizes: dict[str, str] | None = None,
    advanced: dict | None = None,
) -> dict:
    """Price a topology from P.6's sized hosts.

    `sized` is what `api.sizing.size_hosts` returned. Machines are priced from
    each host's summed shape; managed components are priced through the ordinary
    per-component path so they keep their own billing model; licences are added
    once per licensed component.

    An unresolved host makes the whole estimate unresolved. A number that is
    missing a machine is worse than no number, because somebody can approve it.
    """
    component_sizes = component_sizes or {}
    hosts = sized.get("hosts", [])

    # ORDER IS LOAD-BEARING. `placement_attachment_rows` rebuilds the approver's
    # table by walking `hosts` the same way, and the PDF pairs its rows with the
    # priced lines BY POSITION. Both walks are in host order, machines before
    # managed; changing either without the other silently puts one machine's
    # price against another machine's name.
    machines: list[dict] = []
    managed: list[dict] = []
    placed_components: list[str] = []

    for host in hosts:
        placed_components.extend(host.get("components") or ())
        if host.get("host_mode") == "managed":
            for code in host.get("components") or ():
                managed.append({"technology_code": code,
                                "size": component_sizes.get(code)})
            continue
        if not host.get("resolved"):
            continue
        # One machine, at the shape P.6 resolved for it. The explicit vCPU /
        # memory / storage override the size anchor, which is what makes a
        # summed host price as the machine it will actually be.
        machines.append({
            "technology_code": "compute-vm",
            "size": component_sizes.get((host.get("components") or [None])[0]),
            "vcpu": host.get("vcpu"),
            "memory_gb": host.get("memory_gb"),
            "storage_gb": host.get("storage_gb"),
        })

    machine_cost = (estimate_cost(machines, deployment_target, session, advanced)
                    if machines else None)
    managed_cost = (estimate_cost(managed, deployment_target, session, advanced)
                    if managed else None)

    # Licences for everything on a machine. The managed estimate already carries
    # licences for what it priced, so those are not added again.
    licence_rates = _rates(session, "licence")
    managed_codes = {c["technology_code"] for c in managed}
    licence_monthly = 0.0
    licence_lines: list[dict] = []
    for code in placed_components:
        if code in managed_codes:
            continue
        item = TECHNOLOGY_LICENCE.get(code)
        if not item:
            continue
        amount = licence_rates.get(item, 0.0)
        licence_monthly += amount
        licence_lines.append({"technology_code": code, "item": item,
                              "monthly": amount})

    def total(estimate: dict | None, field: str) -> float:
        return float(estimate["totals"][field]) if estimate else 0.0

    monthly = (total(machine_cost, "monthly") + total(managed_cost, "monthly")
               + licence_monthly)
    one_time = total(machine_cost, "one_time") + total(managed_cost, "one_time")

    # EVERYTHING BELOW EXISTS SO THIS CAN REPLACE THE PER-COMPONENT BREAKDOWN
    # RATHER THAN SIT BESIDE IT (P.12).
    #
    # Once a placement is chosen, the per-component estimate priced a different
    # arrangement of the same components — so it is not a second opinion, it is
    # the wrong answer. But the approval ticket, the budget guardrail and the
    # signed handoff all read the SHAPE `estimate_cost` returns, and every field
    # in that shape is there because an approver was once misled without it: the
    # licence split, the "not the whole cost" caveat, the foreign-currency
    # licence note. Swapping in a thinner dict would have silently dropped all
    # three from the ticket while appearing to work.
    #
    # So the placement estimate reports the same facts, derived from what it
    # actually priced.
    def merge(field: str) -> list:
        out: list = []
        for estimate in (machine_cost, managed_cost):
            for item in (estimate or {}).get(field) or []:
                if item not in out:
                    out.append(item)
        return out

    # WHAT HAS NO PRICE, named. Components on a host P.6 refused to size are the
    # real gap and the sub-estimates cannot report them: an unresolved host is
    # skipped before anything is priced, so without this the components on it
    # would vanish from the estimate entirely and the total would look complete.
    # That is the failure mode this project has been bitten by twice — a figure
    # that is missing a machine is worse than no figure, because someone can
    # approve it.
    unpriced: list[str] = []
    for host in hosts:
        if host.get("resolved") or host.get("host_mode") == "managed":
            continue
        for code in host.get("components") or ():
            if code not in unpriced:
                unpriced.append(code)
    for name in merge("unpriced"):
        if name not in unpriced:
            unpriced.append(name)

    by_category = {key: 0.0 for key in
                   ("compute", "storage", "licence", "backup", "monitoring",
                    "support")}
    for estimate in (machine_cost, managed_cost):
        for key, amount in ((estimate or {}).get("by_category") or {}).items():
            if key in by_category:
                by_category[key] += float(amount or 0.0)
    # Licences priced per component here, not by the sub-estimates.
    by_category["licence"] += licence_monthly
    by_category = {key: round(value, 2) for key, value in by_category.items()}

    return {
        "currency": (machine_cost or managed_cost or {}).get("currency", "AED"),
        "pricing_source": (machine_cost or managed_cost or {}).get("pricing_source"),
        "deployment_target": (deployment_target or "").strip() or None,
        "known_target": bool((machine_cost or managed_cost or {})
                             .get("known_target", True)),
        "machine_count": sized.get("machine_count", 0),
        "machines": machine_cost,
        "managed": managed_cost,
        "licences": licence_lines,
        # The per-line detail the cost sheet reads. Machines appear as machines,
        # because a machine is what is bought — pricing per component is the
        # mistake the whole of this function exists to correct.
        "lines": merge("lines"),
        "unpriced": unpriced,
        "provisional": merge("provisional"),
        "external_licences": merge("external_licences"),
        "by_category": by_category,
        "resolved": bool(sized.get("resolved")) and (
            machine_cost is None or not machine_cost.get("unpriced")),
        "totals": {
            "one_time": round(one_time, 2),
            "monthly": round(monthly, 2),
            "annual": round(monthly * 12, 2),
        },
    }


def placement_attachment_rows(sizing: dict) -> dict:
    """The approver's attachments, described as machines rather than components.

    WHY THIS EXISTS. `build_request_pdf` and `build_cost_sheet_xlsx` pair
    `sizing["components"][i]` with `breakdown["lines"][i]` BY POSITION. That was
    safe while a component and a priced line were the same thing. Once a
    placement supplies the cost they are not: the lines price MACHINES, and three
    components on one host produce one line. Left alone, the PDF would have
    printed a machine's monthly figure beside a component's name — a per-component
    price that was never calculated, in the document handed to the person
    approving the money.

    So the rows are rebuilt to match what was actually priced, in the order it
    was priced: resolved machines first, then managed components, which is the
    order `estimate_placement_cost` merges its lines in.

    A host P.6 could not size is included and marked, not dropped. It is a
    machine that will be built; leaving it out would make the table look like the
    whole request.
    """
    hosts = sizing.get("hosts") or []
    rows: list[dict] = []

    for host in hosts:
        if host.get("host_mode") == "managed" or not host.get("resolved"):
            continue
        components = ", ".join(host.get("components") or ()) or "nothing"
        rows.append({
            "technology_code": "compute-vm",
            "technology_name": f"{host.get('host_id')}: {components}",
            "size": host.get("host_mode"),
            "vcpu": host.get("vcpu"),
            "memory_gb": host.get("memory_gb"),
            "storage_gb": host.get("storage_gb"),
            "resolved": True,
            "custom_shape": False,
        })

    for host in hosts:
        if host.get("host_mode") != "managed":
            continue
        for code in host.get("components") or ():
            rows.append({
                "technology_code": code,
                "technology_name": f"{code} (run by the cloud)",
                "size": None, "vcpu": None, "memory_gb": None,
                "storage_gb": None, "resolved": True, "custom_shape": False,
            })

    # Appended last so it cannot shift the positions above, and carrying no
    # figure of its own — the price it would need is exactly what is missing.
    for host in hosts:
        if host.get("resolved") or host.get("host_mode") == "managed":
            continue
        components = ", ".join(host.get("components") or ()) or "nothing"
        rows.append({
            "technology_code": None,
            "technology_name": f"{host.get('host_id')}: {components} (SIZE NOT DETERMINED)",
            "size": host.get("host_mode"), "vcpu": None, "memory_gb": None,
            "storage_gb": None, "resolved": False, "custom_shape": False,
        })

    return {"components": rows}


def compare_options(priced: list[dict]) -> list[dict]:
    """Attach each option's monthly delta against the cheapest priceable one.

    The delta is the point: "consolidated" and "separated" mean nothing to a
    requester until one of them carries a number saying what choosing it costs.
    Options that could not be priced are left out of the comparison rather than
    treated as free — a zero would make an unpriceable option look like the
    bargain of the set.
    """
    # An option claiming to be priced must carry the figure. Without this the
    # wrong shape produces no deltas at all rather than an error — which is how
    # a caller nesting `resolved` and `totals` one level down got every option
    # back marked unpriceable, silently, with a plausible response body.
    for option in priced:
        if option.get("resolved") and "totals" not in option:
            raise ValueError(
                "a resolved option must carry `totals`; got keys "
                f"{sorted(option)}")

    priceable = [p for p in priced if p.get("resolved")]
    if not priceable:
        return [dict(p, monthly_delta=None, cheapest=False) for p in priced]

    baseline = min(p["totals"]["monthly"] for p in priceable)
    out = []
    for option in priced:
        if not option.get("resolved"):
            out.append(dict(option, monthly_delta=None, cheapest=False))
            continue
        delta = round(option["totals"]["monthly"] - baseline, 2)
        out.append(dict(option, monthly_delta=delta,
                        cheapest=(delta == 0)))
    return out
