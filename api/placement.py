"""Which host topologies a selection could run on, and which of them are allowed.

WHAT THIS SOLVES. A requester picks "VM with OS", "PostgreSQL" and "Node.js" and
the request goes straight to costing. Nothing has decided whether that is one
machine or three — so the estimate cannot be right, the Terraform plan has no
topology to select modules against, and the approver in Jira reads a shopping
list rather than an architecture.

This module turns a selection into the ordered options of Scenario A:

    1. managed where available   the cloud runs what it can; you patch less
    2. consolidated              one machine carries everything
    3. separated                 one machine each

WHY IT DECIDES NOTHING. It enumerates and asks; it never judges. Whether an
option is permitted is answered by OPA (`infra.placement`, P.3), because the
policy is the authority and app code that reimplements a rule is a second
authority that will one day disagree with the first.

WHY A DENIED OPTION IS STILL RETURNED. It comes back marked ineligible with the
policy's own sentence attached, never dropped. An option that vanishes without
explanation makes the portal look broken and produces the support ticket the
portal exists to prevent — so "why can't I pick that one?" is answered on the
screen where the question is asked.

NO I/O. Everything this needs arrives as arguments, including the policy
evaluator. That keeps it testable without a database or a running OPA, and it
keeps the catalogue lookups in one place (the API layer) rather than scattered
through the enumeration.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field

# Host modes, mirroring db.models. Repeated as literals rather than imported so
# this module stays free of the database layer and can be reasoned about alone.
HOST_VM = "vm"
HOST_CONTAINER = "container"
HOST_MANAGED = "managed"

MANAGED_OPTION = "managed"
CONSOLIDATED_OPTION = "consolidated"
SEPARATED_OPTION = "separated"
EXISTING_CLUSTER_OPTION = "existing-cluster"
NEW_CLUSTER_OPTION = "new-cluster"

# The order the requester sees them in. Managed first because it is the option
# that removes work from them; separated last because it is the most expensive.
OPTION_ORDER = (MANAGED_OPTION, CONSOLIDATED_OPTION, SEPARATED_OPTION)

# The name each layout goes by, in one place.
#
# Only the KEY is persisted with a placement, so anything reading a stored
# placement back -- the approval ticket, most of all -- has to turn "consolidated"
# into something a person can judge. Written here rather than wherever it is
# needed: an approver and a requester describing the same layout differently is
# the kind of drift nobody notices until the two are read side by side.
OPTION_TITLES: dict[str, str] = {
    MANAGED_OPTION: "Managed where available",
    CONSOLIDATED_OPTION: "Consolidated",
    SEPARATED_OPTION: "Separated",
    EXISTING_CLUSTER_OPTION: "Deploy onto an existing cluster",
    NEW_CLUSTER_OPTION: "Provision a new cluster",
}


def option_title(key: str) -> str:
    """The human name for a layout key, falling back to the key itself.

    An unknown key is returned as-is rather than replaced with "Unknown": a
    ticket naming a layout the code no longer recognises is still telling the
    truth about what was chosen, and hiding it would not make it recognised.
    """
    return OPTION_TITLES.get(key, key)


@dataclass(frozen=True)
class ComponentFacts:
    """What the catalogue knows about one selected component, on one cloud.

    `is_host` marks a component that IS a machine rather than something placed
    on one — "VM with OS" is the host its neighbours land on, not a workload
    competing for space on someone else's.
    """

    code: str
    host_modes: frozenset[str]
    is_host: bool = False
    # True for OKE/AKS: the component IS a cluster, so workloads land ON it
    # rather than beside it. Determined from the BLUEPRINT's resource kind, never
    # from Technology.resource_kind, whose own docstring says it "exists and is
    # wrong for most components".
    provides_cluster: bool = False

    def supports(self, mode: str) -> bool:
        return mode in self.host_modes

    @property
    def hosts_others(self) -> bool:
        """A machine or a cluster: something other components are placed on."""
        return self.is_host or self.provides_cluster


@dataclass(frozen=True)
class Host:
    id: str
    host_mode: str
    components: tuple[str, ...]

    def as_dict(self) -> dict:
        return {"id": self.id, "host_mode": self.host_mode,
                "components": list(self.components)}


@dataclass(frozen=True)
class Option:
    """One way the selection could be laid out, and whether it is permitted."""

    key: str
    title: str
    summary: str
    hosts: tuple[Host, ...]
    eligible: bool = True
    reasons: tuple[str, ...] = field(default_factory=tuple)
    # Clusters this option could land on, each already assessed for entitlement,
    # capacity and quota by api.clusters. Only "existing-cluster" carries any.
    clusters: tuple = field(default_factory=tuple)
    # Advisory, never blocking: running a database on Kubernetes is a legitimate
    # choice, and the portal's job is to see it made knowingly.
    warnings: tuple[str, ...] = field(default_factory=tuple)

    @property
    def host_count(self) -> int:
        """Machines this option would provision. A managed service is not one."""
        return sum(1 for h in self.hosts if h.host_mode != HOST_MANAGED)

    def as_dict(self) -> dict:
        return {
            "key": self.key,
            "title": self.title,
            "summary": self.summary,
            "hosts": [h.as_dict() for h in self.hosts],
            "host_count": self.host_count,
            "eligible": self.eligible,
            "reasons": list(self.reasons),
            "clusters": [c.as_dict() if hasattr(c, "as_dict") else c
                         for c in self.clusters],
            "warnings": list(self.warnings),
        }


def topology_document(option: Option, environment: str,
                      deployment_target: str) -> dict:
    """The shape `infra.placement` expects as its input."""
    return {
        "environment": environment,
        "deployment_target": deployment_target,
        "hosts": [h.as_dict() for h in option.hosts],
    }


def _workloads(components: Sequence[ComponentFacts]) -> list[ComponentFacts]:
    """The components that need placing, as opposed to the things they land on."""
    return [c for c in components if not c.hosts_others]


def _managed_option(components: Sequence[ComponentFacts]) -> Option | None:
    """Everything the cloud can run, run by the cloud; the rest on one machine.

    Returns None when nothing in the selection is available as a managed
    service. Offering an option identical to the consolidated one, under a name
    promising less operational burden, would be a lie told twice.
    """
    workloads = _workloads(components)
    managed = [c for c in workloads if c.supports(HOST_MANAGED)]
    if not managed:
        return None

    remaining = [c for c in workloads if not c.supports(HOST_MANAGED)]
    hosts = [Host(id=f"managed-{c.code}", host_mode=HOST_MANAGED,
                  components=(c.code,)) for c in managed]
    if remaining:
        hosts.append(Host(id="host-1", host_mode=HOST_VM,
                          components=tuple(c.code for c in remaining)))

    names = ", ".join(c.code for c in managed)
    return Option(
        key=MANAGED_OPTION,
        title=OPTION_TITLES[MANAGED_OPTION],
        summary=(f"The cloud runs {names}. Patching, backups and failover for "
                 f"it stop being yours."),
        hosts=tuple(hosts),
    )


def _consolidated_option(components: Sequence[ComponentFacts]) -> Option | None:
    """One machine carrying everything that needs one."""
    workloads = _workloads(components)
    if len(workloads) < 2:
        return None  # nothing to consolidate; it would equal `separated`
    return Option(
        key=CONSOLIDATED_OPTION,
        title=OPTION_TITLES[CONSOLIDATED_OPTION],
        summary=(f"One machine hosts all {len(workloads)} components. Their "
                 f"resource needs are summed, not maximised."),
        hosts=(Host(id="host-1", host_mode=HOST_VM,
                    components=tuple(c.code for c in workloads)),),
    )


def _separated_option(components: Sequence[ComponentFacts]) -> Option | None:
    """A machine each."""
    workloads = _workloads(components)
    if not workloads:
        return None
    hosts = tuple(
        Host(id=f"host-{i}", host_mode=HOST_VM, components=(c.code,))
        for i, c in enumerate(workloads, start=1)
    )
    return Option(
        key=SEPARATED_OPTION,
        title=OPTION_TITLES[SEPARATED_OPTION],
        summary=(f"{len(hosts)} machines, one per component. The most isolated "
                 f"and the most expensive."),
        hosts=hosts,
    )


# Workloads that keep data. On a cluster their storage, failover and backups
# become the requester's burden rather than the platform's, which is worth
# saying out loud without refusing the choice.
STATEFUL = frozenset({"postgres16", "mysql", "mssql", "oracle-free", "mongodb",
                      "valkey", "opensearch"})


def _cluster_warnings(workloads: Sequence[ComponentFacts]) -> tuple[str, ...]:
    """Advice for putting data-keeping workloads on Kubernetes.

    Never a refusal. Running a database on a cluster is a legitimate choice, and
    the portal's job is to see it made knowingly rather than by default. Where
    the same component exists as a managed service that is named, because the
    alternative is more useful to a requester than the caution.
    """
    warnings = []
    for component in workloads:
        if component.code not in STATEFUL:
            continue
        message = (
            f"{component.code} keeps data. On Kubernetes its storage, failover "
            f"and backups become your team's responsibility rather than the "
            f"platform's.")
        if component.supports(HOST_MANAGED):
            message += (
                f" The cloud's managed {component.code} carries none of that — "
                f"it is offered as 'Managed where available'.")
        warnings.append(message)
    return tuple(warnings)


def _existing_cluster_option(components: Sequence[ComponentFacts],
                             clusters: Sequence | None) -> Option | None:
    """Land the workloads on a cluster that already exists.

    `clusters` has already been through api.clusters: out-of-scope ones are
    absent entirely (entitlement is a visibility boundary), and ones that do not
    fit are present and marked ineligible with the number that stopped them.

    An empty list does NOT mean "hide this option". A requester entitled to no
    cluster needs to be told that, not left wondering why an option the
    documentation mentions is missing from their screen.
    """
    workloads = _workloads(components)
    if not workloads:
        return None

    hosts = (Host(id="existing-cluster", host_mode=HOST_CONTAINER,
                  components=tuple(c.code for c in workloads)),)
    if clusters is None:
        # Discovery is unavailable, which is NOT the same as "you have none" and
        # must not be reported as it. Telling a requester they are entitled to
        # nothing, when the truth is that nobody looked, is a lie the portal
        # would be believed about.
        return Option(
            key=EXISTING_CLUSTER_OPTION,
            title=OPTION_TITLES[EXISTING_CLUSTER_OPTION],
            summary="Existing clusters cannot be listed at the moment.",
            hosts=hosts, eligible=False,
            reasons=("The portal cannot list existing clusters yet, so it "
                     "cannot tell you which ones you could deploy onto. This is "
                     "a gap in the portal, not a statement about your access. "
                     "Provisioning a new cluster is offered below.",),
            warnings=_cluster_warnings(workloads),
        )

    usable = [c for c in clusters if getattr(c, "eligible", False)]

    if not clusters:
        return Option(
            key=EXISTING_CLUSTER_OPTION,
            title=OPTION_TITLES[EXISTING_CLUSTER_OPTION],
            summary="No cluster is available to you.",
            hosts=hosts, eligible=False,
            reasons=("You are not entitled to any existing cluster in this "
                     "project, cost centre and subsidiary. Provisioning a new "
                     "one is offered below.",),
            warnings=_cluster_warnings(workloads),
        )

    if not usable:
        return Option(
            key=EXISTING_CLUSTER_OPTION,
            title=OPTION_TITLES[EXISTING_CLUSTER_OPTION],
            summary=f"{len(clusters)} cluster(s), none of which can take this.",
            hosts=hosts, eligible=False,
            reasons=tuple(
                f"{c.cluster.name}: {' '.join(c.reasons)}" for c in clusters),
            clusters=tuple(clusters),
            warnings=_cluster_warnings(workloads),
        )

    return Option(
        key=EXISTING_CLUSTER_OPTION,
        title=OPTION_TITLES[EXISTING_CLUSTER_OPTION],
        summary=(f"{len(usable)} of {len(clusters)} cluster(s) can take this "
                 f"workload. Nothing new is provisioned."),
        hosts=hosts,
        clusters=tuple(clusters),
        warnings=_cluster_warnings(workloads),
    )


def _new_cluster_option(components: Sequence[ComponentFacts]) -> Option | None:
    """Provision a cluster as part of this request.

    Always offered when a cluster was asked for, including when the requester
    has no existing cluster — it is the answer in that case, not a fallback.
    """
    providers = [c for c in components if c.provides_cluster]
    if not providers:
        return None

    workloads = _workloads(components)
    name = ", ".join(c.code for c in providers)
    return Option(
        key=NEW_CLUSTER_OPTION,
        title=OPTION_TITLES[NEW_CLUSTER_OPTION],
        summary=(f"A new {name} cluster, with its node pool sized from the "
                 f"{len(workloads)} workload(s) placed on it."
                 if workloads else
                 f"A new {name} cluster with no workloads placed on it yet."),
        hosts=(Host(id="new-cluster", host_mode=HOST_CONTAINER,
                    components=tuple(c.code for c in workloads)),),
        warnings=_cluster_warnings(workloads),
    )


def enumerate_options(components: Sequence[ComponentFacts],
                      clusters: Sequence | None = None) -> list[Option]:
    """Every layout worth offering, in the order the requester should see them.

    Which scenario applies is decided by the SELECTION, not by a flag. Asking for
    OKE or AKS is asking for a cluster, so the cluster options are the answer and
    "one machine or three" is not a question about it. Everything else gets
    Scenario A.

    The managed option appears in both, because a stateful workload put on
    Kubernetes should always be able to see the alternative that removes the
    operational burden it is about to take on.

    No judgement here — an option that policy forbids is still enumerated, so
    that `resolve_options` can return it with the reason attached rather than
    leaving the requester to guess why a choice they expected is missing.
    """
    if any(c.provides_cluster for c in components):
        built = [
            _existing_cluster_option(components, clusters),
            _new_cluster_option(components),
            _managed_option(components),
        ]
        return [option for option in built if option is not None]

    builders = {
        MANAGED_OPTION: _managed_option,
        CONSOLIDATED_OPTION: _consolidated_option,
        SEPARATED_OPTION: _separated_option,
    }
    built = [builders[key](components) for key in OPTION_ORDER]
    return [option for option in built if option is not None]


def resolve_options(
    components: Iterable[ComponentFacts],
    environment: str,
    deployment_target: str,
    evaluate: Callable[[dict], dict],
    clusters: Sequence | None = None,
) -> list[Option]:
    """The options for this selection, each marked eligible or refused.

    `evaluate` is given a topology document and returns OPA's answer —
    `{"allow": bool, "violations": [str, ...]}`. Injected rather than imported
    so this stays testable without a running OPA, and so the caller decides what
    a policy outage means; here it can only mean "refused", never "allowed".
    """
    components = list(components)
    resolved: list[Option] = []

    for option in enumerate_options(components, clusters):
        # An option already refused when it was built — no entitled cluster, say
        # — is not sent to the policy: it has its reason, and asking whether a
        # layout nobody can have is permitted would overwrite that reason with a
        # less useful one.
        if not option.eligible:
            resolved.append(option)
            continue

        answer = evaluate(topology_document(option, environment, deployment_target))
        allowed = bool(answer.get("allow"))
        violations = tuple(answer.get("violations", ()))
        if allowed:
            resolved.append(option)
            continue
        # A refusal with no sentence is the failure mode this module exists to
        # prevent, so supply one rather than render an empty "unavailable".
        reasons = violations or (
            "This layout is not permitted here, and the policy gave no reason. "
            "Report this: a refusal without an explanation is a defect.",)
        resolved.append(Option(
            key=option.key, title=option.title, summary=option.summary,
            hosts=option.hosts, eligible=False, reasons=reasons,
            clusters=option.clusters, warnings=option.warnings,
        ))
    return resolved
